"""
app.py — Entry point del dashboard Gestión de Vendedores GSU.

Responsabilidades:
  1. Auth gate (login con password única).
  2. Sidebar con logout y los 5 file uploaders.
  3. Cargar las 5 planillas (con cache de Streamlit).
  4. Correr `transforms.prepare_facturacion()` para semanal y mensual.
  5. Mostrar el panel de salud de datos (semáforos).
  6. Routing a las 3 vistas (Resumen / Sub-rubro / Cobertura) en tabs.

Toda la lógica de negocio vive en data_loader.py, transforms.py y
metrics.py. Este archivo es solo orquestación + UI shell.
"""

import io

import pandas as pd
import streamlit as st

import auth
import data_loader
import exports
import theme
import transforms
from views import analisis, cobertura, resumen, sub_rubro

# =====================================================================
# CONFIG
# =====================================================================

st.set_page_config(
    page_title="Gestión de Vendedores GSU",
    layout="wide",
)

# Aplicar el theme visual (Dieter Rams / Vitsoe). Tiene que correr antes
# del auth gate para que el formulario de login también herede los estilos.
theme.apply_theme()

# =====================================================================
# AUTH GATE
# =====================================================================

if not auth.check_password():
    st.stop()

auth.logout_button()

st.title("Dashboard de Gestión de Vendedores")
st.caption(
    "Reunión semanal del Jefe de Ventas — GSU. "
    "Cargá las 5 planillas en la barra lateral para empezar."
)

# =====================================================================
# CACHED LOADERS
#
# Pasamos `bytes` (no UploadedFile) porque bytes es hasheable y rápido
# de comparar; el cache de Streamlit invalida automáticamente cuando
# el contenido del archivo cambia.
# =====================================================================

@st.cache_data(show_spinner="Cargando facturación...")
def _load_fc_cached(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_fc(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando clientes...")
def _load_clientes_cached(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_clientes(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando productos...")
def _load_productos_cached(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_productos(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando combos...")
def _load_combos_cached(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_combos(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Procesando datos...")
def _prepare_cached(df_fc, df_cli, df_prod, df_comb):
    return transforms.prepare_facturacion(df_fc, df_cli, df_prod, df_comb)


@st.cache_data(show_spinner="Generando agenda...")
def _agenda_bytes_cached(df_sem, df_mes, df_clientes, vendedor: str) -> bytes:
    """
    Genera la agenda personal de un vendedor y devuelve los bytes del xlsx.
    Cacheado por (df_sem, df_mes, df_clientes, vendedor): si nada de eso
    cambia, no regenera.
    """
    return exports.exportar_agenda_vendedor(
        df_sem, df_mes, df_clientes, vendedor
    ).getvalue()


# =====================================================================
# SIDEBAR — file uploaders
# =====================================================================

with st.sidebar:
    st.header("Planillas del período")
    f_fc_sem = st.file_uploader("fc_semanal.xlsx", type=["xlsx"], key="up_fc_sem")
    f_fc_mes = st.file_uploader("fc_mensual.xlsx", type=["xlsx"], key="up_fc_mes")
    f_clientes = st.file_uploader("clientes.xlsx", type=["xlsx"], key="up_cli")
    f_productos = st.file_uploader("productos.xlsx", type=["xlsx"], key="up_prod")
    f_combos = st.file_uploader("combos.xlsx", type=["xlsx"], key="up_comb")

# Hasta tener las 5, mostrar instrucciones y cortar.
all_files = [f_fc_sem, f_fc_mes, f_clientes, f_productos, f_combos]
if not all(all_files):
    st.info("Cargá las 5 planillas en la barra lateral para empezar.")
    with st.expander("¿Qué planillas necesita el app?"):
        st.markdown(
            """
1. **fc_semanal.xlsx** — Facturación de la última semana (hoja `Comprobantes`)
2. **fc_mensual.xlsx** — Facturación del mes en curso (hoja `Comprobantes`)
3. **clientes.xlsx** — Cartera asignada a cada vendedor (hoja `Clientes`)
4. **productos.xlsx** — Maestro de productos (hoja `Productos`)
5. **combos.xlsx** — Maestro de combos (hoja `Combos`)

El detalle de columnas, reglas de validación y filtrado vive en el
manual operativo (`claude.md.txt`).
            """
        )
    st.stop()


# =====================================================================
# CARGA Y PROCESAMIENTO
# =====================================================================

try:
    df_fc_sem_raw = _load_fc_cached(f_fc_sem.getvalue())
    df_fc_mes_raw = _load_fc_cached(f_fc_mes.getvalue())
    df_clientes = _load_clientes_cached(f_clientes.getvalue())
    df_productos = _load_productos_cached(f_productos.getvalue())
    df_combos = _load_combos_cached(f_combos.getvalue())
except (data_loader.MissingColumnsError, data_loader.SheetNotFoundError) as e:
    st.error(f"**Error en una planilla:** {e}")
    st.stop()
except Exception as e:
    st.error(f"**Error inesperado al cargar planillas:** {e}")
    st.stop()

try:
    df_sem, health_sem = _prepare_cached(
        df_fc_sem_raw, df_clientes, df_productos, df_combos
    )
    df_mes, health_mes = _prepare_cached(
        df_fc_mes_raw, df_clientes, df_productos, df_combos
    )
except Exception as e:
    st.error(f"**Error procesando datos:** {e}")
    st.stop()


# =====================================================================
# SIDEBAR — bloque de exportar agenda (necesita los datos ya cargados)
# =====================================================================

with st.sidebar:
    st.header("Exportar agenda")
    _vendedores_export = sorted(
        df_clientes["vendedor"].dropna().astype(str).unique().tolist()
    )
    if _vendedores_export:
        _v_export = st.selectbox(
            "Vendedor",
            options=_vendedores_export,
            key="export_vendedor_sel",
        )
        _agenda_bytes = _agenda_bytes_cached(
            df_sem, df_mes, df_clientes, _v_export
        )
        _nombre_archivo = f"agenda_{_v_export.split('@')[0].lower()}.xlsx"
        st.download_button(
            label="Descargar agenda.xlsx",
            data=_agenda_bytes,
            file_name=_nombre_archivo,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    else:
        st.caption("Sin vendedores con cartera para exportar.")


# =====================================================================
# PANEL DE SALUD
# =====================================================================

def _semaforo(health: dict) -> str:
    """Devuelve 'verde' / 'amarillo' / 'rojo' según las alertas."""
    if health["vendedores_sin_cartera"] or health["clientes_duplicados"]:
        return "rojo"
    if (
        health["filas_no_uyu"] > 0
        or health["skus_sin_asignar"]
        or health["filas_doc_faltante"] > 0
        or health["filas_cliente_no_encontrado"] > 0
    ):
        return "amarillo"
    return "verde"


def _render_health_section(label: str, health: dict) -> None:
    color = _semaforo(health)
    color_label = {
        "verde": "VERDE — sin alertas",
        "amarillo": "AMARILLO — warnings menores",
        "rojo": "ROJO — errores estructurales",
    }[color]

    with st.expander(
        f"Salud {label} — [{color_label}] · {health['filas_finales']} filas finales",
        expanded=(color != "verde"),
    ):
        # Aviso informativo de exclusión OP (decisión 2026-04-10)
        if health.get("filas_op_excluidas", 0) > 0:
            st.info(
                f"**{health['filas_op_excluidas']} filas excluidas** por vendedor "
                f"operativo (no cuentan para ninguna métrica): "
                + ", ".join(health.get("vendedores_op_excluidos", []))
            )

        col1, col2, col3 = st.columns(3)
        col1.metric("Filas iniciales", health["filas_iniciales"])
        col1.metric("OP excluidos (filas)", health.get("filas_op_excluidas", 0))
        col1.metric("NCF descuentos descartados", health["ncf_descartadas_descuento"])
        col2.metric("Filas no UYU (excluidas)", health["filas_no_uyu"])
        col2.metric("Documento faltante", health["filas_doc_faltante"])
        col3.metric("Cliente no encontrado", health["filas_cliente_no_encontrado"])
        col3.metric("Filas finales", health["filas_finales"])

        if health["skus_sin_asignar"]:
            preview = ", ".join(health["skus_sin_asignar"][:20])
            extra = (
                f" (+{len(health['skus_sin_asignar']) - 20} más)"
                if len(health["skus_sin_asignar"]) > 20
                else ""
            )
            st.warning(
                f"**SKUs sin clasificar ({len(health['skus_sin_asignar'])})**: "
                f"{preview}{extra}"
            )
        if health["vendedores_sin_cartera"]:
            st.error(
                "**Vendedores con ventas pero sin cartera asignada**: "
                + ", ".join(health["vendedores_sin_cartera"])
            )
        if health["clientes_duplicados"]:
            st.error(
                f"**Documentos duplicados en clientes.xlsx**: "
                f"{len(health['clientes_duplicados'])} casos. Revisar el maestro."
            )
        if health["monedas_no_uyu"]:
            st.warning(
                "**Monedas distintas de UYU detectadas (excluidas del cálculo)**: "
                + ", ".join(health["monedas_no_uyu"])
            )


def _has_red_alerts(health: dict) -> bool:
    """True si el panel de salud tendría semáforo rojo (errores estructurales)."""
    return bool(health.get("vendedores_sin_cartera")) or bool(
        health.get("clientes_duplicados")
    )


# Banner discreto SOLO si hay alertas en rojo (errores estructurales).
# Las warnings amarillas viven calladitas en la pestaña Salud — no necesitan
# llamar la atención porque no cambian el significado de los datos.
if _has_red_alerts(health_sem) or _has_red_alerts(health_mes):
    st.error(
        "Hay alertas estructurales en los datos cargados. "
        "Revisar la pestaña **Salud** antes de presentar las cifras."
    )


# =====================================================================
# TABS DE VISTAS
# =====================================================================
# 5 tabs: las 3 vistas de datos diarias + Análisis profundo + Salud.
# El usuario empieza en Resumen por default; los demás están a un click.

(
    tab_resumen,
    tab_sub_rubro,
    tab_cobertura,
    tab_analisis,
    tab_salud,
) = st.tabs(["Resumen", "Sub-rubro", "Cobertura", "Análisis", "Salud"])

with tab_resumen:
    resumen.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_sub_rubro:
    sub_rubro.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_cobertura:
    cobertura.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_analisis:
    analisis.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_salud:
    st.subheader("Panel de salud de datos")
    st.caption(
        "Diagnóstico de las planillas cargadas: filas filtradas, "
        "alertas estructurales y trazabilidad de los filtros aplicados."
    )
    col_sem, col_mes = st.columns(2)
    with col_sem:
        _render_health_section("Semana", health_sem)
    with col_mes:
        _render_health_section("Mes", health_mes)
