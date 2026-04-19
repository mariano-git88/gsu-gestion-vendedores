"""
app.py — Entry point del dashboard Gestión de Vendedores GSU.

Responsabilidades:
  1. Auth gate (login con password única).
  2. Sidebar con DOS fuentes de datos disponibles:
       - Primaria: "Sincronizar desde Contabilium" (API REST, default).
       - Secundaria: "Modo Manual Secundario" (upload de 5 xlsx, fallback).
  3. Bifurcación de datos según fuente activa (session_state.fuente_activa).
  4. Correr `transforms.prepare_facturacion()` para semanal y mensual.
  5. Mostrar el panel de salud de datos (tab dedicada).
  6. Routing a las 5 vistas en tabs.

Toda la lógica de negocio vive en data_loader.py, api_loader.py,
transforms.py y metrics.py. Este archivo es orquestación + UI shell.

Coexistencia API / Manual: ver entrada 2026-04-17 en decisions.md.
"""

import io
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import api_loader
import auth
import data_loader
import exports
import theme
import transforms
import tutorial
from subrubros import RUBROS, SUBRUBROS
from vendedores import VENDEDORES
from views import analisis, cobertura, cobranzas, inventario, resumen, sub_rubro

# =====================================================================
# CONFIG
# =====================================================================

st.set_page_config(
    page_title="Gestión de Vendedores GSU",
    layout="wide",
)

# Timeout global para operaciones de sync. Ver decisión 2026-04-18
# (mensaje amigable + timeout de 10 min).
SYNC_TIMEOUT_SEC = 600

theme.apply_theme()

# =====================================================================
# AUTH GATE
# =====================================================================

if not auth.check_password():
    st.stop()

auth.logout_button()


@st.dialog("Tutorial — Cómo usar el dashboard", width="large")
def _show_tutorial_dialog():
    tutorial.render()


_col_title, _col_btn = st.columns([5, 1], vertical_alignment="center")
with _col_title:
    st.title("Dashboard de Gestión de Vendedores")
    st.caption(
        "Reunión semanal del Jefe de Ventas — GSU. "
        "Sincronizá desde Contabilium en la barra lateral para empezar."
    )
with _col_btn:
    if st.button("Tutorial", use_container_width=True, key="btn_tutorial"):
        _show_tutorial_dialog()


# =====================================================================
# SESSION STATE
# =====================================================================
# fuente_activa: None | "api" | "manual"
# Cada fuente puebla sus propios DataFrames al accionarse su flujo.
# =====================================================================

st.session_state.setdefault("fuente_activa", None)
st.session_state.setdefault("api_last_sync", None)
st.session_state.setdefault("api_rango", None)  # tupla de 6 dates (mes/sem/tri)
st.session_state.setdefault("api_errors_mes", [])
st.session_state.setdefault("api_errors_sem", [])
st.session_state.setdefault("api_errors_tri", [])
st.session_state.setdefault("api_errors_prev", [])
st.session_state.setdefault("api_errors_yoy", [])
st.session_state.setdefault("api_rango_comp", None)
# Histórico 12m — carga opt-in, separada del Sincronizar normal
st.session_state.setdefault("api_hist_last_sync", None)
st.session_state.setdefault("api_errors_hist", [])
st.session_state.setdefault("api_rango_hist", None)

# DataFrames cacheados en session (sobreviven reruns pero no logouts).
for _key in (
    "df_fc_sem_raw",
    "df_fc_mes_raw",
    "df_fc_tri_raw",
    "df_fc_prev_raw",
    "df_fc_yoy_raw",
    "df_fc_hist12_raw",
    "df_clientes",
    "df_productos",
    "df_combos",
):
    st.session_state.setdefault(_key, None)


# =====================================================================
# LOADERS — xlsx (Modo Manual Secundario)
# =====================================================================

@st.cache_data(show_spinner="Cargando facturación...")
def _load_fc_xlsx(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_fc(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando clientes...")
def _load_clientes_xlsx(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_clientes(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando productos...")
def _load_productos_xlsx(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_productos(io.BytesIO(file_bytes))


@st.cache_data(show_spinner="Cargando combos...")
def _load_combos_xlsx(file_bytes: bytes) -> pd.DataFrame:
    return data_loader.load_combos(io.BytesIO(file_bytes))


# =====================================================================
# LOADERS — API Contabilium
# =====================================================================

@st.cache_resource(show_spinner="Conectando a Contabilium...")
def _api_session():
    """Sesión compartida en el proceso.

    `obtener_token` retorna un `ApiSession` con expiración interna.
    `@st.cache_resource` no aplica TTL — el `ApiSession` auto-renueva
    cuando `api_loader.api_get` detecta que está por vencer.
    """
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=3600, show_spinner="Sincronizando clientes...")
def _api_sync_maestros():
    """Pullea clientes + conceptos (productos/combos) una sola vez por TTL.

    Retorna (df_clientes, df_productos, df_combos, clientes_items_raw).
    `clientes_items_raw` se devuelve también para pasarlo a
    `load_fc_api` y evitar re-pullear.
    """
    session = _api_session()
    session, clientes_items = api_loader._fetch_all_clientes(session)
    session, df_cli = api_loader.load_clientes_api(
        session, vendedores_map=VENDEDORES, clientes_items=clientes_items,
    )
    session, conceptos_items = api_loader._fetch_all_conceptos(session)
    session, df_prod = api_loader.load_productos_api(
        session,
        subrubros_map=SUBRUBROS,
        rubros_map=RUBROS,
        conceptos_items=conceptos_items,
    )
    session, df_comb = api_loader.load_combos_api(
        session, conceptos_items=conceptos_items,
    )
    return df_cli, df_prod, df_comb, clientes_items


@st.cache_data(ttl=3600, show_spinner="Sincronizando facturación del período...")
def _api_sync_fc(fecha_desde: str, fecha_hasta: str):
    """Pullea facturación para un rango arbitrario.

    Cache por (fecha_desde, fecha_hasta) — mismo rango reusa, rango
    distinto re-sync.

    Retorna (df, errors) donde errors es la lista de (Id, mensaje)
    de comprobantes cuyo GetById falló tras los retries.
    """
    session = _api_session()
    # Reutiliza el maestro de clientes ya cacheado (ahorra una vuelta).
    _, _, _, clientes_items = _api_sync_maestros()
    session, df, errors = api_loader.load_fc_api(
        session,
        fecha_desde,
        fecha_hasta,
        vendedores_map=VENDEDORES,
        clientes_items=clientes_items,
    )
    return df, errors


@st.cache_data(ttl=86400, show_spinner="Sincronizando facturación histórica...")
def _api_sync_fc_historico(fecha_desde: str, fecha_hasta: str):
    """Variante de `_api_sync_fc` para rangos CERRADOS (meses anteriores
    al mes en curso). Mismo payload y mismo parser que el sync normal,
    pero con TTL de 24h porque los meses cerrados no cambian — así
    pagamos el costo del N+1 una sola vez por día en lugar de cada hora.

    Usado para las comparaciones temporales (Δ vs mes anterior / YoY)
    de Sprint 2 del dashboard.

    Retorna (df, errors) — mismo formato que `_api_sync_fc`.
    """
    session = _api_session()
    _, _, _, clientes_items = _api_sync_maestros()
    session, df, errors = api_loader.load_fc_api(
        session,
        fecha_desde,
        fecha_hasta,
        vendedores_map=VENDEDORES,
        clientes_items=clientes_items,
    )
    return df, errors


# Maestro de familia: archivo estático en `assets/` (independiente de
# la fuente activa API/Manual). Se carga una vez por proceso.
_FAMILIA_PATH = Path(__file__).parent / "assets" / "sku_familia_subgrupo.xlsx"


@st.cache_resource(show_spinner="Cargando maestro de familias...")
def _load_familia_master():
    """Carga el maestro SKU → Familia desde assets/. Caso único, no cambia
    por input del usuario; `cache_resource` lo tiene en el proceso."""
    if not _FAMILIA_PATH.exists():
        # Si falta el archivo, devolvemos None y `enrich_familia` va a
        # marcar todos los SKUs como "SIN FAMILIA".
        return None
    return data_loader.load_familia(_FAMILIA_PATH)


# Cache del pipeline prepare_facturacion.
# Agregamos df_familia como input hasheable para que el cache invalide
# si en algún momento cambia el maestro (poco frecuente pero posible).
@st.cache_data(show_spinner="Procesando datos...")
def _prepare_cached(df_fc, df_cli, df_prod, df_comb, df_fam):
    return transforms.prepare_facturacion(
        df_fc, df_cli, df_prod, df_comb, df_familia=df_fam
    )


@st.cache_data(show_spinner="Generando agenda...")
def _agenda_bytes_cached(df_sem, df_mes, df_clientes, vendedor: str) -> bytes:
    return exports.exportar_agenda_vendedor(
        df_sem, df_mes, df_clientes, vendedor
    ).getvalue()


# =====================================================================
# HELPERS de fechas para el selector de mes/semana
# =====================================================================

def _opciones_meses_recientes(n: int = 12) -> list[tuple[int, int]]:
    """Lista de (year, month) de los últimos n meses, mes actual primero."""
    hoy = date.today()
    y, m = hoy.year, hoy.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def _label_mes(y: int, m: int) -> str:
    meses = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]
    return f"{meses[m-1]} {y}"


def _rango_mes(y: int, m: int, today: date | None = None) -> tuple[date, date]:
    """Primer y último día del mes (y, m). Si es el mes en curso, recorta
    fecha_hasta al día de hoy (no al último del mes)."""
    if today is None:
        today = date.today()
    inicio = date(y, m, 1)
    if (y, m) == (today.year, today.month):
        return inicio, today
    ultimo = monthrange(y, m)[1]
    return inicio, date(y, m, ultimo)


def _mes_anterior(y: int, m: int) -> tuple[int, int]:
    """(y, m) del mes anterior calendario. Ej: (2026, 4) → (2026, 3).
    Cruza año: (2026, 1) → (2025, 12)."""
    m_prev = m - 1
    y_prev = y
    if m_prev == 0:
        m_prev = 12
        y_prev = y - 1
    return y_prev, m_prev


def _mes_yoy(y: int, m: int) -> tuple[int, int]:
    """Mismo mes, un año atrás. Ej: (2026, 4) → (2025, 4)."""
    return y - 1, m


def _rango_mes_comparativo_mismo_dia(
    y: int, m: int, today: date
) -> tuple[date, date]:
    """Rango de un mes comparativo, cortado al mismo día del mes que
    `today` tiene para que la comparación sea apples-to-apples.

    Ejemplo: hoy = 2026-04-18. Comparar con marzo 2026 → rango
    (2026-03-01, 2026-03-18). Comparar con abril 2025 → rango
    (2025-04-01, 2025-04-18). Si el mes comparativo no tiene ese día
    (ej: today=2026-03-31 vs febrero), se usa el último día de ese mes.
    """
    inicio = date(y, m, 1)
    dia_today = today.day
    ultimo_mes_comp = monthrange(y, m)[1]
    dia_corte = min(dia_today, ultimo_mes_comp)
    return inicio, date(y, m, dia_corte)


def _semana_default() -> tuple[date, date]:
    """Semana por default: lunes de la semana actual hasta hoy."""
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes, hoy


# ---------------------------------------------------------------------
# Trimestre = ventana móvil de 3 meses consecutivos.
#
# Ya NO es el trimestre calendario (Q1/Q2/Q3/Q4). El usuario elige un
# "mes final" y la ventana son los 3 meses que terminan en él. Si el
# mes final es el mes en curso, el rango se recorta a los días ya
# transcurridos (ver `_rango_mes`).
#
# Ejemplo: mes final = abr 2026 → ventana = feb+mar+abr 2026. Si hoy
# es 18-abr-2026, fecha_hasta = 18-abr-2026.
# ---------------------------------------------------------------------


def _rango_trimestre(
    y_final: int, m_final: int, today: date | None = None
) -> tuple[date, date]:
    """Primer día del mes que arranca la ventana y último día del mes
    final (recortado a hoy si es el mes en curso)."""
    if today is None:
        today = date.today()
    # Retroceder 2 meses desde el mes final para obtener el mes inicial.
    y_ini, m_ini = y_final, m_final
    for _ in range(2):
        m_ini -= 1
        if m_ini == 0:
            m_ini = 12
            y_ini -= 1
    inicio = date(y_ini, m_ini, 1)
    _, fin = _rango_mes(y_final, m_final, today=today)
    return inicio, fin


def _opciones_trimestres_recientes(n: int = 12) -> list[tuple[int, int]]:
    """Lista de (year, month) recientes como candidatos a "mes final" de
    la ventana, mes actual primero."""
    return _opciones_meses_recientes(n)


def _label_trimestre(y_final: int, m_final: int) -> str:
    """Label humano de la ventana de 3 meses. Ejemplo: "Feb → Abr 2026"
    o "Nov 2025 → Ene 2026" cuando cruza año."""
    meses_abrev = [
        "Ene", "Feb", "Mar", "Abr", "May", "Jun",
        "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
    ]
    y_ini, m_ini = y_final, m_final
    for _ in range(2):
        m_ini -= 1
        if m_ini == 0:
            m_ini = 12
            y_ini -= 1
    if y_ini == y_final:
        return f"{meses_abrev[m_ini-1]} → {meses_abrev[m_final-1]} {y_final}"
    return (
        f"{meses_abrev[m_ini-1]} {y_ini} → "
        f"{meses_abrev[m_final-1]} {y_final}"
    )


# =====================================================================
# SIDEBAR — Sección primaria: API Contabilium
# =====================================================================

with st.sidebar:
    st.header("Sincronizar desde Contabilium")

    # -- Selector de mes --
    _opciones = _opciones_meses_recientes(12)
    _idx_mes = st.selectbox(
        "Mes para vista 'mensual'",
        options=range(len(_opciones)),
        format_func=lambda i: _label_mes(*_opciones[i]),
        index=0,
        key="api_mes_idx",
    )
    _hoy = date.today()
    _y_mes, _m_mes = _opciones[_idx_mes]
    _fd_mes, _fh_mes = _rango_mes(_y_mes, _m_mes, today=_hoy)

    # -- Selector de semana --
    _sem_default = _semana_default()
    _col_s1, _col_s2 = st.columns(2)
    _fd_sem = _col_s1.date_input(
        "Semana desde", value=_sem_default[0], key="api_sem_desde"
    )
    _fh_sem = _col_s2.date_input(
        "Semana hasta", value=_sem_default[1], key="api_sem_hasta"
    )

    # -- Selector de trimestre (ventana móvil de 3 meses) --
    # El usuario elige el "mes final" y la ventana son los 3 meses que
    # terminan en él. Si el mes final es el mes en curso, se recorta a
    # los días ya transcurridos.
    _opc_tri = _opciones_trimestres_recientes(12)
    _idx_tri = st.selectbox(
        "Trimestre (mes final) — para Cobertura",
        options=range(len(_opc_tri)),
        format_func=lambda i: _label_trimestre(*_opc_tri[i]),
        index=0,
        key="api_tri_idx",
    )
    _y_tri, _m_tri = _opc_tri[_idx_tri]
    _fd_tri, _fh_tri = _rango_trimestre(_y_tri, _m_tri, today=_hoy)
    st.caption(
        f"Rango: {_fd_tri.isoformat()} → {_fh_tri.isoformat()}"
    )

    if st.button(
        "Sincronizar",
        type="primary",
        use_container_width=True,
        key="btn_sync_api",
    ):
        # Umbral blando de SYNC_TIMEOUT_SEC (global, 10 min): si al
        # terminar un sub-step el total acumulado lo supera, abortamos
        # antes del próximo sub-step y mostramos el mensaje amigable.
        _t0 = time.monotonic()

        def _check_timeout():
            if time.monotonic() - _t0 > SYNC_TIMEOUT_SEC:
                raise TimeoutError(
                    f"Sync excedió {SYNC_TIMEOUT_SEC}s"
                )

        try:
            # 1) Maestros (clientes, productos, combos) — cache 1h.
            df_cli, df_prod, df_comb, _ = _api_sync_maestros()
            _check_timeout()
            # 2) Facturación mensual, semanal y trimestral — cache 1h por rango.
            df_fc_mes, errors_mes = _api_sync_fc(
                _fd_mes.isoformat(), _fh_mes.isoformat()
            )
            _check_timeout()
            df_fc_sem, errors_sem = _api_sync_fc(
                _fd_sem.isoformat(), _fh_sem.isoformat()
            )
            _check_timeout()
            df_fc_tri, errors_tri = _api_sync_fc(
                _fd_tri.isoformat(), _fh_tri.isoformat()
            )
            _check_timeout()
            # 3) Comparativos temporales (mes anterior + mismo mes año
            # pasado) — cache 24h porque son meses ya cerrados.
            # Se recortan al mismo día del mes que `_fh_mes` para que la
            # comparación sea apples-to-apples si el mes en curso está
            # a medio camino.
            _y_prev, _m_prev = _mes_anterior(_y_mes, _m_mes)
            _fd_prev, _fh_prev = _rango_mes_comparativo_mismo_dia(
                _y_prev, _m_prev, _fh_mes,
            )
            _y_yoy, _m_yoy = _mes_yoy(_y_mes, _m_mes)
            _fd_yoy, _fh_yoy = _rango_mes_comparativo_mismo_dia(
                _y_yoy, _m_yoy, _fh_mes,
            )
            df_fc_prev, errors_prev = _api_sync_fc_historico(
                _fd_prev.isoformat(), _fh_prev.isoformat()
            )
            _check_timeout()
            df_fc_yoy, errors_yoy = _api_sync_fc_historico(
                _fd_yoy.isoformat(), _fh_yoy.isoformat()
            )
            # 3) Guardar en session y activar fuente API.
            st.session_state.df_clientes = df_cli
            st.session_state.df_productos = df_prod
            st.session_state.df_combos = df_comb
            st.session_state.df_fc_mes_raw = df_fc_mes
            st.session_state.df_fc_sem_raw = df_fc_sem
            st.session_state.df_fc_tri_raw = df_fc_tri
            st.session_state.df_fc_prev_raw = df_fc_prev
            st.session_state.df_fc_yoy_raw = df_fc_yoy
            st.session_state.fuente_activa = "api"
            st.session_state.api_last_sync = datetime.now()
            st.session_state.api_rango = (
                _fd_mes, _fh_mes, _fd_sem, _fh_sem, _fd_tri, _fh_tri,
            )
            st.session_state.api_rango_comp = (
                _fd_prev, _fh_prev, _fd_yoy, _fh_yoy,
            )
            st.session_state.api_errors_mes = errors_mes
            st.session_state.api_errors_sem = errors_sem
            st.session_state.api_errors_tri = errors_tri
            st.session_state.api_errors_prev = errors_prev
            st.session_state.api_errors_yoy = errors_yoy
            _total_errors = (
                len(errors_mes) + len(errors_sem) + len(errors_tri)
            )
            if _total_errors:
                st.warning(
                    f"Sincronizado con {_total_errors} comprobante(s) omitidos "
                    f"por error de fetch. Ver detalle en tab **Salud**."
                )
            else:
                st.success(
                    f"Sincronizado: {len(df_fc_mes)} filas del mes, "
                    f"{len(df_fc_sem)} de la semana, "
                    f"{len(df_fc_tri)} del trimestre."
                )
        except api_loader.AuthError as e:
            st.error(
                f"**Credenciales rechazadas por Contabilium.** {e}\n\n"
                "Revisá `contabilium_client_id` y `contabilium_client_secret` "
                "en `.streamlit/secrets.toml` (o en Streamlit Cloud → Settings "
                "→ Secrets si estás en producción). Mientras tanto podés "
                "usar el **Modo Manual Secundario** más abajo."
            )
        except (TimeoutError, api_loader.ApiError) as e:
            # Unificamos timeout y errores de red bajo el mismo mensaje
            # amigable al usuario — ambos se resuelven igual: esperar y
            # reintentar, o usar el Modo Manual.
            st.error(
                "**Lamentablemente Contabilium está caído.** Por favor "
                "probá nuevamente más tarde o utilizá la opción de "
                "carga manual más abajo.\n\n"
                f"_Detalle técnico_: {e}"
            )

    # -- Estado del último sync + botón resync forzado --
    if st.session_state.api_last_sync is not None:
        _ts = st.session_state.api_last_sync.strftime("%Y-%m-%d %H:%M")
        st.caption(f"Último sync OK: {_ts}")
        # Botón secundario para bypassear el TTL de 1h del cache cuando
        # el usuario necesita datos frescos del momento.
        if st.button(
            "Resync forzado (bypass caché)",
            type="secondary",
            use_container_width=True,
            key="btn_resync_forzado",
            help=(
                "Limpia la caché de 1h y vuelve a pullear todo desde "
                "Contabilium. Útil si acabás de emitir una factura y "
                "querés verla reflejada."
            ),
        ):
            _api_sync_maestros.clear()
            _api_sync_fc.clear()
            _api_sync_fc_historico.clear()
            st.session_state.api_last_sync = None
            st.session_state.api_hist_last_sync = None
            st.session_state.fuente_activa = None
            st.success(
                "Caché limpiado. Tocá 'Sincronizar' arriba para traer "
                "datos frescos."
            )
            st.rerun()

    # -- Carga opt-in del histórico 12 meses --
    # Es un pull separado porque es caro (~11-18 min primera vez).
    # Con TTL de 24h, la segunda y siguientes veces del día vienen
    # del cache instantáneamente. Se usa para features que requieren
    # ventana temporal amplia (dormidos, nuevos, retención, frecuencia).
    st.divider()
    st.markdown("**Histórico 12 meses** (para dormidos / nuevos / retención)")
    _hist_ts = st.session_state.api_hist_last_sync
    if _hist_ts is not None:
        st.caption(
            f"Último pull histórico OK: {_hist_ts.strftime('%Y-%m-%d %H:%M')}"
        )
    else:
        st.caption(
            "No cargado. Requiere un pull pesado (~11-18 min la primera "
            "vez; las siguientes 24 h usan caché)."
        )
    _btn_label = (
        "Recargar histórico (12 meses)"
        if _hist_ts is not None
        else "Cargar histórico (12 meses)"
    )
    if st.button(
        _btn_label,
        type="secondary",
        use_container_width=True,
        key="btn_load_historico",
        help=(
            "Pullea los últimos 12 meses de facturación desde Contabilium. "
            "Habilita las features de clientes dormidos, nuevos, retención "
            "y frecuencia de compra. TTL de caché: 24 h."
        ),
    ):
        _t0_hist = time.monotonic()
        try:
            _fd_hist = date(_hoy.year - 1, _hoy.month, 1)
            _fh_hist = _hoy
            df_fc_hist12, errors_hist = _api_sync_fc_historico(
                _fd_hist.isoformat(), _fh_hist.isoformat()
            )
            if time.monotonic() - _t0_hist > SYNC_TIMEOUT_SEC:
                raise TimeoutError(f"Histórico excedió {SYNC_TIMEOUT_SEC}s")
            st.session_state.df_fc_hist12_raw = df_fc_hist12
            st.session_state.api_hist_last_sync = datetime.now()
            st.session_state.api_rango_hist = (_fd_hist, _fh_hist)
            st.session_state.api_errors_hist = errors_hist
            st.success(
                f"Histórico cargado: {len(df_fc_hist12):,} filas en "
                f"{_fd_hist.isoformat()} → {_fh_hist.isoformat()}. "
                f"{len(errors_hist)} comprobante(s) omitidos por fetch fallido."
            )
            st.rerun()
        except api_loader.AuthError as e:
            st.error(f"**Credenciales rechazadas**: {e}")
        except (TimeoutError, api_loader.ApiError) as e:
            st.error(
                "**Lamentablemente Contabilium está caído.** Por favor "
                "probá nuevamente más tarde o utilizá la opción de "
                "carga manual más abajo.\n\n"
                f"_Detalle técnico_: {e}"
            )

    st.divider()

    # =================================================================
    # SIDEBAR — Sección secundaria: Modo Manual
    # =================================================================
    with st.expander("Modo Manual Secundario", expanded=False):
        st.caption(
            "Fallback para cuando la API de Contabilium no esté disponible "
            "o necesites procesar archivos puntuales. Cargá las 5 planillas "
            "y tocá 'Procesar planillas'."
        )
        f_fc_sem = st.file_uploader(
            "fc_semanal.xlsx", type=["xlsx"], key="up_fc_sem"
        )
        f_fc_mes = st.file_uploader(
            "fc_mensual.xlsx", type=["xlsx"], key="up_fc_mes"
        )
        f_clientes = st.file_uploader(
            "clientes.xlsx", type=["xlsx"], key="up_cli"
        )
        f_productos = st.file_uploader(
            "productos.xlsx", type=["xlsx"], key="up_prod"
        )
        f_combos = st.file_uploader(
            "combos.xlsx", type=["xlsx"], key="up_comb"
        )
        _todos_xlsx = all(
            [f_fc_sem, f_fc_mes, f_clientes, f_productos, f_combos]
        )
        if st.button(
            "Procesar planillas",
            disabled=not _todos_xlsx,
            use_container_width=True,
            key="btn_proc_manual",
        ):
            try:
                df_fc_sem_m = _load_fc_xlsx(f_fc_sem.getvalue())
                df_fc_mes_m = _load_fc_xlsx(f_fc_mes.getvalue())
                df_cli_m = _load_clientes_xlsx(f_clientes.getvalue())
                df_prod_m = _load_productos_xlsx(f_productos.getvalue())
                df_comb_m = _load_combos_xlsx(f_combos.getvalue())
                st.session_state.df_fc_sem_raw = df_fc_sem_m
                st.session_state.df_fc_mes_raw = df_fc_mes_m
                # El modo manual no soporta trimestre ni comparativas
                # temporales (no hay xlsx de 3 meses ni de mes
                # anterior/YoY) — limpiamos residuos del modo API.
                st.session_state.df_fc_tri_raw = None
                st.session_state.df_fc_prev_raw = None
                st.session_state.df_fc_yoy_raw = None
                st.session_state.api_rango_comp = None
                st.session_state.df_clientes = df_cli_m
                st.session_state.df_productos = df_prod_m
                st.session_state.df_combos = df_comb_m
                st.session_state.fuente_activa = "manual"
                # El modo manual no tiene N+1, no hay errores de fetch.
                st.session_state.api_errors_mes = []
                st.session_state.api_errors_sem = []
                st.session_state.api_errors_tri = []
                st.success("Planillas procesadas correctamente.")
            except (
                data_loader.MissingColumnsError,
                data_loader.SheetNotFoundError,
            ) as e:
                st.error(f"**Error en una planilla:** {e}")
            except Exception as e:  # noqa: BLE001
                st.error(f"**Error inesperado al cargar planillas:** {e}")


# =====================================================================
# GATE DE DATOS — si no hay fuente activa, detener acá
# =====================================================================

if st.session_state.fuente_activa is None:
    st.info(
        "Para empezar, sincronizá desde Contabilium en la barra lateral. "
        "O usá el **Modo Manual Secundario** si tenés los 5 xlsx a mano."
    )
    st.stop()

# Referencia local más cómoda
df_fc_sem_raw = st.session_state.df_fc_sem_raw
df_fc_mes_raw = st.session_state.df_fc_mes_raw
df_clientes = st.session_state.df_clientes
df_productos = st.session_state.df_productos
df_combos = st.session_state.df_combos


# =====================================================================
# PROCESAMIENTO (pipeline transforms.prepare_facturacion)
# =====================================================================

df_familia = _load_familia_master()

try:
    df_sem, health_sem = _prepare_cached(
        df_fc_sem_raw, df_clientes, df_productos, df_combos, df_familia
    )
    df_mes, health_mes = _prepare_cached(
        df_fc_mes_raw, df_clientes, df_productos, df_combos, df_familia
    )
except Exception as e:  # noqa: BLE001
    st.error(f"**Error procesando datos:** {e}")
    st.stop()

# Trimestre: solo si fue pulleado (modo API). El modo Manual no lo soporta.
_df_fc_tri_raw = st.session_state.get("df_fc_tri_raw")
if _df_fc_tri_raw is not None and not _df_fc_tri_raw.empty:
    try:
        df_tri, health_tri = _prepare_cached(
            _df_fc_tri_raw, df_clientes, df_productos, df_combos, df_familia
        )
        st.session_state.df_tri = df_tri
        st.session_state.health_tri = health_tri
    except Exception as e:  # noqa: BLE001
        st.warning(f"No se pudo procesar el trimestre: {e}")
        st.session_state.df_tri = None
        st.session_state.health_tri = None
else:
    st.session_state.df_tri = None
    st.session_state.health_tri = None

# Comparativos temporales (mes anterior + YoY): solo modo API.
# Se procesan con el mismo pipeline que el mes actual para que tengan
# id_comprobante, clasificación, etc. listos para `metrics` y comparativas.
def _prepare_comparativo(raw):
    if raw is None or raw.empty:
        return None
    try:
        df, _health = _prepare_cached(
            raw, df_clientes, df_productos, df_combos, df_familia
        )
        return df
    except Exception as e:  # noqa: BLE001
        st.warning(f"No se pudo procesar el rango comparativo: {e}")
        return None


st.session_state.df_prev = _prepare_comparativo(
    st.session_state.get("df_fc_prev_raw")
)
st.session_state.df_yoy = _prepare_comparativo(
    st.session_state.get("df_fc_yoy_raw")
)

# Histórico 12 meses — mismo tratamiento, separado porque es opt-in.
st.session_state.df_hist12 = _prepare_comparativo(
    st.session_state.get("df_fc_hist12_raw")
)


# =====================================================================
# SIDEBAR — bloque de exportar agenda (necesita datos ya cargados)
# =====================================================================

with st.sidebar:
    st.divider()
    st.header("Exportar agenda")
    # Del dropdown excluimos:
    #   - vendedor vacío (clientes sin IdUsuarioAdicional en Contabilium)
    #   - IDs sin mapping ("ID_<n>") — clientes huérfanos de ex-vendedores
    #     o de usuarios que nunca facturaron en el rango sincronizado.
    # Ninguno de los dos representa un vendedor válido para una agenda.
    _vendedores_export = sorted(
        v
        for v in df_clientes["vendedor"].dropna().astype(str).unique().tolist()
        if v and not v.startswith("ID_")
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
# PANEL DE SALUD — helpers
# =====================================================================

def _semaforo(health: dict) -> str:
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
    return bool(health.get("vendedores_sin_cartera")) or bool(
        health.get("clientes_duplicados")
    )


# Banner discreto si hay alertas rojas.
if _has_red_alerts(health_sem) or _has_red_alerts(health_mes):
    st.error(
        "Hay alertas estructurales en los datos cargados. "
        "Revisar la pestaña **Salud** antes de presentar las cifras."
    )


# =====================================================================
# TABS DE VISTAS
# =====================================================================

(
    tab_resumen,
    tab_sub_rubro,
    tab_cobertura,
    tab_analisis,
    tab_cobranzas,
    tab_inventario,
    tab_salud,
) = st.tabs(
    [
        "Resumen", "Sub-rubro", "Cobertura", "Análisis",
        "Cobranzas", "Inventario", "Salud",
    ]
)

with tab_resumen:
    resumen.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_sub_rubro:
    sub_rubro.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_cobertura:
    cobertura.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_analisis:
    analisis.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_cobranzas:
    cobranzas.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_inventario:
    inventario.render(df_sem, df_mes, df_clientes, health_sem, health_mes)
with tab_salud:
    st.subheader("Panel de salud de datos")
    # Encabezado con fuente activa + timestamp (para trazabilidad).
    if st.session_state.fuente_activa == "api":
        _rango = st.session_state.api_rango
        if _rango and len(_rango) == 6:
            _fd_m, _fh_m, _fd_s, _fh_s, _fd_t, _fh_t = _rango
            st.caption(
                f"Fuente: **API Contabilium** · "
                f"mes {_fd_m.isoformat()} → {_fh_m.isoformat()} · "
                f"semana {_fd_s.isoformat()} → {_fh_s.isoformat()} · "
                f"trimestre {_fd_t.isoformat()} → {_fh_t.isoformat()} · "
                f"sync {st.session_state.api_last_sync:%Y-%m-%d %H:%M}"
            )
        else:
            st.caption("Fuente: **API Contabilium**")
    else:
        st.caption("Fuente: **Modo Manual Secundario** (xlsx cargados manualmente)")

    # -- Errores del N+1 de load_fc_api, si los hay --
    _errs_mes = st.session_state.get("api_errors_mes", []) or []
    _errs_sem = st.session_state.get("api_errors_sem", []) or []
    _errs_tri = st.session_state.get("api_errors_tri", []) or []
    if _errs_mes or _errs_sem or _errs_tri:
        st.warning(
            f"**Comprobantes omitidos del sync**: {len(_errs_mes)} del mes, "
            f"{len(_errs_sem)} de la semana, {len(_errs_tri)} del trimestre. "
            f"Son comprobantes cuyo detalle (GetById) falló tras los retries "
            f"automáticos. Los montos totales están subvaluados por esos "
            f"comprobantes — si la cifra es significativa, re-sincronizá "
            f"con el botón 'Resync forzado' de la sidebar."
        )
        _total_errs = len(_errs_mes) + len(_errs_sem) + len(_errs_tri)
        with st.expander(f"Ver IDs omitidos ({_total_errs})"):
            for _label, _errs in [
                ("Mes", _errs_mes),
                ("Semana", _errs_sem),
                ("Trimestre", _errs_tri),
            ]:
                if _errs:
                    st.markdown(f"**{_label}:**")
                    for _id, _msg in _errs[:50]:
                        st.text(f"  {_id}: {_msg[:200]}")
                    if len(_errs) > 50:
                        st.caption(f"(+{len(_errs) - 50} más)")

    col_sem, col_mes = st.columns(2)
    with col_sem:
        _render_health_section("Semana", health_sem)
    with col_mes:
        _render_health_section("Mes", health_mes)
