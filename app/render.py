"""Textos (HTML de Telegram) y teclados. Todo dato de usuario/IA se escapa con html.escape."""
import html

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb

from .models import Activo, EstadoActivo, Estacion, Incidencia, Usuario
from .utils import fmt_dt, fmt_duracion, segundos_entre

ESTADOS = {
    "recibida": "[PROCESANDO]",
    "abierta": "[ABIERTA]",
    "en_atencion": "[EN ATENCIÓN]",
    "pendiente_confirmacion": "[PENDIENTE DE CONFIRMACIÓN]",
    "cerrada": "[CERRADA]",
    "descartada": "[DESCARTADA]",
}


def h(valor) -> str:
    return html.escape(str(valor)) if valor not in (None, "") else "—"


def desc_activo(a: Activo | None) -> str:
    if a is None:
        return "—"
    partes = [a.tipo_equipo]
    if a.etiqueta:
        partes.append(a.etiqueta)
    if a.serial:
        partes.append(f"S/N {a.serial}")
    return " · ".join(partes)


async def texto_incidencia(s: AsyncSession, inc: Incidencia) -> str:
    est = await s.get(Estacion, inc.estacion_id) if inc.estacion_id else None
    act = await s.get(Activo, inc.activo_id) if inc.activo_id else None
    rep = await s.get(Usuario, inc.reportante_id)
    tec = await s.get(Usuario, inc.tecnico_id) if inc.tecnico_id else None

    L = [f"<b>Caso #{inc.id}</b> — {ESTADOS.get(inc.estado, inc.estado)}"]
    if inc.origen == "registro_directo":
        L.append("<i>Atención presencial (registro directo)</i>")
    L.append(f"<b>Ubicación:</b> {h(est.nombre if est else None)}")
    if act:
        L.append(f"<b>Equipo:</b> {h(desc_activo(act))}")
    else:
        L.append(f"<b>Equipo:</b> {h(inc.tipo_equipo)} (Sin identificar)")
    L.append(f"<b>Reporta:</b> {h(rep.nombre if rep else None)}")
    L.append(f"<b>Falla:</b> {h(inc.descripcion)}")
    L.append(f"<b>Reportado:</b> {fmt_dt(inc.fechahora_reporte)}")

    if inc.requiere_revision:
        L.append(f"[Revisión requerida] <b>Motivo:</b> {h(inc.motivo_revision)}")
        if not act and est and inc.tipo_equipo:  # candidatos calculados al vuelo (no se guardan)
            cands = (
                await s.exec(
                    select(Activo).where(
                        Activo.estacion_id == est.id,
                        Activo.tipo_equipo == inc.tipo_equipo,
                        Activo.estado == EstadoActivo.OPERATIVO.value,
                    ).limit(8)
                )
            ).all()
            if cands:
                L.append("   Candidatos: " + ", ".join(h(c.etiqueta or c.serial or f"#{c.id}") for c in cands))
        if inc.transcripcion:
            L.append(f"<i>«{h(inc.transcripcion[:300])}»</i>")

    if tec:
        L.append(f"<b>Atiende:</b> {h(tec.nombre)}")
    if inc.estado == "en_atencion":
        L.append("\n<i>Para cerrar: responde a este mensaje (o a su audio) con una nota de voz o con texto.</i>")
    if inc.estado == "cerrada":
        if inc.diagnostico:
            L.append(f"<b>Diagnóstico:</b> {h(inc.diagnostico)}")
        if inc.mttr_segundos is not None:
            L.append(f"<b>MTTR:</b> {fmt_duracion(inc.mttr_segundos)}")
        if inc.requiere_baja:
            L.append("<b>Estado del equipo:</b> Pendiente de Baja" + (" (recambio instalado)" if inc.activo_recambio_id else " (sin recambio)"))
    return "\n".join(L)


async def texto_borrador(s: AsyncSession, inc: Incidencia) -> str:
    est = await s.get(Estacion, inc.estacion_id) if inc.estacion_id else None
    act = await s.get(Activo, inc.activo_id) if inc.activo_id else None
    mttr = segundos_entre(inc.fechahora_reporte, inc.fechahora_cierre)
    return (
        f"<b>Borrador de cierre — Caso #{inc.id}</b>\n"
        f"<b>Ubicación:</b> {h(est.nombre if est else None)}\n"
        f"<b>Equipo:</b> {h(desc_activo(act) if act else inc.tipo_equipo)}\n"
        f"<b>Diagnóstico:</b> {h(inc.diagnostico)}\n"
        f"<b>Acción:</b> {h(inc.accion_realizada)}\n"
        f"<b>Hora de cierre registrada:</b> {fmt_dt(inc.fechahora_cierre)}\n"
        f"<b>Tiempo de solución (MTTR):</b> {fmt_duracion(mttr)}"
    )


def teclado_tomar(inc_id: int) -> Kb:
    return Kb([[Btn("Tomar Caso", callback_data=f"tomar:{inc_id}")]])


def teclado_borrador(inc_id: int) -> Kb:
    return Kb(
        [
            [Btn("Confirmar y Cerrar", callback_data=f"cerrar:{inc_id}")],
            [Btn("Reportar Baja / Reemplazo", callback_data=f"baja:{inc_id}")],
            [Btn("Reescribir / Regrabar Cierre", callback_data=f"regrabar:{inc_id}")],
        ]
    )
