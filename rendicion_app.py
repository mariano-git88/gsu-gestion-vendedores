"""
rendicion_app.py — App de Streamlit para la Rendición de Cobranzas GSU.

Entry point separado del dashboard (`app.py`), del Facturador
(`facturador_app.py`), de Pedidos (`pedidos_app.py`) y de Comisiones
(`comisiones_app.py`). Se deploya en Streamlit Cloud como otro app del
mismo repo: misma codebase, URL distinta, secrets propios. Reutiliza
`api_loader.py` y `theme.py`, y delega TODA la lógica a `rendicion.py`.

FASE 1 — SIMULADOR (SOLO LECTURA). Lee la planilla de rendición de
cobranzas del vendedor, busca las facturas en Contabilium, calcula la NC
por descuento comercial (10%) y el cobro esperado, y arma un reporte
OK / REVISAR. **No crea NC, ni recibos, ni imputa nada.** El objetivo es
validar la lógica con riesgo cero antes de automatizar la escritura
(Fase 2). Ver decisión de diseño 2026-07-01.

Auth: 1 password adicional (`rendicion_password` en secrets).

Flujo:
  1. Login con password.
  2. Subir la planilla de rendición (.xlsx).
  3. Elegir rango de fechas de facturas (para acotar el índice) y tolerancia.
  4. "Analizar" → lee planilla + pagina comprobantes + calcula + verifica saldos.
  5. Reporte: KPIs, tabla OK/REVISAR, filas descartadas, descarga Excel.
"""

from __future__ import annotations

import hmac
import io
from datetime import date

import pandas as pd
import streamlit as st

import api_loader
import rendicion
import theme

# =====================================================================
# Page config + theme
# =====================================================================

st.set_page_config(
    page_title="Rendición de Cobranzas — GSU",
    page_icon="💵",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()

st.markdown(
    """
    <style>
    [data-testid="stMain"] .stButton > button,
    [data-testid="stMain"] .stDownloadButton > button,
    [data-testid="stMain"] [data-testid="stFormSubmitButton"] > button {
        background-color: #C8552F !important;
        color: #FFFFFF !important;
        border-color: #C8552F !important;
        padding: 0.2rem 0.7rem !important;
        font-size: 0.72rem !important;
        letter-spacing: 0.03em;
    }
    [data-testid="stMain"] .stButton > button:hover,
    [data-testid="stMain"] .stDownloadButton > button:hover,
    [data-testid="stMain"] [data-testid="stFormSubmitButton"] > button:hover {
        background-color: #A8451F !important;
        border-color: #A8451F !important;
        color: #FFFFFF !important;
    }
    [data-testid="stMain"] [data-testid="stMetricValue"] {
        font-size: 1.5rem !important;
        line-height: 1.1 !important;
    }
    [data-testid="stMain"] [data-testid="stMetricLabel"] {
        font-size: 0.75rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Auth gate
# =====================================================================

def _check_password() -> bool:
    """Login con `rendicion_password`. Key session: `auth_rendicion`."""
    if st.session_state.get("auth_rendicion", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Rendición de Cobranzas</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Simulador de cobranzas desde la planilla de los vendedores. "
            "Acceso restringido a administración y operaciones."
        )
        with st.form("login_rendicion", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("rendicion_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_rendicion = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Caches: API session + índice de facturas
# =====================================================================

@st.cache_resource
def _api_session():
    """Token OAuth cacheado por process. ApiSession dura ~24h."""
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=600, show_spinner="Consultando facturas en Contabilium...")
def _cargar_indice(fecha_desde_iso: str, fecha_hasta_iso: str) -> dict:
    """Índice de facturas del rango, por Nº. Cacheado 10min (paginar el
    rango es lo más caro del flujo)."""
    session = _api_session()
    session, indice = rendicion.construir_indice_facturas(
        session, fecha_desde_iso, fecha_hasta_iso
    )
    return indice


# =====================================================================
# Helpers
# =====================================================================

def _rango_default() -> tuple[date, date]:
    """Facturas a cobrar suelen ser de los últimos ~90 días. Default
    amplio para no perder facturas viejas en cuenta corriente."""
    hoy = date.today()
    desde = date(hoy.year, hoy.month, 1)
    # retroceder ~3 meses
    m = hoy.month - 3
    y = hoy.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1), hoy


def _fmt(v: float) -> str:
    return f"$ {v:,.0f}".replace(",", ".")


# =====================================================================
# Cuerpo
# =====================================================================

st.title("💵 Rendición de Cobranzas")
st.caption(
    "Fase 1 — Simulador (solo lectura). Analiza la planilla y arma el "
    "reporte OK / REVISAR. **No** genera NC, recibos ni imputaciones."
)

with st.sidebar:
    st.header("Parámetros")
    d_ini, d_fin = _rango_default()
    fecha_desde = st.date_input("Facturas desde", value=d_ini, format="DD/MM/YYYY")
    fecha_hasta = st.date_input("Facturas hasta", value=d_fin, format="DD/MM/YYYY")
    tolerancia = st.number_input(
        "Tolerancia ± ($)",
        min_value=0.0, value=rendicion.TOLERANCIA_DEFAULT, step=10.0,
        help="Diferencia máxima entre lo cobrado y lo esperado para "
             "considerar que la cobranza cuadra.",
    )
    if st.button("🔄 Refrescar facturas de Contabilium"):
        _cargar_indice.clear()
        st.rerun()

archivo = st.file_uploader(
    "Planilla de Rendición de Cobranzas (.xlsx)", type=["xlsx"]
)

if archivo is None:
    st.info(
        "Subí la planilla que arman los vendedores. La herramienta espera "
        "las columnas: Fecha, Nro. Cliente, Nro Factura, Cobro Efectivo, "
        "Cobro Cheque, Total Recibo (y opcional: Descuento, Nº Cheque)."
    )
    st.stop()

if fecha_desde > fecha_hasta:
    st.error("El rango de fechas es inválido: 'desde' es posterior a 'hasta'.")
    st.stop()

# --- Leer planilla ---
try:
    filas, descartadas = rendicion.leer_planilla(archivo.getvalue())
except Exception as e:  # noqa: BLE001 — mostrar el error al usuario, no crashear.
    st.error(f"No se pudo leer la planilla: {e}")
    st.stop()

if not filas:
    st.warning("La planilla no tiene filas con Nº de factura para procesar.")
    if descartadas:
        st.dataframe(pd.DataFrame(descartadas), use_container_width=True)
    st.stop()

st.success(f"Planilla leída: {len(filas)} fila(s) con factura.")

if not st.button("▶️ Analizar", type="primary"):
    st.stop()

# --- Analizar ---
indice = _cargar_indice(fecha_desde.isoformat(), fecha_hasta.isoformat())
resultados = rendicion.analizar(filas, indice, tolerancia=tolerancia)
with st.spinner("Verificando saldos de las facturas encontradas..."):
    session = _api_session()
    rendicion.verificar_saldos(session, resultados)

df = rendicion.resultados_a_dataframe(resultados)
n_ok = int((df["Estado"] == rendicion.ESTADO_OK).sum())
n_rev = int((df["Estado"] == rendicion.ESTADO_REVISAR).sum())

# --- KPIs ---
c1, c2, c3, c4 = st.columns(4)
c1.metric("Filas analizadas", len(df))
c2.metric("✅ OK (automatizables)", n_ok)
c3.metric("⚠️ A revisar", n_rev)
c4.metric("Descartadas (sin factura)", len(descartadas))

# --- Tabla ---
def _color_estado(fila):
    color = "#e7f6ec" if fila["Estado"] == rendicion.ESTADO_OK else "#fdecea"
    return [f"background-color: {color}"] * len(fila)

st.subheader("Reporte")
st.dataframe(
    df.style.apply(_color_estado, axis=1),
    use_container_width=True,
    hide_index=True,
)

if descartadas:
    with st.expander(f"Filas descartadas ({len(descartadas)}) — sin Nº de factura"):
        st.dataframe(pd.DataFrame(descartadas), use_container_width=True, hide_index=True)

# --- Descarga Excel ---
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xl:
    df.to_excel(xl, index=False, sheet_name="Reporte")
    if descartadas:
        pd.DataFrame(descartadas).to_excel(xl, index=False, sheet_name="Descartadas")
st.download_button(
    "⬇️ Descargar reporte (Excel)",
    data=buf.getvalue(),
    file_name=f"reporte_cobranzas_{date.today().isoformat()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.caption(
    "Recordá: esto es un simulador. Para las filas OK, la creación real de "
    "NC + recibo + imputación se hará en la Fase 2, una vez validada la lógica."
)
