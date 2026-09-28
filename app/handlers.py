"""Handlers de Telegram: reporte, asignación atómica, cierre por reply, baja/reemplazo y administración."""
import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import ai
from .catalogo import Ubicacion
from .config import settings
from .database import SessionLocal
from .mailer import enviar_correo_baja
from .models import (
    ROLES_SOPORTE, Activo, Estacion, EstadoActivo as A, EstadoIncidencia as E, Incidencia, Rol, Usuario,
)
from .render import desc_activo, h, teclado_borrador, teclado_tomar, texto_borrador, texto_incidencia
from .reports import generar_informe_soporte_en_bg, sincronizar_inventario_en_bg
from .resolver import resolver_activo
from .utils import fmt_duracion, segundos_entre, utcnow

log = logging.getLogger(__name__)
Ctx = ContextTypes.DEFAULT_TYPE
HTML = ParseMode.HTML
AUDIO_DIR = Path(settings.AUDIO_DIR)


# ================================================================= helpers
async def get_usuario(s: AsyncSession, chat_id: int) -> Usuario | None:
    r = await s.exec(select(Usuario).where(Usuario.chat_id == chat_id, Usuario.activo == True))  # noqa: E712
    return r.first()


def es_admin_user(user: Usuario | None, chat_id: int) -> bool:
    if chat_id in settings.admin_ids:
        return True
    return bool(user and user.rol == Rol.ADMIN.value)


async def guardar_audio(ctx: Ctx, file_id: str, nombre: str) -> str:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    destino = AUDIO_DIR / nombre
    archivo = await ctx.bot.get_file(file_id)
    await archivo.download_to_drive(custom_path=destino)
    return str(destino)


async def refrescar_caso(ctx: Ctx, s: AsyncSession, inc: Incidencia) -> None:
    """Actualiza el mensaje original del caso en el grupo de Soporte TI."""
    if not inc.msg_soporte_id:
        return
    try:
        await ctx.bot.edit_message_text(
            await texto_incidencia(s, inc), chat_id=settings.SUPPORT_CHAT_ID,
            message_id=inc.msg_soporte_id, parse_mode=HTML,
        )
    except BadRequest as e:
        log.warning("No se pudo refrescar el caso %s: %s", inc.id, e)


# ================================================================= comandos
async def cmd_chatid(update: Update, ctx: Ctx) -> None:
    await update.effective_message.reply_text(
        f"chat_id de este chat: {update.effective_chat.id}\nTu user id: {update.effective_user.id}"
    )


async def cmd_start(update: Update, ctx: Ctx) -> None:
    async with SessionLocal() as s:
        u = await get_usuario(s, update.effective_user.id)
    if u:
        await update.message.reply_text(
            f"Hola {u.nombre} (rol: {u.rol}). Mándame una nota de voz o texto para reportar una falla."
        )
    else:
        await update.message.reply_text(
            f"No estás autorizado. Pásale este ID al administrador: {update.effective_user.id}"
        )


async def cmd_help(update: Update, ctx: Ctx) -> None:
    """/help o /ayuda — muestra los comandos disponibles según el rol del usuario."""
    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
    if not user:
        await update.message.reply_text(
            f"No estás registrado. Tu ID de usuario es <code>{update.effective_user.id}</code>; pásaselo al administrador.",
            parse_mode=HTML,
        )
        return

    es_admin = es_admin_user(user, update.effective_user.id)
    es_informes = es_admin or (user.rol in ROLES_INFORMES)

    lineas = [
        f"<b>Comandos disponibles (Rol: {user.rol})</b>\n",
        "<b>General:</b>",
        "/start — Inicia el bot y verifica tu autorización.",
        "/help o /ayuda — Muestra esta lista de comandos.",
        "/chatid — Muestra el User ID y el ID del chat actual.\n",
    ]

    if es_informes:
        lineas.extend([
            "<b>Informes e Inventario:</b>",
            "/sync_inventario — Actualiza y descarga el libro Excel de inventario.",
            "/informe_soporte [diario|semanal|mensual] — Genera y descarga el reporte de Soporte TI.\n",
        ])

    if es_admin:
        lineas.extend([
            "<b>Administración:</b>",
            "/cargar_inventario — Permite subir un archivo .csv o .xlsx para cargar equipos.",
            "/alta &lt;user_id&gt; &lt;rol&gt; &lt;nombre&gt; — Registra o actualiza un usuario.",
            "/nombre &lt;user_id&gt; &lt;nuevo_nombre&gt; — Cambia el nombre visible de un usuario.",
            "/desactivar &lt;user_id&gt; — Desactiva el acceso a un usuario.",
            "/usuarios — Lista todos los usuarios registrados en el sistema.",
        ])

    await update.message.reply_text("\n".join(lineas), parse_mode=HTML)


async def cmd_alta(update: Update, ctx: Ctx) -> None:
    """/alta <user_id> <funcionario|soporte|jefatura|admin> <nombre>  (solo admin)"""
    async with SessionLocal() as s:
        admin = await get_usuario(s, update.effective_user.id)
        if not es_admin_user(admin, update.effective_user.id):
            return
        roles = {r.value for r in Rol}
        if len(ctx.args) < 3 or ctx.args[1] not in roles or not ctx.args[0].lstrip("-").isdigit():
            await update.message.reply_text(f"Uso: /alta <user_id> <{'|'.join(sorted(roles))}> <nombre>")
            return
        chat_id, rol, nombre = int(ctx.args[0]), ctx.args[1], " ".join(ctx.args[2:])
        u = (await s.exec(select(Usuario).where(Usuario.chat_id == chat_id))).first()
        if u:
            u.nombre, u.rol, u.activo = nombre, rol, True
        else:
            s.add(Usuario(chat_id=chat_id, nombre=nombre, rol=rol))
        await s.commit()
    await update.message.reply_text(f"[OK] {nombre} ({rol}) autorizado.")


async def cmd_nombre(update: Update, ctx: Ctx) -> None:
    """/nombre <user_id> <nuevo_nombre>  (solo admin)"""
    async with SessionLocal() as s:
        admin = await get_usuario(s, update.effective_user.id)
        if not es_admin_user(admin, update.effective_user.id):
            return
        if len(ctx.args) < 2 or not ctx.args[0].lstrip("-").isdigit():
            await update.message.reply_text("Uso: /nombre <user_id> <nuevo_nombre>")
            return
        chat_id, nuevo_nombre = int(ctx.args[0]), " ".join(ctx.args[1:])
        u = (await s.exec(select(Usuario).where(Usuario.chat_id == chat_id))).first()
        if not u:
            await update.message.reply_text("No existe ese usuario.")
            return
        u.nombre = nuevo_nombre
        await s.commit()
    await update.message.reply_text(f"[OK] Nombre actualizado: {nuevo_nombre} (ID: {chat_id})")


async def cmd_desactivar(update: Update, ctx: Ctx) -> None:
    """/desactivar <user_id>  (solo admin)"""
    async with SessionLocal() as s:
        admin = await get_usuario(s, update.effective_user.id)
        if not es_admin_user(admin, update.effective_user.id):
            return
        if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
            await update.message.reply_text("Uso: /desactivar <user_id>")
            return
        u = (await s.exec(select(Usuario).where(Usuario.chat_id == int(ctx.args[0])))).first()
        if not u:
            await update.message.reply_text("No existe ese usuario.")
            return
        u.activo = False
        await s.commit()
    await update.message.reply_text(f"[OK] Usuario {u.nombre} desactivado.")


async def cmd_usuarios(update: Update, ctx: Ctx) -> None:
    async with SessionLocal() as s:
        admin = await get_usuario(s, update.effective_user.id)
        if not es_admin_user(admin, update.effective_user.id):
            return
        usuarios = (await s.exec(select(Usuario).order_by(Usuario.rol, Usuario.nombre))).all()
    lineas = [f"{u.chat_id} · {u.nombre} · {u.rol}" + ("" if u.activo else " · INACTIVO") for u in usuarios]
    await update.message.reply_text("\n".join(lineas) or "Sin usuarios.")


ROLES_INFORMES = {Rol.ADMIN.value, Rol.SOPORTE.value, Rol.JEFATURA.value}


async def cmd_sync_inventario(update: Update, ctx: Ctx) -> None:
    """/sync_inventario — actualiza el Excel de inventario (Postgres -> nube) y lo envía por Telegram."""
    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
    if not (es_admin_user(user, update.effective_user.id) or (user and user.rol in ROLES_INFORMES)):
        await update.message.reply_text("No tienes permiso para pedir este informe.")
        return
    aviso = await update.message.reply_text("Actualizando inventario…")
    ruta = await sincronizar_inventario_en_bg()
    if ruta is None:
        await aviso.edit_text(
            "[ERROR] No se pudo actualizar el Excel de inventario. Revisa que el archivo base exista en la ruta "
            f"configurada (<code>{settings.ONEDRIVE_INVENTARIO_XLSX}</code>) y los logs del bot.",
            parse_mode=HTML,
        )
        return
    await aviso.edit_text("[OK] Inventario actualizado y sincronizado a la nube. Enviando archivo…")
    await update.message.reply_document(ruta, filename=ruta.name, caption="Inventario actualizado")


async def cmd_informe_soporte(update: Update, ctx: Ctx) -> None:
    """/informe_soporte [diario|semanal|mensual] — genera el reporte de Soporte TI y lo envía por Telegram."""
    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
    if not (es_admin_user(user, update.effective_user.id) or (user and user.rol in ROLES_INFORMES)):
        await update.message.reply_text("No tienes permiso para pedir este informe.")
        return
    periodo = (ctx.args[0].lower() if ctx.args else "mensual")
    if periodo not in ("diario", "semanal", "mensual"):
        await update.message.reply_text("Uso: /informe_soporte [diario|semanal|mensual] (por defecto: mensual)")
        return
    aviso = await update.message.reply_text(f"Generando informe ({periodo})…")
    ruta = await generar_informe_soporte_en_bg(periodo)
    if ruta is None:
        await aviso.edit_text("[ERROR] No se pudo generar el informe. Revisa los logs del bot.")
        return
    await aviso.edit_text("[OK] Informe generado. Enviando archivo…")
    await update.message.reply_document(ruta, filename=ruta.name, caption=f"Reporte de Soporte TI — {periodo}")


async def cmd_cargar_inventario(update: Update, ctx: Ctx) -> None:
    """/cargar_inventario — permite al admin cargar inventario enviando un archivo .csv o .xlsx."""
    msg = update.effective_message
    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
    if not es_admin_user(user, update.effective_user.id):
        await msg.reply_text("Solo los administradores pueden cargar archivos de inventario.")
        return

    documento = msg.document if msg else None
    if not documento:
        await msg.reply_text(
            "Envía o adjunta un archivo <b>.csv</b> o <b>.xlsx</b> con el inventario para procesar la carga.",
            parse_mode=HTML,
        )
        return

    nombre_archivo = documento.file_name or "inventario.csv"
    ext = Path(nombre_archivo).suffix.lower()
    if ext not in (".csv", ".xlsx"):
        await msg.reply_text("[ERROR] El archivo debe ser de formato .csv o .xlsx.")
        return

    aviso = await msg.reply_text("Descargando y procesando archivo de inventario…")
    ruta_temp = Path(settings.AUDIO_DIR) / f"import_{update.update_id}{ext}"
    archivo_telegram = await ctx.bot.get_file(documento.file_id)
    await archivo_telegram.download_to_drive(custom_path=ruta_temp)

    try:
        from .seed import cargar_inventario
        nuevos = await cargar_inventario(str(ruta_temp))
        await sincronizar_inventario_en_bg()
        await aviso.edit_text(f"[OK] Carga finalizada: {nuevos} equipos nuevos registrados. Inventario sincronizado en la nube.")
    except Exception as e:
        log.exception("Falló la carga de inventario por Telegram")
        await aviso.edit_text(f"[ERROR] No se pudo procesar el archivo: {e}")
    finally:
        if ruta_temp.exists():
            ruta_temp.unlink()


# ================================================================= A. reporte (privado)
async def on_privado(update: Update, ctx: Ctx) -> None:
    msg = update.effective_message
    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
        if not user:
            await msg.reply_text(f"No estás autorizado. Tu ID es {update.effective_user.id}; pásaselo al administrador.")
            return
        es_soporte = user.rol in ROLES_SOPORTE

        if msg.document:
            if es_admin_user(user, update.effective_user.id):
                await cmd_cargar_inventario(update, ctx)
            else:
                await msg.reply_text("Solo los administradores pueden enviar archivos de inventario.")
            return

        # 1) PERSISTIR primero (audio + update_id): si Groq falla, el reporte no se pierde.
        file_id = msg.voice.file_id if msg.voice else None
        audio_path = None
        if file_id:
            try:
                audio_path = await guardar_audio(ctx, file_id, f"{update.update_id}.ogg")
            except Exception:
                log.exception("No se pudo descargar el audio (se conserva el file_id)")
        inc = Incidencia(
            update_id=update.update_id, reportante_id=user.id, fechahora_reporte=msg.date,
            estado=E.RECIBIDA.value, audio_path=audio_path, audio_file_id=file_id, transcripcion=msg.text,
        )
        s.add(inc)
        try:
            await s.commit()
        except IntegrityError:  # reintento de Telegram: ya lo procesamos
            await s.rollback()
            return
        inc_id = inc.id

    # 2) Respuesta inmediata, sin pedir confirmación
    await msg.reply_text("Recibido, procesando…" if es_soporte else "Reporte recibido, Soporte TI notificado")

    # 3) IA + publicación
    try:
        await procesar_reporte(ctx, inc_id, es_soporte, msg)
    except Exception:
        log.exception("Falló el procesamiento de la incidencia %s", inc_id)


async def procesar_reporte(ctx: Ctx, inc_id: int, es_soporte: bool, msg) -> None:
    async with SessionLocal() as s:
        inc = await s.get(Incidencia, inc_id)

        texto = inc.transcripcion
        if not texto and inc.audio_path:
            try:
                texto = inc.transcripcion = await ai.transcribir(inc.audio_path)
            except Exception:
                log.exception("Whisper falló")
        ext = None
        if texto:
            try:
                ext = await ai.extraer(texto)
                inc.datos_ia = ext.model_dump_json()
            except Exception:
                log.exception("El LLM falló")

        # D. Registro directo del técnico
        if es_soporte and ext is not None and ext.intencion == "registro_directo_tecnico":
            await registrar_directo(ctx, s, inc, ext, msg)
            return
        # Un técnico cuyo mensaje no se entiende: se descarta y se le pide repetir.
        if es_soporte and (ext is None or ext.intencion == "no_entendido"):
            inc.estado = E.DESCARTADA.value
            await s.commit()
            await msg.reply_text("No pude entender el mensaje. Repítelo indicando ubicación, equipo y qué pasó.")
            return

        # Reporte de daño. Los funcionarios SIEMPRE terminan aquí: ya se les dijo "Soporte TI notificado".
        ubicacion = getattr(ext, "ubicacion", None)
        tipo = getattr(ext, "tipo_equipo", None)
        etiqueta = getattr(ext, "etiqueta", None)
        res = await resolver_activo(s, ubicacion.value if ubicacion else None, tipo.value if tipo else None, etiqueta)

        inc.estacion_id = res.estacion.id if res.estacion else None
        inc.activo_id = res.activo.id if res.activo else None
        inc.tipo_equipo = tipo.value if tipo else None
        inc.descripcion = getattr(ext, "descripcion_falla", None) or texto
        if ext is None:
            motivo = "No se pudo clasificar automáticamente; revisar el audio original"
        elif ext.intencion == "no_entendido":
            motivo = "La IA no identificó una falla; revisar el audio original"
        else:
            motivo = res.motivo
        inc.requiere_revision, inc.motivo_revision = motivo is not None, motivo
        inc.estado = E.ABIERTA.value
        await s.commit()

        await publicar_en_soporte(ctx, s, inc)


async def publicar_en_soporte(ctx: Ctx, s: AsyncSession, inc: Incidencia) -> None:
    """B. Publica el caso con botón 'Tomar Caso' y adjunta el audio original."""
    m = await ctx.bot.send_message(
        settings.SUPPORT_CHAT_ID, await texto_incidencia(s, inc), parse_mode=HTML, reply_markup=teclado_tomar(inc.id)
    )
    inc.msg_soporte_id = m.message_id
    if inc.audio_file_id:
        try:
            a = await ctx.bot.send_voice(
                settings.SUPPORT_CHAT_ID, inc.audio_file_id, reply_parameters=ReplyParameters(message_id=m.message_id)
            )
            inc.msg_audio_soporte_id = a.message_id
        except Exception:
            log.exception("No se pudo adjuntar el audio original al caso %s", inc.id)
    await s.commit()


async def registrar_directo(ctx: Ctx, s: AsyncSession, inc: Incidencia, ext, msg) -> None:
    ub, tipo = ext.ubicacion, ext.tipo_equipo
    res = await resolver_activo(s, ub.value if ub else None, tipo.value if tipo else None, ext.etiqueta)
    inc.origen = "registro_directo"
    inc.estacion_id = res.estacion.id if res.estacion else None
    inc.activo_id = res.activo.id if res.activo else None
    inc.tipo_equipo = tipo.value if tipo else None
    inc.descripcion = ext.descripcion_falla
    inc.accion_realizada = inc.diagnostico = ext.accion_realizada
    inc.tecnico_id = inc.reportante_id
    # Inicio y cierre = hora de la nota de voz. MTTR no aplica (queda NULL para no sesgar promedios).
    inc.fechahora_toma = inc.fechahora_cierre = inc.fechahora_confirmacion = inc.fechahora_reporte
    inc.mttr_segundos = None
    inc.requiere_revision, inc.motivo_revision = res.motivo is not None, res.motivo
    inc.estado = E.CERRADA.value
    await s.commit()

    aviso = f"✅ Atención presencial registrada (caso #{inc.id})."
    if res.motivo:
        aviso += f"\n⚠️ {res.motivo}"
    await msg.reply_text(aviso)
    try:
        m = await ctx.bot.send_message(settings.SUPPORT_CHAT_ID, await texto_incidencia(s, inc), parse_mode=HTML)
        inc.msg_soporte_id = m.message_id
        await s.commit()
    except Exception:
        log.exception("No se pudo notificar el registro directo %s", inc.id)

    asyncio.create_task(generar_informe_soporte_en_bg())


# ================================================================= C. cierre por reply (grupo de soporte)
async def on_reply_soporte(update: Update, ctx: Ctx) -> None:
    msg = update.effective_message
    if not msg.reply_to_message:
        return
    reply_id = msg.reply_to_message.message_id
    if not msg.voice and not (msg.text and msg.text.strip()):
        return  # reply que no es ni voz ni texto (sticker, foto, etc.)

    async with SessionLocal() as s:
        user = await get_usuario(s, update.effective_user.id)
        if not user or user.rol not in ROLES_SOPORTE:
            return
        inc = (
            await s.exec(
                select(Incidencia).where(
                    or_(Incidencia.msg_soporte_id == reply_id, Incidencia.msg_audio_soporte_id == reply_id)
                )
            )
        ).first()
        if inc is None:
            return  # reply a otro mensaje del grupo
        if inc.cierre_update_id == update.update_id:
            return  # duplicado
        if inc.estado != E.EN_ATENCION.value:
            await msg.reply_text("Este caso no está 'en atención' (¿ya cerrado o sin tomar?).")
            return
        if inc.tecnico_id != user.id:
            await msg.reply_text("Solo el técnico asignado puede cerrar este caso.")
            return

        # Persistir primero. La hora de cierre se congela con el PRIMER audio o texto (message.date):
        # si el técnico regraba/reescribe, se actualiza el diagnóstico pero NO la hora.
        audio_path = None
        if msg.voice:
            try:
                audio_path = await guardar_audio(ctx, msg.voice.file_id, f"cierre_{update.update_id}.ogg")
            except Exception:
                log.exception("No se pudo descargar el audio de cierre")
        if inc.fechahora_cierre is None:
            inc.fechahora_cierre = msg.date
        inc.cierre_update_id = update.update_id
        inc.cierre_audio_path = audio_path
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return

        texto, cierre = None, None
        if audio_path:
            try:
                texto = await ai.transcribir(audio_path)
            except Exception:
                log.exception("Whisper falló en el cierre del caso %s", inc.id)
        elif msg.text:
            texto = msg.text
        if texto:
            try:
                cierre = await ai.extraer_cierre(texto)
            except Exception:
                log.exception("El LLM falló en el cierre del caso %s", inc.id)

        if cierre:
            ub_val = cierre.ubicacion.value if cierre.ubicacion else None
            tipo_val = cierre.tipo_equipo.value if cierre.tipo_equipo else None
            etiqueta_val = cierre.etiqueta

            if ub_val or tipo_val or etiqueta_val or inc.estacion_id is None or inc.activo_id is None:
                est_actual = await s.get(Estacion, inc.estacion_id) if inc.estacion_id else None
                res = await resolver_activo(
                    s,
                    ub_val or (est_actual.nombre if est_actual else None),
                    tipo_val or inc.tipo_equipo,
                    etiqueta_val,
                )
                if res.estacion:
                    inc.estacion_id = res.estacion.id
                if res.activo:
                    inc.activo_id = res.activo.id
                    inc.tipo_equipo = res.activo.tipo_equipo
                elif tipo_val:
                    inc.tipo_equipo = tipo_val

        inc.diagnostico = cierre.diagnostico if cierre else (texto or None)
        inc.accion_realizada = cierre.accion_realizada if cierre else None
        inc.estado = E.PENDIENTE_CONFIRMACION.value
        await s.commit()

        await msg.reply_text(await texto_borrador(s, inc), parse_mode=HTML, reply_markup=teclado_borrador(inc.id))


# ================================================================= botones
async def on_callback(update: Update, ctx: Ctx) -> None:
    q = update.callback_query
    try:
        accion, *resto = q.data.split(":")
        inc_id = int(resto[0])
        arg = int(resto[1]) if len(resto) > 1 else None
    except (ValueError, IndexError):
        await q.answer()
        return

    async with SessionLocal() as s:
        user = await get_usuario(s, q.from_user.id)
        if not user or user.rol not in ROLES_SOPORTE:
            await q.answer("Solo Soporte TI puede usar estos botones.", show_alert=True)
            return
        inc = await s.get(Incidencia, inc_id)
        if inc is None:
            await q.answer("Caso no encontrado.", show_alert=True)
            return
        if accion != "tomar" and inc.tecnico_id != user.id:
            await q.answer("Solo el técnico asignado puede hacer esto.", show_alert=True)
            return
        despacho = {
            "tomar": _tomar, "cerrar": _confirmar, "regrabar": _regrabar, "volver": _volver,
            "baja": _baja, "afect": _afectado, "repl": _aplicar_recambio,
        }
        await despacho[accion](ctx, q, s, inc, user, arg)


async def _tomar(ctx, q, s, inc, user, arg) -> None:
    # Asignación atómica: solo uno gana aunque dos técnicos pulsen a la vez.
    r = await s.execute(
        sql_update(Incidencia)
        .where(Incidencia.id == inc.id, Incidencia.estado == E.ABIERTA.value)
        .values(estado=E.EN_ATENCION.value, tecnico_id=user.id, fechahora_toma=utcnow())
        .returning(Incidencia.id)
    )
    if r.scalar_one_or_none() is None:
        await s.rollback()
        await q.answer("Este caso ya fue tomado.", show_alert=True)
        return
    await s.commit()
    await s.refresh(inc)
    await q.answer("Caso asignado a ti")
    await q.edit_message_text(await texto_incidencia(s, inc), parse_mode=HTML)

    # Notificación al funcionario que reportó
    rep = await s.get(Usuario, inc.reportante_id)
    if rep and rep.chat_id:
        try:
            await ctx.bot.send_message(
                rep.chat_id,
                f"<b>[Caso #{inc.id}]</b> Su solicitud ha sido asignada al técnico <b>{h(user.nombre)}</b> y se encuentra en proceso de atención.",
                parse_mode=HTML,
            )
        except Exception as e:
            log.warning("No se pudo notificar al funcionario %s: %s", rep.chat_id, e)


async def _confirmar(ctx, q, s, inc, user, arg) -> None:
    mttr = segundos_entre(inc.fechahora_reporte, inc.fechahora_cierre)
    r = await s.execute(
        sql_update(Incidencia)
        .where(Incidencia.id == inc.id, Incidencia.estado == E.PENDIENTE_CONFIRMACION.value,
               Incidencia.tecnico_id == user.id)
        .values(estado=E.CERRADA.value, mttr_segundos=mttr, fechahora_confirmacion=utcnow())
        .returning(Incidencia.id)
    )
    if r.scalar_one_or_none() is None:
        await s.rollback()
        await q.answer("Este caso ya fue procesado.", show_alert=True)
        return
    await s.commit()
    await s.refresh(inc)
    await q.answer("Caso cerrado")
    await q.edit_message_text(
        f"<b>Caso #{inc.id} cerrado</b>\nMTTR: {fmt_duracion(mttr)}", parse_mode=HTML
    )
    await refrescar_caso(ctx, s, inc)
    asyncio.create_task(generar_informe_soporte_en_bg())


async def _regrabar(ctx, q, s, inc, user, arg) -> None:
    r = await s.execute(
        sql_update(Incidencia)
        .where(Incidencia.id == inc.id, Incidencia.estado == E.PENDIENTE_CONFIRMACION.value)
        .values(estado=E.EN_ATENCION.value)
        .returning(Incidencia.id)
    )
    if r.scalar_one_or_none() is None:
        await s.rollback()
        await q.answer("Este caso ya fue procesado.", show_alert=True)
        return
    await s.commit()
    await q.answer()
    await q.edit_message_text(
        f"Responde de nuevo al mensaje del caso #{inc.id} con voz o texto.\n"
        "<i>Se conserva la hora del primer mensaje de cierre.</i>",
        parse_mode=HTML,
    )


async def _volver(ctx, q, s, inc, user, arg) -> None:
    await q.answer()
    await q.edit_message_text(await texto_borrador(s, inc), parse_mode=HTML, reply_markup=teclado_borrador(inc.id))


async def _baja(ctx, q, s, inc, user, arg) -> None:
    if inc.estado != E.PENDIENTE_CONFIRMACION.value:
        await q.answer("Este caso ya fue procesado.", show_alert=True)
        return
    if inc.activo_id:
        await _listar_recambios(q, s, inc)
    else:
        await _listar_afectados(q, s, inc)


async def _listar_afectados(q, s, inc) -> None:
    """El caso no tiene equipo identificado: el técnico lo elige entre los operativos de la estación."""
    if not inc.estacion_id:
        await q.answer("El caso no tiene ubicación identificada. Ciérralo sin baja y regístrala manualmente.", show_alert=True)
        return
    consulta = select(Activo).where(Activo.estacion_id == inc.estacion_id, Activo.estado == A.OPERATIVO.value)
    if inc.tipo_equipo:
        consulta = consulta.where(Activo.tipo_equipo == inc.tipo_equipo)
    activos = (await s.exec(consulta.limit(12))).all()
    if not activos:
        await q.answer("No hay equipos operativos registrados para ese caso en la estación.", show_alert=True)
        return
    filas = [[Btn(desc_activo(a), callback_data=f"afect:{inc.id}:{a.id}")] for a in activos]
    filas.append([Btn("Cancelar", callback_data=f"volver:{inc.id}")])
    await q.answer()
    await q.edit_message_text(
        f"<b>Caso #{inc.id}</b> — ¿Cuál es el equipo afectado?", parse_mode=HTML, reply_markup=Kb(filas)
    )


async def _afectado(ctx, q, s, inc, user, arg) -> None:
    a = await s.get(Activo, arg) if arg else None
    if not a or a.estado != A.OPERATIVO.value or a.estacion_id != inc.estacion_id:
        await q.answer("Ese equipo no es válido para este caso.", show_alert=True)
        return
    inc.activo_id = a.id
    inc.tipo_equipo = inc.tipo_equipo or a.tipo_equipo
    await s.commit()
    await _listar_recambios(q, s, inc)


async def _listar_recambios(q, s, inc) -> None:
    afectado = await s.get(Activo, inc.activo_id)
    recambios = (
        await s.exec(
            select(Activo)
            .join(Estacion, Estacion.id == Activo.estacion_id)
            .where(
                Estacion.nombre == Ubicacion.RECAMBIO.value,
                Activo.estado == A.EN_RESERVA.value,
                Activo.tipo_equipo == afectado.tipo_equipo,
            )
            .limit(10)
        )
    ).all()
    filas = [[Btn(desc_activo(x), callback_data=f"repl:{inc.id}:{x.id}")] for x in recambios]
    filas.append([Btn("Sin recambio disponible", callback_data=f"repl:{inc.id}:0")])
    filas.append([Btn("Cancelar", callback_data=f"volver:{inc.id}")])
    await q.answer()
    await q.edit_message_text(
        f"<b>Baja / reemplazo — Caso #{inc.id}</b>\nEquipo afectado: {h(desc_activo(afectado))}\n"
        + ("Elige el equipo de Recambio que quedó instalado:" if recambios else "No hay equipos de ese tipo en Recambio."),
        parse_mode=HTML, reply_markup=Kb(filas),
    )


async def _aplicar_recambio(ctx, q, s, inc, user, arg) -> None:
    """E. Cierra el caso, marca el equipo como Pendiente de Baja, mueve el recambio y avisa a Jefatura."""
    repl_id = arg or None
    if not inc.activo_id:
        await q.answer("Falta identificar el equipo afectado.", show_alert=True)
        return
    mttr = segundos_entre(inc.fechahora_reporte, inc.fechahora_cierre)

    # Todo en una transacción: o se aplica completo o nada.
    r = await s.execute(
        sql_update(Incidencia)
        .where(Incidencia.id == inc.id, Incidencia.estado == E.PENDIENTE_CONFIRMACION.value,
               Incidencia.tecnico_id == user.id)
        .values(estado=E.CERRADA.value, mttr_segundos=mttr, fechahora_confirmacion=utcnow(),
                requiere_baja=True, activo_recambio_id=repl_id)
        .returning(Incidencia.id)
    )
    if r.scalar_one_or_none() is None:
        await s.rollback()
        await q.answer("Este caso ya fue procesado.", show_alert=True)
        return
    afectado = await s.get(Activo, inc.activo_id)
    if repl_id:
        r2 = await s.execute(
            sql_update(Activo)
            .where(Activo.id == repl_id, Activo.estado == A.EN_RESERVA.value)
            .values(estado=A.OPERATIVO.value, estacion_id=afectado.estacion_id)
            .returning(Activo.id)
        )
        if r2.scalar_one_or_none() is None:
            await s.rollback()
            await q.answer("Ese equipo de recambio ya no está disponible.", show_alert=True)
            return
    afectado.estado = A.PENDIENTE_BAJA.value
    await s.commit()
    await s.refresh(inc)
    recambio = await s.get(Activo, repl_id) if repl_id else None
    await q.answer("Baja registrada")

    enviado = await enviar_correo_baja(inc.id)  # no bloquea el cierre si falla
    excel_ok = await sincronizar_inventario_en_bg()  # ídem: un fallo aquí tampoco bloquea el cierre
    asyncio.create_task(generar_informe_soporte_en_bg())

    msg_correo = "Correo a Jefatura enviado exitosamente." if enviado else "Fallo al enviar correo (quedó en cola de reintento automático)."
    msg_excel = "Inventario en la nube actualizado." if excel_ok else "No se pudo actualizar el Excel de inventario."

    await q.edit_message_text(
        f"<b>Caso #{inc.id} cerrado con baja/reemplazo</b>\n"
        f"MTTR: {fmt_duracion(mttr)}\n"
        f"Afectado: {h(desc_activo(afectado))} -> Pendiente de baja\n"
        f"Recambio: {h(desc_activo(recambio)) if recambio else 'Sin recambio disponible'}\n"
        f"Notificación: {msg_correo}\n"
        f"Sincronización: {msg_excel}",
        parse_mode=HTML,
    )
    await refrescar_caso(ctx, s, inc)


# ================================================================= jobs / errores
async def job_actualizar_reportes(ctx: Ctx) -> None:
    """Mantiene los dos Excel al día en la nube aunque nadie los pida (además del disparo
    automático tras cada baja y de los comandos /sync_inventario y /informe_soporte)."""
    await sincronizar_inventario_en_bg()
    await generar_informe_soporte_en_bg("mensual")


async def job_reintentar_correos(ctx: Ctx) -> None:
    """Reintenta correos de baja que no salieron (SMTP caído, etc.)."""
    if not (settings.SMTP_HOST and settings.JEFE_EMAIL):
        return
    limite = utcnow() - timedelta(minutes=2)  # margen para no chocar con el envío inmediato
    async with SessionLocal() as s:
        ids = (
            await s.exec(
                select(Incidencia.id).where(
                    Incidencia.requiere_baja == True,  # noqa: E712
                    Incidencia.correo_enviado_at.is_(None),
                    Incidencia.estado == E.CERRADA.value,
                    Incidencia.fechahora_confirmacion < limite,
                )
            )
        ).all()
    for inc_id in ids:
        await enviar_correo_baja(inc_id)


async def on_error(update: object, ctx: Ctx) -> None:
    log.error("Excepción no controlada", exc_info=ctx.error)
