"""Esquemas Pydantic para la salida de la IA. Nada se persiste sin pasar por aquí."""
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .catalogo import TipoEquipo, Ubicacion, a_enum, norm_etiqueta


class _CamposEquipo(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    ubicacion: Optional[Ubicacion] = None
    tipo_equipo: Optional[TipoEquipo] = None
    etiqueta: Optional[str] = None
    descripcion_falla: str = Field(min_length=3, max_length=1000)

    # Tolerantes: si la IA devuelve algo fuera del catálogo se trata como "no mencionado" (None)
    # y el caso se marca para revisión, en vez de perder el reporte.
    @field_validator("ubicacion", mode="before")
    @classmethod
    def _ubicacion(cls, v):
        return a_enum(Ubicacion, v)

    @field_validator("tipo_equipo", mode="before")
    @classmethod
    def _tipo(cls, v):
        return a_enum(TipoEquipo, v)

    @field_validator("etiqueta", mode="before")
    @classmethod
    def _etiqueta(cls, v):
        return norm_etiqueta(v)


class ReporteDanoIA(_CamposEquipo):
    intencion: Literal["reporte_dano"]


class RegistroDirectoIA(_CamposEquipo):
    intencion: Literal["registro_directo_tecnico"]
    accion_realizada: str = Field(min_length=3, max_length=1000)


class NoEntendidoIA(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intencion: Literal["no_entendido"]
    motivo: Optional[str] = None


# Una sola llamada al LLM: la unión discriminada por `intencion` decide el esquema.
ExtraccionIA = Annotated[
    Union[ReporteDanoIA, RegistroDirectoIA, NoEntendidoIA],
    Field(discriminator="intencion"),
]


class CierreIA(BaseModel):
    """Lo que dice el técnico en la nota de voz o texto de cierre."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    diagnostico: str = Field(min_length=3, max_length=1500)
    accion_realizada: Optional[str] = Field(default=None, max_length=1500)
    ubicacion: Optional[Ubicacion] = None
    tipo_equipo: Optional[TipoEquipo] = None
    etiqueta: Optional[str] = None

    @field_validator("ubicacion", mode="before")
    @classmethod
    def _ubicacion(cls, v):
        return a_enum(Ubicacion, v)

    @field_validator("tipo_equipo", mode="before")
    @classmethod
    def _tipo(cls, v):
        return a_enum(TipoEquipo, v)

    @field_validator("etiqueta", mode="before")
    @classmethod
    def _etiqueta(cls, v):
        return norm_etiqueta(v)
