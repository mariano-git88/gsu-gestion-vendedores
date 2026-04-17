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
from calendar import monthrange
from datetime import date, datetime, timedelta

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
from views import analisis, cobertura, resumen, sub_rubro

# =====================================================================
# CONFIG
# =====================================================================

st.set_page_config(
    page_title="Gestión de Vendedores GSU",
    layout="wide",
)

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
st.session_state.setdefault("api_rango", None)  # (fecha_desde_mes, ..., hasta_sem)
st.session_state.setdefault("api_errors_mes", [])
st.session_state.setdefault("api_errors_sem", [])

# DataFrames cacheados en session (sobreviven reruns pero no logouts).
for _key in (
    "df_fc_sem_raw",
    "df_fc_mes_raw",
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


# Cache del pipeline prepare_facturacion (sigue como estaba).
@st.cache_data(show_spinner="Procesando datos...")
def _prepare_cached(df_fc, df_cli, df_prod, df_comb):
    return transforms.prepare_facturacion(df_fc, df_cli, df_prod, df_comb)


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


def _rango_mes(y: int, m: int) -> tuple[date, date]:
    ultimo = monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, ultimo)


def _semana_default() -> tuple[date, date]:
    """Semana por default: lunes de la semana actual hasta hoy."""
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes, hoy


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
    _y_mes, _m_mes = _opciones[_idx_mes]
    _fd_mes, _fh_mes = _rango_mes(_y_mes, _m_mes)

    # -- Selector de semana --
    _sem_default = _semana_default()
    _col_s1, _col_s2 = st.columns(2)
    _fd_sem = _col_s1.date_input(
        "Semana desde", value=_sem_default[0], key="api_sem_desde"
    )
    _fh_sem = _col_s2.date_input(
        "Semana hasta", value=_sem_default[1], key="api_sem_hasta"
    )

    if st.button(
        "Sincronizar",
        type="primary",
        use_container_width=True,
        key="btn_sync_api",
    ):
        try:
            # 1) Maestros (clientes, productos, combos) — cache 1h.
            df_cli, df_prod, df_comb, _ = _api_sync_maestros()
            # 2) Facturación mensual y semanal — cache 1h por rango.
            df_fc_mes, errors_mes = _api_sync_fc(
                _fd_mes.isoformat(), _fh_mes.isoformat()
            )
            df_fc_sem, errors_sem = _api_sync_fc(
                _fd_sem.isoformat(), _fh_sem.isoformat()
            )
            # 3) Guardar en session y activar fuente API.
            st.session_state.df_clientes = df_cli
            st.session_state.df_productos = df_prod
            st.session_state.df_combos = df_comb
            st.session_state.df_fc_mes_raw = df_fc_mes
            st.session_state.df_fc_sem_raw = df_fc_sem
            st.session_state.fuente_activa = "api"
            st.session_state.api_last_sync = datetime.now()
            st.session_state.api_rango = (_fd_mes, _fh_mes, _fd_sem, _fh_sem)
            st.session_state.api_errors_mes = errors_mes
            st.session_state.api_errors_sem = errors_sem
            _total_errors = len(errors_mes) + len(errors_sem)
            if _total_errors:
                st.warning(
                    f"Sincronizado con {_total_errors} comprobante(s) omitidos "
                    f"por error de fetch. Ver detalle en tab **Salud**."
                )
            else:
                st.success(
                    f"Sincronizado: {len(df_fc_mes)} filas del mes, "
                    f"{len(df_fc_sem)} filas de la semana."
                )
        except api_loader.AuthError as e:
            st.error(
                f"**Credenciales rechazadas por Contabilium.** {e}\n\n"
                "Revisá `contabilium_client_id` y `contabilium_client_secret` "
                "en `.streamlit/secrets.toml` (o en Streamlit Cloud → Settings "
                "→ Secrets si estás en producción). Mientras tanto podés "
                "usar el **Modo Manual Secundario** más abajo."
            )
        except api_loader.ApiError as e:
            st.error(
                f"**Error al sincronizar con la API:** {e}\n\n"
                "Si persiste, podés usar el **Modo Manual Secundario** abajo."
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
            st.session_state.api_last_sync = None
            st.session_state.fuente_activa = None
            st.success(
                "Caché limpiado. Tocá 'Sincronizar' arriba para traer "
                "datos frescos."
            )
            st.rerun()

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
                st.session_state.df_clientes = df_cli_m
                st.session_state.df_productos = df_prod_m
                st.session_state.df_combos = df_comb_m
                st.session_state.fuente_activa = "manual"
                # El modo manual no tiene N+1, no hay errores de fetch.
                st.session_state.api_errors_mes = []
                st.session_state.api_errors_sem = []
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

try:
    df_sem, health_sem = _prepare_cached(
        df_fc_sem_raw, df_clientes, df_productos, df_combos
    )
    df_mes, health_mes = _prepare_cached(
        df_fc_mes_raw, df_clientes, df_productos, df_combos
    )
except Exception as e:  # noqa: BLE001
    st.error(f"**Error procesando datos:** {e}")
    st.stop()


# =====================================================================
# SIDEBAR — bloque de exportar agenda (necesita datos ya cargados)
# =====================================================================

with st.sidebar:
    st.divider()
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
    # Encabezado con fuente activa + timestamp (para trazabilidad).
    if st.session_state.fuente_activa == "api":
        _rango = st.session_state.api_rango
        if _rango:
            _fd_m, _fh_m, _fd_s, _fh_s = _rango
            st.caption(
                f"Fuente: **API Contabilium** · "
                f"mes {_fd_m.isoformat()} → {_fh_m.isoformat()} · "
                f"semana {_fd_s.isoformat()} → {_fh_s.isoformat()} · "
                f"sync {st.session_state.api_last_sync:%Y-%m-%d %H:%M}"
            )
        else:
            st.caption("Fuente: **API Contabilium**")
    else:
        st.caption("Fuente: **Modo Manual Secundario** (xlsx cargados manualmente)")

    # -- Errores del N+1 de load_fc_api, si los hay --
    _errs_mes = st.session_state.get("api_errors_mes", []) or []
    _errs_sem = st.session_state.get("api_errors_sem", []) or []
    if _errs_mes or _errs_sem:
        st.warning(
            f"**Comprobantes omitidos del sync**: {len(_errs_mes)} del mes, "
            f"{len(_errs_sem)} de la semana. Son comprobantes cuyo detalle "
            f"(GetById) falló tras los retries automáticos. Los montos totales "
            f"están subvaluados por esos comprobantes — si la cifra es "
            f"significativa, re-sincronizá con el botón 'Resync forzado' de "
            f"la sidebar."
        )
        with st.expander(f"Ver IDs omitidos ({len(_errs_mes) + len(_errs_sem)})"):
            if _errs_mes:
                st.markdown("**Mes:**")
                for _id, _msg in _errs_mes[:50]:
                    st.text(f"  {_id}: {_msg[:200]}")
                if len(_errs_mes) > 50:
                    st.caption(f"(+{len(_errs_mes) - 50} más)")
            if _errs_sem:
                st.markdown("**Semana:**")
                for _id, _msg in _errs_sem[:50]:
                    st.text(f"  {_id}: {_msg[:200]}")
                if len(_errs_sem) > 50:
                    st.caption(f"(+{len(_errs_sem) - 50} más)")

    col_sem, col_mes = st.columns(2)
    with col_sem:
        _render_health_section("Semana", health_sem)
    with col_mes:
        _render_health_section("Mes", health_mes)
