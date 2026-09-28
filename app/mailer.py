"""Correo HTML a Jefatura por SMTP (Python estándar). Si falla, queda pendiente y un job lo reintenta."""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings
from .database import SessionLocal
from .models import Activo, Estacion, Incidencia, Usuario
from .render import desc_activo, h
from .utils import fmt_dt, fmt_duracion, segundos_entre, utcnow

log = logging.getLogger(__name__)


def _fila(etiqueta: str, valor: str) -> str:
    return (
        f'<tr><td style="padding:6px 12px;background:#f3f4f6;font-weight:bold;width:34%">{etiqueta}</td>'
        f'<td style="padding:6px 12px">{valor}</td></tr>'
    )


def _construir_html(inc: Incidencia, est, rep, tec, afectado, recambio) -> str:
    respuesta = fmt_duracion(segundos_entre(inc.fechahora_reporte, inc.fechahora_toma)) if inc.fechahora_toma else "—"
    filas = [
        ("Estación", h(est.nombre if est else None)),
        ("Funcionario que reportó", h(rep.nombre if rep else None)),
        ("Técnico asignado", h(tec.nombre if tec else None)),
        ("Dispositivo dañado", h(desc_activo(afectado))),
        ("Dispositivo de Recambio instalado", h(desc_activo(recambio)) if recambio else "Sin recambio disponible"),
        ("Diagnóstico justificativo", h(inc.diagnostico) + (f"<br>{h(inc.accion_realizada)}" if inc.accion_realizada else "")),
        ("Reportado", fmt_dt(inc.fechahora_reporte)),
        ("Tomado por el técnico", fmt_dt(inc.fechahora_toma)),
        ("Cerrado", fmt_dt(inc.fechahora_cierre)),
        ("Tiempo de respuesta", respuesta),
        ("Tiempo de solución (MTTR)", fmt_duracion(inc.mttr_segundos)),
    ]
    tabla = "".join(_fila(k, v) for k, v in filas)
    return (
        '<div style="font-family:Arial,sans-serif;max-width:640px">'
        f'<h2 style="color:#1e3a8a">Baja / reemplazo de equipo — Caso #{inc.id}</h2>'
        "<p>El equipo indicado quedó en estado <b>Pendiente de Baja</b>.</p>"
        f'<table style="border-collapse:collapse;width:100%;border:1px solid #d1d5db">{tabla}</table>'
        '<p style="color:#6b7280;font-size:12px">Mensaje automático — SGI-Pasaportes.</p></div>'
    )


def _enviar(asunto: str, cuerpo_html: str, destinatarios: list[str]) -> None:
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = ", ".join(destinatarios)
    msg.set_content("Este mensaje requiere un cliente de correo compatible con HTML.")
    msg.add_alternative(cuerpo_html, subtype="html")
    contexto = ssl.create_default_context()
    if settings.SMTP_SECURITY == "ssl":
        servidor = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=contexto, timeout=30)
    else:
        servidor = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        servidor.starttls(context=contexto)
    with servidor:
        if settings.SMTP_USER:
            servidor.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        servidor.send_message(msg)


async def enviar_correo_baja(inc_id: int) -> bool:
    """True si el correo quedó enviado (ahora o antes). Nunca lanza: un fallo de SMTP no bloquea el cierre."""
    destinatarios = [x.strip() for x in settings.JEFE_EMAIL.split(",") if x.strip()]
    if not (settings.SMTP_HOST and destinatarios):
        log.warning("SMTP/JEFE_EMAIL sin configurar; correo del caso %s queda pendiente", inc_id)
        return False

    async with SessionLocal() as s:
        inc = await s.get(Incidencia, inc_id)
        if inc is None:
            return False
        if inc.correo_enviado_at:
            return True
        est = await s.get(Estacion, inc.estacion_id) if inc.estacion_id else None
        rep = await s.get(Usuario, inc.reportante_id)
        tec = await s.get(Usuario, inc.tecnico_id) if inc.tecnico_id else None
        afectado = await s.get(Activo, inc.activo_id) if inc.activo_id else None
        recambio = await s.get(Activo, inc.activo_recambio_id) if inc.activo_recambio_id else None
        cuerpo = _construir_html(inc, est, rep, tec, afectado, recambio)

    try:
        await asyncio.to_thread(_enviar, f"[SGI-Pasaportes] Baja/reemplazo de equipo — Caso #{inc_id}", cuerpo, destinatarios)
    except Exception:
        log.exception("Falló el envío del correo del caso %s", inc_id)
        return False

    async with SessionLocal() as s:
        inc = await s.get(Incidencia, inc_id)
        inc.correo_enviado_at = utcnow()
        await s.commit()
    return True
