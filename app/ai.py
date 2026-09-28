"""Groq: Whisper (voz -> texto) y LLM (texto -> JSON). La IA solo interpreta lenguaje; nunca calcula."""
import logging
from pathlib import Path

from groq import AsyncGroq
from pydantic import TypeAdapter, ValidationError

from .catalogo import TipoEquipo, Ubicacion
from .config import settings
from .schemas import CierreIA, ExtraccionIA

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.GROQ_API_KEY, timeout=25.0, max_retries=1)
_adaptador_extraccion = TypeAdapter(ExtraccionIA)
_adaptador_cierre = TypeAdapter(CierreIA)

_UBICACIONES = ", ".join(f'"{u.value}"' for u in Ubicacion)
_TIPOS = ", ".join(f'"{t.value}"' for t in TipoEquipo)

# Sesga a Whisper hacia el vocabulario del dominio (mejora nombres de módulos y equipos).
PROMPT_VOZ = (
    "Oficina de pasaportes. "
    + ", ".join(u.value for u in Ubicacion)
    + ". "
    + ", ".join(t.value for t in TipoEquipo)
    + ". Etiquetas como ENT-01."
)

SYSTEM_EXTRACCION = (
    "Eres un extractor de datos para la mesa de ayuda de TI de una oficina de pasaportes. "
    "Recibes la transcripción de un mensaje (voz o texto, en español) y respondes SOLO con un objeto JSON, sin texto adicional.\n"
    "Elige el valor de 'intencion':\n"
    '- "reporte_dano": alguien reporta que un equipo falla o se dañó.\n'
    '- "registro_directo_tecnico": un técnico informa que YA atendió/resolvió algo presencialmente (ej. "reporto y cierro atención en Módulo 4...").\n'
    '- "no_entendido": el mensaje no describe ninguna falla ni atención de equipos.\n'
    "Campos por intención:\n"
    '- reporte_dano: {"intencion","ubicacion","tipo_equipo","etiqueta","descripcion_falla"}\n'
    '- registro_directo_tecnico: {"intencion","ubicacion","tipo_equipo","etiqueta","descripcion_falla","accion_realizada"}\n'
    '- no_entendido: {"intencion","motivo"}\n'
    "Reglas:\n"
    f"- 'ubicacion' debe ser EXACTAMENTE una de: {_UBICACIONES}; o null si no se menciona.\n"
    f"- 'tipo_equipo' debe ser EXACTAMENTE una de: {_TIPOS}; o null si no se menciona.\n"
    "- Equivalencias: lector de huellas/biométrico = Huellero; cámara = Camara Fotográfica; tablero o tableta de firma = Pad de Firma; "
    "lector de código de barras = Escaner Codigo de Barras; PC/CPU/torre = Computador; pantalla = Monitor; "
    "teclado o mouse/ratón = Mouse/Teclado; televisor = TV; multifuncional o impresora de pasaportes = Impresora.\n"
    "- 'etiqueta' es un código tipo ENT-01 SOLO si se dice explícitamente; si no, null.\n"
    "- 'descripcion_falla' y 'accion_realizada': resumen breve con lo dicho. No inventes datos ni hagas cálculos."
)

SYSTEM_CIERRE = (
    "Eres asistente de TI. Recibes la transcripción de la nota de voz o texto con la que un técnico cierra un caso. "
    'Responde SOLO con un objeto JSON:\n'
    '{\n'
    '  "diagnostico": "causa o falla encontrada, 1-2 frases",\n'
    '  "accion_realizada": "qué hizo para resolverlo, o null",\n'
    '  "ubicacion": "nombre de la ubicación si se menciona o aclara en el mensaje de cierre, o null",\n'
    '  "tipo_equipo": "tipo de equipo si se menciona o aclara en el mensaje de cierre, o null",\n'
    '  "etiqueta": "código como ENT-01 si se menciona explícitamente, o null"\n'
    '}.\n'
    f"Si 'ubicacion' se menciona, debe ser EXACTAMENTE una de: {_UBICACIONES}; o null.\n"
    f"Si 'tipo_equipo' se menciona, debe ser EXACTAMENTE una de: {_TIPOS}; o null.\n"
    "No inventes nada que no se haya dicho."
)


async def transcribir(ruta_audio: str) -> str:
    datos = Path(ruta_audio).read_bytes()
    r = await _client.audio.transcriptions.create(
        file=(Path(ruta_audio).name, datos),
        model=settings.GROQ_MODEL_STT,
        language="es",
        prompt=PROMPT_VOZ,
        temperature=0.0,
    )
    return (r.text or "").strip()


async def _llamar_json(system: str, usuario: str, adaptador: TypeAdapter):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": usuario}]
    ultimo_error = None
    for _ in range(2):  # un reintento si el JSON no cumple el esquema
        r = await _client.chat.completions.create(
            model=settings.GROQ_MODEL_LLM,
            messages=msgs,
            temperature=0,
            response_format={"type": "json_object"},
        )
        contenido = r.choices[0].message.content or "{}"
        try:
            return adaptador.validate_json(contenido)
        except ValidationError as e:
            ultimo_error = e
            msgs += [
                {"role": "assistant", "content": contenido},
                {"role": "user", "content": "El JSON no cumple el esquema: " + str(e)[:600] + " Corrígelo y responde solo con el JSON."},
            ]
    raise ValueError(f"El LLM devolvió JSON inválido: {ultimo_error}")


async def extraer(texto: str) -> ExtraccionIA:
    return await _llamar_json(SYSTEM_EXTRACCION, texto, _adaptador_extraccion)


async def extraer_cierre(texto: str) -> CierreIA:
    return await _llamar_json(SYSTEM_CIERRE, texto, _adaptador_cierre)
