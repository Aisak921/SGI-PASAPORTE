"""Modelos SQLModel / PostgreSQL. Todas las fechas se guardan en UTC (timestamptz)."""
from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import BigInteger, Column, DateTime, Text
from sqlmodel import Field, SQLModel

from .utils import utcnow


def _ts(nullable: bool = True) -> Column:
    return Column(DateTime(timezone=True), nullable=nullable)


# ---------------------------------------------------------------- enums (se guardan como texto)
class Rol(StrEnum):
    FUNCIONARIO = "funcionario"
    SOPORTE = "soporte"
    JEFATURA = "jefatura"
    ADMIN = "admin"


ROLES_SOPORTE = {Rol.SOPORTE.value, Rol.ADMIN.value}  # quienes pueden tomar/cerrar casos


class EstadoIncidencia(StrEnum):
    RECIBIDA = "recibida"  # persistida, aún sin procesar por IA
    ABIERTA = "abierta"  # publicada en Soporte TI, sin técnico
    EN_ATENCION = "en_atencion"  # tomada por un técnico
    PENDIENTE_CONFIRMACION = "pendiente_confirmacion"  # borrador de cierre mostrado
    CERRADA = "cerrada"
    DESCARTADA = "descartada"  # mensaje de técnico que no se pudo interpretar


class EstadoActivo(StrEnum):
    OPERATIVO = "operativo"
    EN_RESERVA = "en_reserva"  # stock en "Recambio"
    PENDIENTE_BAJA = "pendiente_baja"
    DADO_DE_BAJA = "dado_de_baja"


# ---------------------------------------------------------------- tablas
class Usuario(SQLModel, table=True):
    """Lista blanca. `chat_id` = ID de usuario de Telegram (en chats privados coincide con el chat_id)."""

    __tablename__ = "usuarios"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column(BigInteger, unique=True, index=True, nullable=False))
    nombre: str
    rol: str = Field(default=Rol.FUNCIONARIO.value)
    activo: bool = Field(default=True)
    creado_en: datetime = Field(default_factory=utcnow, sa_column=_ts(nullable=False))


class Estacion(SQLModel, table=True):
    """Las 13 ubicaciones oficiales."""

    __tablename__ = "estaciones_trabajo"

    id: Optional[int] = Field(default=None, primary_key=True)
    nombre: str = Field(unique=True, index=True)
    es_multiequipo: bool = Field(default=False)  # True: se identifica por etiqueta/QR además del tipo


class Activo(SQLModel, table=True):
    __tablename__ = "inventario_activos"

    id: Optional[int] = Field(default=None, primary_key=True)
    estacion_id: int = Field(foreign_key="estaciones_trabajo.id", index=True)
    tipo_equipo: str = Field(index=True)
    etiqueta: Optional[str] = Field(default=None, unique=True, index=True)  # QR / etiqueta interna, ej. ENT-01
    serial: Optional[str] = None
    marca_modelo: Optional[str] = None
    estado: str = Field(default=EstadoActivo.OPERATIVO.value, index=True)
    creado_en: datetime = Field(default_factory=utcnow, sa_column=_ts(nullable=False))


class Incidencia(SQLModel, table=True):
    __tablename__ = "incidencias"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Idempotencia: un update_id de Telegram nunca crea dos filas.
    update_id: int = Field(sa_column=Column(BigInteger, unique=True, nullable=False))
    cierre_update_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, unique=True, nullable=True))

    origen: str = Field(default="reporte")  # reporte | registro_directo
    estado: str = Field(default=EstadoIncidencia.RECIBIDA.value, index=True)

    estacion_id: Optional[int] = Field(default=None, foreign_key="estaciones_trabajo.id")
    activo_id: Optional[int] = Field(default=None, foreign_key="inventario_activos.id")  # equipo afectado
    activo_recambio_id: Optional[int] = Field(default=None, foreign_key="inventario_activos.id")
    tipo_equipo: Optional[str] = None  # lo que dijo el usuario, aunque no se resuelva el activo

    reportante_id: int = Field(foreign_key="usuarios.id", index=True)
    tecnico_id: Optional[int] = Field(default=None, foreign_key="usuarios.id", index=True)

    transcripcion: Optional[str] = Field(default=None, sa_column=Column(Text))  # texto escrito o salida de Whisper
    descripcion: Optional[str] = Field(default=None, sa_column=Column(Text))
    diagnostico: Optional[str] = Field(default=None, sa_column=Column(Text))
    accion_realizada: Optional[str] = Field(default=None, sa_column=Column(Text))
    datos_ia: Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON crudo de la extracción (auditoría)

    audio_path: Optional[str] = None
    audio_file_id: Optional[str] = None
    cierre_audio_path: Optional[str] = None

    msg_soporte_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, index=True))
    msg_audio_soporte_id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, index=True))

    requiere_revision: bool = Field(default=False)
    motivo_revision: Optional[str] = None
    requiere_baja: bool = Field(default=False)

    # Marcas de tiempo (UTC). MTTR = fechahora_reporte -> fechahora_cierre (calculado en Python).
    fechahora_reporte: datetime = Field(sa_column=_ts(nullable=False))  # message.date del reporte
    fechahora_toma: Optional[datetime] = Field(default=None, sa_column=_ts())  # hora del servidor al pulsar "Tomar"
    fechahora_cierre: Optional[datetime] = Field(default=None, sa_column=_ts())  # message.date del 1er audio de cierre
    fechahora_confirmacion: Optional[datetime] = Field(default=None, sa_column=_ts())
    correo_enviado_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    mttr_segundos: Optional[int] = None
