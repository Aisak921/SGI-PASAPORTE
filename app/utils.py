"""Utilidades deterministas (tiempos y formato). Nada de esto pasa por la IA."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(ZoneInfo(settings.TIMEZONE)).strftime("%d/%m/%Y %H:%M")


def fmt_duracion(segundos: int | None) -> str:
    if segundos is None:
        return "—"
    segundos = max(0, int(segundos))
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


def segundos_entre(inicio: datetime, fin: datetime) -> int:
    return max(0, int((fin - inicio).total_seconds()))
