"""Genera y actualiza los DOS libros de Excel, cada uno independiente entre sí:

1. actualizar_excel_inventario() -> reescribe 'Inventario Detallado' y 'Equipo de Recambio' del
   Excel base con los datos ACTUALES de Postgres (que es la fuente de verdad), agrega/actualiza
   la pestaña 'Dashboard_Inventario' con KPIs y gráficos, y deja 'Buscador Dinámico' y
   'Configuracion' con su estructura y fórmulas intactas.
2. generar_excel_incidencias(periodo) -> genera desde cero el libro de seguimiento de Soporte TI.
   Sin argumento (periodo='completo'): Dashboard_Total + Bitácora_Completa + una pestaña por mes.
   Con periodo='diario'|'semanal'|'mensual': archivo adicional del período específico.

"NUBE" A COSTO $0: no se usa la API de OneDrive. Ambos archivos se escriben en una ruta local
que está DENTRO de una carpeta sincronizada por el cliente de OneDrive (ver docker-compose.yml
y el README). El propio cliente de escritorio sube el archivo en cuanto detecta el cambio.

RIESGO CONOCIDO: el Excel original usa una validación de datos (el desplegable de
'Buscador Dinámico'!C5) guardada en un formato de extensión moderno que openpyxl NO puede leer
ni conservar (ver advertencia al abrir el archivo). Cada vez que este módulo guarda el libro,
ese desplegable se reconstruye con un método compatible (ver `_asegurar_dropdown_ubicacion`).
Si el archivo original tenía además colores condicionales en la columna 'Estado', ESOS si se
pierden sin aviso; este módulo agrega sus propios colores condicionales (compatibles con
openpyxl) como reemplazo — revísalo una vez en Excel de escritorio tras la primera ejecución.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from sqlmodel import select

from .catalogo import Ubicacion
from .config import settings
from .database import SessionLocal
from .models import Activo, Estacion, EstadoActivo, EstadoIncidencia, Incidencia, Usuario

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.TIMEZONE)

# ============================================================ estilo compartido
AZUL = "1E3A8A"
AZUL_CLARO = "DBEAFE"
BLANCO = "FFFFFF"
GRIS = "F3F4F6"
VERDE = "C6EFCE"
AMARILLO = "FFEB9C"
ROJO = "FFC7CE"

F_TITULO = Font(bold=True, size=16, color=AZUL)
F_HEADER = Font(bold=True, color=BLANCO)
F_KPI_NUM = Font(bold=True, size=22, color=AZUL)
F_KPI_LBL = Font(size=10, color="374151")
FILL_HEADER = PatternFill("solid", fgColor=AZUL)
FILL_KPI = PatternFill("solid", fgColor=AZUL_CLARO)


def _tabla(ws: Worksheet, fila: int, col: int, encabezados: list[str], filas: list[tuple]) -> None:
    """Escribe una tabla simple (encabezado + filas) a partir de (fila, col). Usada para los datos
    "madre" de los gráficos, y para reescribir 'Inventario Detallado' / 'Equipo de Recambio'."""
    for j, titulo in enumerate(encabezados):
        c = ws.cell(fila, col + j, titulo)
        c.font = F_HEADER
        c.fill = FILL_HEADER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, datos in enumerate(filas, start=1):
        for j, valor in enumerate(datos):
            ws.cell(fila + i, col + j, valor)


def _kpi(ws: Worksheet, celda: str, valor, etiqueta: str, ancho: int = 2, alto: int = 2) -> None:
    """Dibuja una "tarjeta" KPI simple: celda combinada con el número + etiqueta debajo."""
    col0, fila0 = ws[celda].column, ws[celda].row
    ws.merge_cells(start_row=fila0, start_column=col0, end_row=fila0 + alto - 1, end_column=col0 + ancho - 1)
    c = ws.cell(fila0, col0)
    c.value = valor
    c.font = F_KPI_NUM
    c.fill = FILL_KPI
    c.alignment = Alignment(horizontal="center", vertical="center")
    fila_lbl = fila0 + alto
    ws.merge_cells(start_row=fila_lbl, start_column=col0, end_row=fila_lbl, end_column=col0 + ancho - 1)
    lc = ws.cell(fila_lbl, col0, etiqueta)
    lc.font = F_KPI_LBL
    lc.alignment = Alignment(horizontal="center")


def _limpiar_hoja(wb, nombre: str) -> Worksheet:
    if nombre in wb.sheetnames:
        del wb[nombre]
    return wb.create_sheet(nombre)


def _rango_periodo(periodo: str, desde: datetime | None, hasta: datetime | None) -> tuple[datetime, datetime, str]:
    """Devuelve (desde_utc, hasta_utc, etiqueta) para 'diario' | 'semanal' | 'mensual', o el rango explícito dado."""
    if desde and hasta:
        return desde, hasta, f"{desde.astimezone(TZ):%d/%m/%Y} — {hasta.astimezone(TZ):%d/%m/%Y}"
    ahora_local = datetime.now(TZ)
    hoy = ahora_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if periodo == "diario":
        ini, etiqueta = hoy, f"Hoy — {hoy:%d/%m/%Y}"
    elif periodo == "semanal":
        ini, etiqueta = hoy - timedelta(days=7), f"Últimos 7 días ({hoy - timedelta(days=7):%d/%m/%Y} — {hoy:%d/%m/%Y})"
    else:  # mensual — últimos 30 días
        ini, etiqueta = hoy - timedelta(days=30), f"Últimos 30 días ({hoy - timedelta(days=30):%d/%m/%Y} — {hoy:%d/%m/%Y})"
    fin = ahora_local + timedelta(minutes=1)  # margen para incluir "ahora mismo"
    return ini.astimezone(ZoneInfo("UTC")), fin.astimezone(ZoneInfo("UTC")), etiqueta


# ============================================================ 1) INVENTARIO
# Traducciones entre el vocabulario de Postgres y el del Excel real de la oficina.
# ⚠️ Si cambias los estados/ubicaciones en app/catalogo.py o app/models.py, actualiza esto también.
MAPA_ESTADO_A_EXCEL = {
    EstadoActivo.OPERATIVO.value: "Bueno",
    EstadoActivo.EN_RESERVA.value: "En Reserva",
    EstadoActivo.PENDIENTE_BAJA.value: "Pendiente de Baja",
    EstadoActivo.DADO_DE_BAJA.value: "Dado de Baja",
}
COLOR_ESTADO_EXCEL = {
    "Bueno": VERDE,
    "Pendiente de Baja": AMARILLO,
    "Dado de Baja": ROJO,
    "En Reparación": AMARILLO,
    "Obsoleto": ROJO,
    "En Reserva": VERDE,
    "Desconocido": GRIS,
}


def _fila_inventario(a: Activo, nombre_estacion: str) -> tuple:
    return (a.serial or "", nombre_estacion, a.tipo_equipo, MAPA_ESTADO_A_EXCEL.get(a.estado, "Desconocido"), a.etiqueta or "")


def _fila_recambio(a: Activo) -> tuple:
    marca, _, modelo = (a.marca_modelo or "").partition(" ")
    return (marca, modelo or (a.marca_modelo or ""), a.tipo_equipo, "En Reserva", a.etiqueta or a.serial or "")


def _reescribir_tabla_datos(ws: Worksheet, filas: list[tuple]) -> None:
    """Sobrescribe SOLO las filas de datos (fila 2 en adelante); conserva encabezado y formato existentes."""
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)
    for i, fila in enumerate(filas, start=2):
        for j, valor in enumerate(fila, start=1):
            ws.cell(i, j, valor)


def _colorear_columna_estado(ws: Worksheet, col_letra: str, filas: int) -> None:
    """Formato condicional (compatible con openpyxl) como reemplazo del que el Excel original traía."""
    rango = f"{col_letra}2:{col_letra}{max(filas, 2)}"
    ws.conditional_formatting.add(rango, CellIsRule(operator="equal", formula=['"Bueno"'], fill=PatternFill("solid", fgColor=VERDE)))
    ws.conditional_formatting.add(rango, CellIsRule(operator="equal", formula=['"Pendiente de Baja"'], fill=PatternFill("solid", fgColor=AMARILLO)))
    ws.conditional_formatting.add(rango, CellIsRule(operator="equal", formula=['"Dado de Baja"'], fill=PatternFill("solid", fgColor=ROJO)))
    ws.conditional_formatting.add(rango, CellIsRule(operator="equal", formula=['"En Reparación"'], fill=PatternFill("solid", fgColor=AMARILLO)))
    ws.conditional_formatting.add(rango, CellIsRule(operator="equal", formula=['"Obsoleto"'], fill=PatternFill("solid", fgColor=ROJO)))


def _asegurar_dropdown_ubicacion(wb) -> None:
    if "Buscador Dinámico" not in wb.sheetnames or "Configuracion" not in wb.sheetnames:
        return
    ws = wb["Buscador Dinámico"]
    ws.data_validations.dataValidation = [dv for dv in ws.data_validations.dataValidation if dv.sqref != "C5"]
    n = wb["Configuracion"].max_row
    dv = DataValidation(type="list", formula1=f"=Configuracion!$A$2:$A${n}", allow_blank=False, showDropDown=False)
    dv.error = "Elige una ubicación de la lista."
    ws.add_data_validation(dv)
    dv.add(ws["C5"])


async def _leer_inventario_actual(s) -> tuple[list[tuple], list[tuple], dict, int]:
    """(filas_inventario_detallado, filas_recambio, conteo_por_estado, total_recambio)."""
    estaciones = {e.id: e.nombre for e in (await s.exec(select(Estacion))).all()}
    activos = (await s.exec(select(Activo))).all()
    recambio_id = next((eid for eid, n in estaciones.items() if n == Ubicacion.RECAMBIO.value), None)

    filas_inv, filas_rec, conteo = [], [], Counter()
    for a in activos:
        if a.estacion_id == recambio_id:
            filas_rec.append(_fila_recambio(a))
        else:
            filas_inv.append(_fila_inventario(a, estaciones.get(a.estacion_id, "—")))
            conteo[MAPA_ESTADO_A_EXCEL.get(a.estado, "Desconocido")] += 1
    return filas_inv, filas_rec, conteo, len(filas_rec)


def _hoja_dashboard_inventario(wb, filas_inv: list, conteo: Counter, filas_rec: list) -> None:
    ws = _limpiar_hoja(wb, "Dashboard_Inventario")
    ws.sheet_view.showGridLines = False
    ws["B2"] = "Dashboard de Inventario — Oficina de Pasaportes"
    ws["B2"].font = F_TITULO
    ws["B3"] = f"Actualizado: {datetime.now(TZ):%d/%m/%Y %H:%M}"

    total = len(filas_inv)
    bajas_totales = conteo.get("Pendiente de Baja", 0) + conteo.get("Dado de Baja", 0)
    _kpi(ws, "B5", total, "Total de equipos")
    _kpi(ws, "D5", conteo.get("Bueno", 0), "Operativos")
    _kpi(ws, "F5", bajas_totales, "Pendientes / Dados de Baja")
    _kpi(ws, "H5", len(filas_rec), "Piezas en Recambio")

    # ---- datos madre de los gráficos (fuera del área visible principal) ----
    fila_estado = [(k, v) for k, v in sorted(conteo.items())]
    _tabla(ws, 30, 2, ["Estado", "Cantidad"], fila_estado)
    conteo_rec = Counter(f[2] for f in filas_rec)  # por tipo de equipo
    fila_rec = sorted(conteo_rec.items(), key=lambda x: -x[1])[:10]
    _tabla(ws, 30, 5, ["Tipo (Recambio)", "Cantidad"], fila_rec)
    ws.cell(29, 2, "Datos de los gráficos (no editar)").font = Font(italic=True, size=9, color="9CA3AF")

    pie = PieChart()
    pie.title = "Estado global del inventario"
    pie.add_data(Reference(ws, min_col=3, min_row=30, max_row=30 + len(fila_estado)), titles_from_data=True)
    pie.set_categories(Reference(ws, min_col=2, min_row=31, max_row=30 + len(fila_estado)))
    pie.dataLabels = DataLabelList()
    pie.dataLabels.showPercent = True
    pie.height, pie.width = 8, 12
    ws.add_chart(pie, "B9")

    bar = BarChart()
    bar.type, bar.title, bar.y_axis.title = "col", "Stock en bodega (Recambio) por tipo", "Cantidad"
    bar.add_data(Reference(ws, min_col=6, min_row=30, max_row=30 + len(fila_rec)), titles_from_data=True)
    bar.set_categories(Reference(ws, min_col=5, min_row=31, max_row=30 + len(fila_rec)))
    bar.height, bar.width = 8, 16
    ws.add_chart(bar, "H9")

    for letra, ancho in {"B": 14, "C": 4, "D": 14, "E": 4, "F": 16, "G": 4, "H": 14}.items():
        ws.column_dimensions[letra].width = ancho


async def actualizar_excel_inventario(ruta: str | Path | None = None) -> Path:
    """Reescribe el inventario en el Excel con los datos actuales de Postgres."""
    ruta = Path(ruta or settings.ONEDRIVE_INVENTARIO_XLSX)
    if not ruta.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        wb_base = Workbook()
        wb_base.active.title = "Inventario Detallado"
        wb_base.create_sheet("Equipo de Recambio")
        wb_base.save(ruta)
        log.info("Creado libro base de inventario en %s", ruta)

    async with SessionLocal() as s:
        filas_inv, filas_rec, conteo, _ = await _leer_inventario_actual(s)

    return await asyncio.to_thread(_escribir_inventario_sync, ruta, filas_inv, filas_rec, conteo)


def _escribir_inventario_sync(ruta: Path, filas_inv: list, filas_rec: list, conteo: Counter) -> Path:
    wb = load_workbook(ruta)

    ws_inv = wb["Inventario Detallado"]
    _reescribir_tabla_datos(ws_inv, filas_inv)
    _colorear_columna_estado(ws_inv, "D", len(filas_inv) + 1)

    ws_rec = wb["Equipo de Recambio"]
    _reescribir_tabla_datos(ws_rec, filas_rec)
    _colorear_columna_estado(ws_rec, "D", len(filas_rec) + 1)

    _hoja_dashboard_inventario(wb, filas_inv, conteo, filas_rec)
    _asegurar_dropdown_ubicacion(wb)

    orden = ["Dashboard_Inventario", "Buscador Dinámico", "Inventario Detallado", "Configuracion", "Equipo de Recambio"]
    wb._sheets.sort(key=lambda w: orden.index(w.title) if w.title in orden else 99)
    wb.active = 0

    wb.save(ruta)
    log.info("Inventario actualizado en %s (%d equipos, %d en recambio)", ruta, len(filas_inv), len(filas_rec))
    return ruta


# ============================================================ 2) SOPORTE TI (libro independiente)
ENCABEZADOS_BITACORA = [
    "ID", "Estado", "Origen", "Módulo/Ubicación", "Equipo", "Funcionario", "Técnico",
    "Síntoma", "Diagnóstico", "Fecha Reporte", "Fecha Cierre", "MTTR (min)", "Baja/Reemplazo",
]

# Colores de fila según estado del caso
_COLOR_ESTADO_FILA = {
    "cerrada": VERDE,
    "en_atencion": AZUL_CLARO,
    "abierta": AMARILLO,
    "pendiente_confirmacion": AMARILLO,
}


async def _casos_del_periodo(s, desde: datetime, hasta: datetime) -> list[Incidencia]:
    r = await s.exec(
        select(Incidencia)
        .where(Incidencia.fechahora_reporte >= desde, Incidencia.fechahora_reporte < hasta)
        .where(Incidencia.estado != EstadoIncidencia.DESCARTADA.value)
        .order_by(Incidencia.fechahora_reporte)
    )
    return r.all()


async def _fila_bitacora(s, inc: Incidencia) -> tuple:
    est = await s.get(Estacion, inc.estacion_id) if inc.estacion_id else None
    act = await s.get(Activo, inc.activo_id) if inc.activo_id else None
    rep = await s.get(Usuario, inc.reportante_id)
    tec = await s.get(Usuario, inc.tecnico_id) if inc.tecnico_id else None
    equipo = act.tipo_equipo if act else (inc.tipo_equipo or "—")
    return (
        inc.id, inc.estado, "Registro directo" if inc.origen == "registro_directo" else "Reporte",
        est.nombre if est else "—", equipo, rep.nombre if rep else "—", tec.nombre if tec else "—",
        inc.descripcion or "", inc.diagnostico or "",
        inc.fechahora_reporte.astimezone(TZ).replace(tzinfo=None) if inc.fechahora_reporte else None,
        inc.fechahora_cierre.astimezone(TZ).replace(tzinfo=None) if inc.fechahora_cierre else None,
        round(inc.mttr_segundos / 60, 1) if inc.mttr_segundos is not None else None,
        "Sí" if inc.requiere_baja else "No",
    )


def _hoja_bitacora_sync(wb, nombre_hoja: str, filas: list[tuple]) -> None:
    """Crea (o reemplaza) una hoja de bitácora con las filas dadas, coloreadas por estado."""
    ws = _limpiar_hoja(wb, nombre_hoja)
    _tabla(ws, 1, 1, ENCABEZADOS_BITACORA, filas)
    ws.freeze_panes = "A2"
    anchos = {"A": 6, "B": 16, "C": 16, "D": 16, "E": 20, "F": 18, "G": 18,
              "H": 36, "I": 36, "J": 17, "K": 17, "L": 12, "M": 14}
    for letra, ancho in anchos.items():
        ws.column_dimensions[letra].width = ancho
    for col in ("J", "K"):
        for c in ws[col][1:]:
            if c.value:
                c.number_format = "dd/mm/yyyy hh:mm"
    # Colorear filas según estado
    for i, fila in enumerate(filas, start=2):
        color = _COLOR_ESTADO_FILA.get(fila[1])
        if color:
            for col_idx in range(1, len(ENCABEZADOS_BITACORA) + 1):
                ws.cell(i, col_idx).fill = PatternFill("solid", fgColor=color)


def _hoja_dashboard_soporte(wb, nombre_hoja: str, filas: list[tuple], etiqueta_periodo: str) -> None:
    """Dashboard de KPIs + gráficos para el conjunto de filas dado."""
    ws = _limpiar_hoja(wb, nombre_hoja)
    ws.sheet_view.showGridLines = False
    ws["B2"] = "Dashboard de Soporte TI — Oficina de Pasaportes"
    ws["B2"].font = F_TITULO
    ws["B3"] = f"Período: {etiqueta_periodo}"

    cerrados = [f for f in filas if f[1] == "cerrada"]
    mttrs = [f[11] for f in cerrados if f[11] is not None]
    bajas = sum(1 for f in filas if f[12] == "Sí")
    _kpi(ws, "B5", len(filas), "Total de casos")
    _kpi(ws, "D5", round(sum(mttrs) / len(mttrs), 1) if mttrs else "—", "MTTR promedio (min)")
    _kpi(ws, "F5", bajas, "Bajas / reemplazos")
    _kpi(ws, "H5", sum(1 for f in filas if f[1] in ("abierta", "en_atencion", "pendiente_confirmacion")), "Casos abiertos")

    top_equipos = Counter(f[4] for f in filas).most_common(8)
    _tabla(ws, 30, 2, ["Equipo", "Casos"], top_equipos)
    por_dia = Counter((f[9].date() if isinstance(f[9], datetime) else f[9]) for f in filas if f[9])
    serie = sorted(por_dia.items())
    _tabla(ws, 30, 5, ["Fecha", "Casos"], [(d.strftime("%d/%m"), n) for d, n in serie])
    ws.cell(29, 2, "Datos de los gráficos (no editar)").font = Font(italic=True, size=9, color="9CA3AF")

    if top_equipos:
        bar = BarChart()
        bar.type, bar.title, bar.y_axis.title = "bar", "Equipos con más fallas", "Casos"
        bar.add_data(Reference(ws, min_col=3, min_row=30, max_row=30 + len(top_equipos)), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=2, min_row=31, max_row=30 + len(top_equipos)))
        bar.height, bar.width = 8, 14
        ws.add_chart(bar, "B9")

    if serie:
        line = LineChart()
        line.title, line.y_axis.title = "Evolución de incidencias", "Casos"
        line.add_data(Reference(ws, min_col=6, min_row=30, max_row=30 + len(serie)), titles_from_data=True)
        line.set_categories(Reference(ws, min_col=5, min_row=31, max_row=30 + len(serie)))
        line.height, line.width = 8, 16
        ws.add_chart(line, "H9")

    for letra, ancho in {"B": 16, "C": 8, "D": 16, "E": 10, "F": 18, "G": 4, "H": 14}.items():
        ws.column_dimensions[letra].width = ancho


async def generar_excel_incidencias(
    periodo: str = "completo", *, desde: datetime | None = None, hasta: datetime | None = None,
    ruta: str | Path | None = None
) -> Path:
    """Genera el reporte de Soporte TI.

    - periodo='completo' (default): un archivo con TODO el historial + una pestaña por mes.
    - periodo='diario'|'semanal'|'mensual': archivo adicional del período específico.
    """
    ruta = Path(ruta or settings.ONEDRIVE_SOPORTE_XLSX)

    async with SessionLocal() as s:
        if periodo == "completo":
            # Traer TODOS los casos sin filtro de fecha
            todos = (await s.exec(
                select(Incidencia)
                .where(Incidencia.estado != EstadoIncidencia.DESCARTADA.value)
                .order_by(Incidencia.fechahora_reporte)
            )).all()
            filas_todas = [await _fila_bitacora(s, inc) for inc in todos]
            # Agrupar por mes local (año, mes)
            meses: dict[tuple[int, int], list[tuple]] = {}
            for fila, inc in zip(filas_todas, todos):
                if inc.fechahora_reporte:
                    dt_local = inc.fechahora_reporte.astimezone(TZ)
                    clave: tuple[int, int] = (dt_local.year, dt_local.month)
                else:
                    clave = (0, 0)
                meses.setdefault(clave, []).append(fila)
            return await asyncio.to_thread(_escribir_soporte_completo_sync, ruta, filas_todas, meses)
        else:
            desde_dt, hasta_dt, etiqueta = _rango_periodo(periodo, desde, hasta)
            casos = await _casos_del_periodo(s, desde_dt, hasta_dt)
            filas = [await _fila_bitacora(s, inc) for inc in casos]
            ruta_periodo = ruta.parent / f"Reporte_Gestion_Soporte_TI_{periodo}.xlsx"
            return await asyncio.to_thread(_escribir_soporte_periodo_sync, ruta_periodo, filas, etiqueta)


def _escribir_soporte_completo_sync(
    ruta: Path, filas_todas: list[tuple], meses: dict[tuple[int, int], list[tuple]]
) -> Path:
    """Archivo completo: Dashboard_Total + Bitácora_Completa + una pestaña por mes."""
    MESES_ES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
                "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

    wb = Workbook()
    del wb["Sheet"]

    # 1. Dashboard total con todo el historial
    _hoja_dashboard_soporte(wb, "Dashboard_Total", filas_todas, "Historial completo")

    # 2. Bitácora con todos los casos
    _hoja_bitacora_sync(wb, "Bitácora_Completa", filas_todas)

    # 3. Una pestaña por mes, ordenada cronológicamente
    for (anio, mes) in sorted(meses.keys()):
        nombre = "Sin fecha" if anio == 0 else f"{MESES_ES[mes]} {anio}"
        _hoja_bitacora_sync(wb, nombre, meses[(anio, mes)])

    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta)
    log.info("Reporte completo generado en %s (%d casos, %d meses)", ruta, len(filas_todas), len(meses))
    return ruta


def _escribir_soporte_periodo_sync(ruta: Path, filas: list[tuple], etiqueta: str) -> Path:
    """Archivo de período específico (diario/semanal/mensual): Dashboard + Bitácora del período."""
    wb = Workbook()
    del wb["Sheet"]
    _hoja_dashboard_soporte(wb, "Dashboard_Soporte", filas, etiqueta)
    _hoja_bitacora_sync(wb, "Bitácora_Detallada", filas)
    wb.active = 0
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta)
    log.info("Reporte de soporte TI (%s) generado en %s (%d casos)", etiqueta, ruta, len(filas))
    return ruta


from .cloud_sync import subir_archivo_a_onedrive


# ============================================================ helpers de conveniencia para el bot
async def sincronizar_inventario_en_bg() -> Path | None:
    """Para usar tras cualquier cambio de inventario (ej. una baja/reemplazo). No lanza: un fallo
    aquí no debe tumbar el flujo de Telegram que la llama."""
    try:
        ruta = await actualizar_excel_inventario()
        await subir_archivo_a_onedrive(ruta)
        return ruta
    except Exception:
        log.exception("No se pudo sincronizar el Excel de inventario")
        return None


async def generar_informe_soporte_en_bg(periodo: str = "completo") -> Path | None:
    try:
        ruta = await generar_excel_incidencias(periodo)
        await subir_archivo_a_onedrive(ruta)
        return ruta
    except Exception:
        log.exception("No se pudo generar el informe de soporte TI")
        return None
