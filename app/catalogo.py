"""Taxonomía oficial del inventario (sin activo fijo)."""
import re
import unicodedata
from enum import StrEnum


def norm(texto: str) -> str:
    """Minúsculas, sin tildes y con espacios colapsados (para comparar)."""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().casefold()


class Ubicacion(StrEnum):
    MODULO_1 = "Módulo 1"
    MODULO_2 = "Módulo 2"
    MODULO_3 = "Módulo 3"
    MODULO_4 = "Módulo 4"
    MODULO_5 = "Módulo 5"
    MODULO_6 = "Módulo 6"
    MODULO_7 = "Módulo 7"
    VENTANILLA = "Ventanilla"
    GESTORES_1 = "Gestores 1"
    GESTORES_2 = "Gestores 2"
    ENTREGA = "Entrega"
    OFICINA_JEFE = "Jefe Pasaporte"  # nombre real usado en el Excel de inventario de la oficina
    SALA_MULTIPLE = "Sala Multiple"
    RECAMBIO = "Recambio"  # pseudo-ubicación interna: stock de reserva/bodega (hoja aparte en el Excel)


class TipoEquipo(StrEnum):
    COMPUTADOR = "Computador"
    MONITOR = "Monitor"
    MOUSE_TECLADO = "Mouse/Teclado"
    HUELLERO = "Huellero"
    CAMARA = "Camara Fotográfica"
    PAD_FIRMA = "Pad de Firma"
    IMPRESORA = "Impresora"
    ESCANER = "Escaner"
    ESCANER_CODIGO = "Escaner Codigo de Barras"
    TV = "TV"
    MICROFONO = "Micrófono"
    HUB_USB = "Hub USB"


# Áreas donde ubicación + tipo identifican un único activo.
# SUPUESTO: los 7 módulos son monodispositivo; el resto se trata como multiequipo (requiere etiqueta/QR).
UBICACIONES_MONODISPOSITIVO = {
    Ubicacion.MODULO_1, Ubicacion.MODULO_2, Ubicacion.MODULO_3, Ubicacion.MODULO_4,
    Ubicacion.MODULO_5, Ubicacion.MODULO_6, Ubicacion.MODULO_7,
}


def a_enum(cls, valor):
    """Convierte texto libre al miembro del enum (tolerante a tildes/mayúsculas). None si no coincide."""
    if valor is None:
        return None
    if isinstance(valor, cls):
        return valor
    n = norm(valor)
    if n in ("", "null", "none"):
        return None
    for miembro in cls:
        if norm(miembro.value) == n:
            return miembro
    return None


def norm_etiqueta(valor) -> str | None:
    """'ent 01' -> 'ENT-01'."""
    if not valor:
        return None
    v = re.sub(r"[\s_]+", "-", str(valor).strip().upper())
    return None if v in ("", "NULL", "NONE") else v
