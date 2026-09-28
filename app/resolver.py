"""Resolución determinística de activos: ubicación + tipo (áreas monodispositivo) o ubicación + etiqueta (multiequipo)."""
from dataclasses import dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .models import Activo, EstadoActivo, Estacion


@dataclass
class Resolucion:
    estacion: Estacion | None = None
    activo: Activo | None = None
    motivo: str | None = None  # None = resuelto sin ambigüedad


async def resolver_activo(
    s: AsyncSession, ubicacion: str | None, tipo: str | None, etiqueta: str | None
) -> Resolucion:
    r = Resolucion()
    operativo = EstadoActivo.OPERATIVO.value

    if ubicacion:
        r.estacion = (await s.exec(select(Estacion).where(Estacion.nombre == ubicacion))).first()

    if etiqueta:
        a = (await s.exec(select(Activo).where(Activo.etiqueta == etiqueta, Activo.estado == operativo))).first()
        if a and (r.estacion is None or a.estacion_id == r.estacion.id):
            r.activo = a
            r.estacion = r.estacion or await s.get(Estacion, a.estacion_id)
            return r
        r.motivo = f"La etiqueta {etiqueta} no existe (o no está operativa) en {ubicacion or 'el inventario'}"

    if r.estacion is None:
        r.motivo = r.motivo or "No se identificó la ubicación"
        return r
    if not tipo:
        r.motivo = r.motivo or "No se identificó el tipo de equipo"
        return r

    candidatos = (
        await s.exec(
            select(Activo).where(
                Activo.estacion_id == r.estacion.id, Activo.tipo_equipo == tipo, Activo.estado == operativo
            )
        )
    ).all()
    if len(candidatos) == 1:
        r.activo, r.motivo = candidatos[0], None
    elif candidatos:
        r.motivo = f"Hay {len(candidatos)} equipos '{tipo}' en {r.estacion.nombre}; falta la etiqueta/QR"
    else:
        r.motivo = f"No hay '{tipo}' operativo registrado en {r.estacion.nombre}"
    return r
