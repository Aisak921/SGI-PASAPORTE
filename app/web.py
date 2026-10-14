"""Servidor web FastAPI para el portal de reporte de incidencias.

Corre como un servicio Docker independiente (mismo código, distinto comando).
Los funcionarios acceden desde cualquier navegador sin instalar nada.
El portal notifica al grupo de Soporte TI en Telegram exactamente igual que el bot.
"""
import hashlib
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import select

from .catalogo import FALLAS_GENERALES, FALLAS_POR_TIPO, TipoEquipo
from .config import settings
from .database import SessionLocal, init_db
from .models import EstadoIncidencia, Estacion, Incidencia, Rol, Usuario
from .render import texto_incidencia
from .resolver import resolver_activo
from .utils import utcnow

log = logging.getLogger("sgi.web")

app = FastAPI(title="SGI Pasaportes — Portal Web", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos (CSS, JS, íconos)
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ──────────────────────────────────────── startup
@app.on_event("startup")
async def startup():
    await init_db()
    log.info("Portal web listo en http://0.0.0.0:8080")


# ──────────────────────────────────────── página principal
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _STATIC / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ──────────────────────────────────────── API: catálogo
@app.get("/api/estaciones")
async def get_estaciones():
    async with SessionLocal() as s:
        ests = (await s.exec(select(Estacion).order_by(Estacion.nombre))).all()
    return [{"id": e.id, "nombre": e.nombre} for e in ests]


@app.get("/api/tipos")
async def get_tipos():
    return [t.value for t in TipoEquipo]


@app.get("/api/fallas")
async def get_fallas(tipo: str = ""):
    return FALLAS_POR_TIPO.get(tipo, FALLAS_GENERALES)


# ──────────────────────────────────────── API: reporte
class ReporteBody(BaseModel):
    nombre: str
    estacion_id: int
    tipo_equipo: str
    descripcion: str  # falla seleccionada (o texto libre)


@app.post("/api/reportar")
async def crear_reporte(body: ReporteBody):
    """Crea la incidencia en BD y notifica al grupo de Soporte TI en Telegram."""
    nombre = body.nombre.strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "Nombre requerido"}, status_code=400)

    async with SessionLocal() as s:
        # Buscar o crear usuario web (chat_id negativo = convenio para usuarios del portal)
        web_chat_id = _web_chat_id(nombre)
        usuario = (await s.exec(
            select(Usuario).where(Usuario.chat_id == web_chat_id)
        )).first()
        if not usuario:
            usuario = Usuario(chat_id=web_chat_id, nombre=nombre, rol=Rol.FUNCIONARIO.value)
            s.add(usuario)
            await s.flush()  # obtener usuario.id sin commitear

        est = await s.get(Estacion, body.estacion_id)
        if not est:
            return JSONResponse({"ok": False, "error": "Ubicación no válida"}, status_code=400)

        res = await resolver_activo(s, est.nombre, body.tipo_equipo, None)

        # update_id único para reportes web: usamos hash negativo del timestamp + nombre
        update_id = _web_update_id(nombre)

        inc = Incidencia(
            update_id=update_id,
            reportante_id=usuario.id,
            fechahora_reporte=utcnow(),
            estado=EstadoIncidencia.ABIERTA.value,
            origen="web",
            estacion_id=body.estacion_id,
            activo_id=res.activo.id if res and res.activo else None,
            tipo_equipo=body.tipo_equipo,
            descripcion=body.descripcion,
            requiere_revision=res.motivo is not None if res else False,
            motivo_revision=res.motivo if res else None,
        )
        s.add(inc)
        await s.commit()
        await s.refresh(inc)
        await s.refresh(usuario)

        # Generar texto del caso (igual al bot de Telegram)
        texto = await texto_incidencia(s, inc)

    # Notificar al grupo de Soporte TI en Telegram
    msg_id = await _notify_telegram(inc.id, texto)
    if msg_id:
        async with SessionLocal() as s:
            inc2 = await s.get(Incidencia, inc.id)
            if inc2:
                inc2.msg_soporte_id = msg_id
                await s.commit()

    return {"ok": True, "caso_id": inc.id}


# ──────────────────────────────────────── API: audio (transcripción Groq)
@app.post("/api/audio")
async def transcribir_audio(audio: UploadFile = File(...)):
    """Recibe un audio grabado desde el teléfono y devuelve la transcripción."""
    try:
        from . import ai
        audio_dir = Path(settings.AUDIO_DIR)
        audio_dir.mkdir(parents=True, exist_ok=True)
        dest = audio_dir / f"web_{utcnow().timestamp():.0f}.webm"
        dest.write_bytes(await audio.read())
        texto = await ai.transcribir(str(dest))
        return {"ok": True, "texto": texto}
    except Exception as e:
        log.exception("Error transcribiendo audio web")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ──────────────────────────────────────── helpers internos
def _web_chat_id(nombre: str) -> int:
    """ID negativo único y estable para usuarios del portal web (nunca colisiona con Telegram IDs positivos)."""
    h = int(hashlib.sha256(nombre.lower().strip().encode()).hexdigest(), 16)
    return -(h % (10 ** 15))  # negativo, máx 15 dígitos


def _web_update_id(nombre: str) -> int:
    """update_id único para idempotencia (combinación de timestamp + nombre)."""
    import time
    h = int(hashlib.sha256(f"{nombre}{time.time_ns()}".encode()).hexdigest(), 16)
    return h % (2 ** 62)  # positivo, cabe en BigInteger


async def _notify_telegram(inc_id: int, texto: str) -> int | None:
    """Envía el caso al grupo de Soporte TI con el botón 'Tomar Caso'."""
    keyboard = {
        "inline_keyboard": [[{
            "text": "✋ Tomar Caso",
            "callback_data": f"tomar:{inc_id}"
        }]]
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": settings.SUPPORT_CHAT_ID,
                    "text": texto,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            log.error("Telegram rechazó la notificación: %s", data)
    except Exception:
        log.exception("No se pudo notificar al grupo de Telegram")
    return None
