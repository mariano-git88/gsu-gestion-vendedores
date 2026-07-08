"""
comisiones_app.py — App de Streamlit para Liquidación de Comisiones.

Entry point separado del dashboard principal (app.py). Se deploya en
Streamlit Cloud como un segundo app del mismo repo: misma codebase,
URL distinta, secrets propios. Reutiliza `api_loader.py`,
`vendedores.py`, `theme.py` y la lógica de cómputo de `commissions.py`.

Auth: 1 password adicional (`comisiones_password` en secrets).
Persistencia del histórico: Google Sheet (módulo `gsheets.py`).

Flujo:
  1. Login con password.
  2. Selector de mes (default mes anterior cerrado).
  3. Botón "Calcular comisiones" → pull API + cálculo + tabla resultado.
  4. Sección de casos especiales (huérfanas a MARIO, vendedores
     excluidos, etc) en un expander.
  5. Descargar liquidación en xlsx.
  6. Guardar en histórico (gate explícito anti-duplicado).
  7. Tab "Histórico" con la tabla acumulada del Sheet.
"""

from __future__ import annotations

import hmac
from calendar import monthrange
from datetime import date

import pandas as pd
import streamlit as st

import api_loader
import comisiones_ajuste
import comisiones_data
import commissions
import gsheets
import theme


# =====================================================================
# Page config + theme
# =====================================================================

st.set_page_config(
    page_title="Liquidación de Comisiones — GSU",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()

# Override local de botones del CUERPO PRINCIPAL: naranja (ACCENT) y
# compactos. Excluye la sidebar (`[data-testid="stSidebar"]`), que
# mantiene el estilo INK oscuro del theme.
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
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Auth gate
# =====================================================================

def _check_password() -> bool:
    """Login con `comisiones_password` (distinto del dashboard principal).

    Usa `auth_comisiones` como key de session_state — separada de
    `authenticated` del dashboard, así si alguien tiene las dos apps
    abiertas no se confunden.
    """
    if st.session_state.get("auth_comisiones", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Liquidación de Comisiones</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Cálculo mensual de comisiones para los vendedores de GSU. "
            "Acceso restringido."
        )
        with st.form("login_comisiones", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("comisiones_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_comisiones = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Helpers de período
# =====================================================================

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def _mes_anterior(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def _opciones_meses(n: int = 12, hoy: date | None = None) -> list[tuple[int, int]]:
    """Lista de (year, month) terminada en mes anterior al actual,
    going hacia atrás `n` meses. Mes anterior primero (default a
    seleccionar)."""
    if hoy is None:
        hoy = date.today()
    y, m = _mes_anterior(hoy)
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def _label_mes(y: int, m: int) -> str:
    return f"{MESES_ES[m - 1]} {y}"


def _rango_mes(y: int, m: int) -> tuple[str, str]:
    """ISO YYYY-MM-DD del primer y último día del mes."""
    inicio = date(y, m, 1)
    ultimo = monthrange(y, m)[1]
    fin = date(y, m, ultimo)
    return inicio.isoformat(), fin.isoformat()


def _periodo_anterior(y: int, m: int) -> tuple[int, int]:
    """(y-1, m-1) calendario, cruzando año si corresponde."""
    if m == 1:
        return y - 1, 12
    return y, m - 1


# =====================================================================
# Caches
# =====================================================================

@st.cache_resource
def _api_session():
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(
    ttl=3600,
    show_spinner="Sincronizando con Contabilium...",
    hash_funcs={api_loader.ApiSession: lambda _: None},
)
def _calcular_periodo(fecha_desde: str, fecha_hasta: str):
    """Pullea clientes + ventas + cobranzas y calcula comisiones.

    Cacheado por (fecha_desde, fecha_hasta) — recalcular el mismo
    período no vuelve a pegar a la API por 1 hora.

    Retorna dict con: resumen, ventas, cobranzas, mapa_clientes
    (este último necesario para el ajuste retroactivo del caller).
    """
    session = _api_session()
    session, mapa, valid_vendors = comisiones_data.cargar_clientes_para_comisiones(session)
    # IDs de órdenes facturadas via API masiva (canceladas pero con
    # factura emitida vinculada vía RefExterna). Se cuentan como
    # ventas válidas aunque su estado en Contabilium sea Cancelada.
    # Ver sesión 2026-05-13: pivot a RefExterna para no perder
    # comisiones de órdenes que el facturador cancela para liberar
    # StockReservado del bug Contabilium.
    session, ids_facturadas = comisiones_data.cargar_ids_ordenes_facturadas_via_api(
        session, fecha_desde, fecha_hasta
    )
    session, ventas = comisiones_data.cargar_ventas_desde_api(
        session, fecha_desde, fecha_hasta, valid_vendors,
        ids_facturadas_via_api=ids_facturadas,
    )
    session, cobranzas = comisiones_data.cargar_cobranzas_desde_api(
        session, fecha_desde, fecha_hasta, mapa
    )
    resumen = commissions.compute_commissions(ventas, cobranzas)
    return {
        "resumen": resumen,
        "ventas": ventas,
        "cobranzas": cobranzas,
        "mapa_clientes": mapa,
    }


@st.cache_data(
    ttl=3600,
    show_spinner="Calculando ajuste retroactivo del mes anterior...",
    hash_funcs={api_loader.ApiSession: lambda _: None},
)
def _pull_cobranzas_api_raw(fecha_desde: str, fecha_hasta: str) -> list[dict]:
    """Pull "crudo" de cobranzas (items API sin agrupar) para usar en
    el cálculo del ajuste retroactivo. Cacheado independiente del
    cálculo principal porque se usa desde el caller del ajuste."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date as _date, timedelta

    session = _api_session()
    session = api_loader._refrescar_si_expirado(session)
    fd = _date.fromisoformat(fecha_desde)
    fh = _date.fromisoformat(fecha_hasta)
    fechas = [(fd + timedelta(days=i)).isoformat() for i in range((fh - fd).days + 1)]

    items: list[dict] = []
    session_snap = session

    def _fetch(fecha):
        path = f"/api/cobranzas/search?fechaDesde={fecha}&fechaHasta={fecha}&count=50"
        _, payload = api_loader.api_get(session_snap, path)
        dia_items = payload.get("Items", []) or []
        if len(dia_items) >= 50:
            raise api_loader.ApiError(
                f"El día {fecha} tiene {len(dia_items)} cobranzas (cap del servidor)."
            )
        return dia_items

    with ThreadPoolExecutor(max_workers=10) as exe:
        futures = [exe.submit(_fetch, f) for f in fechas]
        for fut in as_completed(futures):
            items.extend(fut.result())
    return items


@st.cache_data(ttl=300, show_spinner="Leyendo histórico...")
def _read_historico_cached():
    return gsheets.read_historico(dict(st.secrets["gsheets"]))


# =====================================================================
# Sidebar
# =====================================================================

with st.sidebar:
    st.header("Período")
    opciones = _opciones_meses(12)
    sel_idx = st.selectbox(
        "Mes a liquidar",
        options=range(len(opciones)),
        format_func=lambda i: _label_mes(*opciones[i]),
        index=0,
        key="mes_idx",
    )
    sel_y, sel_m = opciones[sel_idx]
    fecha_desde, fecha_hasta = _rango_mes(sel_y, sel_m)
    periodo_label = f"{sel_y}-{sel_m:02d}"
    st.caption(f"Rango: {fecha_desde} → {fecha_hasta}")

    st.divider()
    if st.button("Forzar recálculo (bypass caché)", use_container_width=True):
        _calcular_periodo.clear()
        _read_historico_cached.clear()
        st.session_state.pop("resultado", None)
        st.rerun()

    st.divider()
    if st.button("Cerrar sesión", use_container_width=True):
        st.session_state.pop("auth_comisiones", None)
        st.rerun()


# =====================================================================
# Main
# =====================================================================

st.title(f"Liquidación de Comisiones — {_label_mes(sel_y, sel_m)}")

# Navegación por secciones con `st.segmented_control` (no `st.tabs`): st.tabs
# renderiza el contenido de todas las tabs y durante los reruns se ve apilado
# (contenido de una sección aparece en otra). Con selector + `if` solo se
# renderiza la sección activa. Ver feedback_streamlit_tabs_derrame.
_SECCIONES = ["Calcular", "Histórico"]
seccion = st.segmented_control(
    "Sección", _SECCIONES, default=_SECCIONES[0],
    key="com_seccion", label_visibility="collapsed")
if not seccion:
    seccion = _SECCIONES[0]
st.write("")


# ---------------------------------------------------------------------
# SECCIÓN CALCULAR
# ---------------------------------------------------------------------

if seccion == _SECCIONES[0]:
    _btn_col, _ = st.columns([1, 3])
    with _btn_col:
        _do_calcular = st.button(
            "Calcular comisiones",
            use_container_width=True,
            key="btn_calcular",
        )
    if _do_calcular:
        try:
            resultado = _calcular_periodo(fecha_desde, fecha_hasta)

            # ----- Ajuste retroactivo del mes anterior (M-1) -----
            ajuste = None
            ajuste_msg = None
            prev_y, prev_m = _periodo_anterior(sel_y, sel_m)
            prev_label = f"{prev_y}-{prev_m:02d}"
            try:
                if gsheets.periodo_existe_en_historico(
                    dict(st.secrets["gsheets"]), prev_label
                ):
                    prev_desde, prev_hasta = _rango_mes(prev_y, prev_m)
                    prev_api_items = _pull_cobranzas_api_raw(
                        prev_desde, prev_hasta
                    )
                    prev_sheet_df = gsheets.read_cobranzas_periodo(
                        dict(st.secrets["gsheets"]), prev_label
                    )
                    diferencias = comisiones_ajuste.detectar_diferencias(
                        prev_api_items, prev_sheet_df
                    )
                    api_total = sum(
                        api_loader.parse_monto_uy(it.get("ImporteTotal"))
                        for it in prev_api_items
                    )
                    sheet_total = (
                        float(prev_sheet_df["importe"].sum())
                        if not prev_sheet_df.empty else 0.0
                    )
                    ajuste = comisiones_ajuste.calcular_ajuste(
                        diferencias,
                        resultado["mapa_clientes"],
                        cobranzas_api_total=api_total,
                        cobranzas_sheet_total=sheet_total,
                    )
                else:
                    ajuste_msg = (
                        f"Sin ajuste retroactivo: el período **{prev_label}** "
                        f"no está en el histórico. Si querés sembrarlo desde el "
                        f"xlsx legacy, corré "
                        f"`python3 sembrar_historico_desde_xlsx.py --periodo {prev_label}`."
                    )
            except (gsheets.GsheetsError, api_loader.ApiError) as e:
                ajuste_msg = (
                    f"⚠️ No se pudo calcular el ajuste retroactivo "
                    f"de {prev_label}: {e}. "
                    f"El cálculo del mes corriente sigue siendo válido."
                )

            st.session_state["resultado"] = {
                **resultado,
                "periodo_label": periodo_label,
                "ajuste": ajuste,
                "ajuste_msg": ajuste_msg,
                "prev_label": prev_label,
            }
        except Exception as e:  # noqa: BLE001 — visibilidad de errores
            st.error(f"Error al calcular: {e}")
            st.session_state.pop("resultado", None)

    res = st.session_state.get("resultado")
    if res is None:
        st.info("Tocá **Calcular comisiones** para sincronizar el período.")
    elif res["periodo_label"] != periodo_label:
        st.warning(
            f"El resultado en pantalla es del período "
            f"**{res['periodo_label']}**, pero seleccionaste "
            f"**{periodo_label}**. Tocá Calcular para refrescar."
        )
    else:
        # ---------- KPIs ----------
        resumen = res["resumen"]
        ventas = res["ventas"]
        cobranzas = res["cobranzas"]
        ajuste = res.get("ajuste")
        ajuste_msg = res.get("ajuste_msg")
        prev_label = res.get("prev_label", "")

        # Si hay ajuste, mergear con merge_commissions_with_adjustment
        # del legacy — ya soporta esa operación. Devuelve un resumen
        # extendido con ajuste_aplicado y comision_neta_con_ajuste.
        if ajuste is not None:
            resumen_eff = commissions.merge_commissions_with_adjustment(
                resumen, ajuste
            )
        else:
            resumen_eff = resumen

        total_neto_normal = sum(int(r["comision_neta"]) for r in resumen)
        total_ventas_brutas = sum(float(r["ventas_brutas"]) for r in resumen)
        total_cobranzas = sum(float(r["cobranzas"]) for r in resumen)
        n_con_comision = sum(1 for r in resumen if r["comision_neta"] > 0)

        if ajuste is not None:
            total_ajuste = sum(
                float(r.get("ajuste_aplicado", 0.0)) for r in resumen_eff
            )
            total_final = sum(
                int(r.get("comision_neta_con_ajuste", r["comision_neta"]))
                for r in resumen_eff
            )
            # Fila 1: tres KPIs anchos para que las cifras grandes
            # entren completas (no se trunquen con "...").
            r1a, r1b, r1c = st.columns(3)
            r1a.metric("Ventas brutas (UYU)", f"{total_ventas_brutas:,.0f}")
            r1b.metric("Cobranzas (UYU)", f"{total_cobranzas:,.0f}")
            r1c.metric("Comisión del mes (UYU)", f"{total_neto_normal:,.0f}")

            # Fila 2: ajuste retroactivo y total a pagar.
            r2a, r2b = st.columns(2)
            r2a.metric(
                f"Ajuste {prev_label} (UYU)",
                f"{total_ajuste:,.0f}",
                help="Cobranzas tardías de M-1 × 3%. Ajustes "
                     "negativos quedan en alerta, no se descuentan.",
            )
            r2b.metric(
                "TOTAL a pagar (UYU)",
                f"{total_final:,.0f}",
                delta=(
                    f"+{total_ajuste:,.0f} por ajuste"
                    if total_ajuste > 0 else None
                ),
                delta_color="off",
            )
        else:
            r1a, r1b, r1c = st.columns(3)
            r1a.metric("Ventas brutas (UYU)", f"{total_ventas_brutas:,.0f}")
            r1b.metric("Cobranzas (UYU)", f"{total_cobranzas:,.0f}")
            r1c.metric(
                "TOTAL a pagar (UYU)",
                f"{total_neto_normal:,.0f}",
                help=f"{n_con_comision} vendedor(es) con comisión.",
            )
            if ajuste_msg:
                st.caption(ajuste_msg)

        # ---------- Tabla de resumen ----------
        st.markdown("### Detalle por vendedor")
        df = pd.DataFrame(resumen_eff)

        if ajuste is not None:
            cols_display = [
                "vendedor", "ventas_brutas", "ventas_netas",
                "cobranzas", "comision_venta", "comision_cobranza",
                "comision_bruta", "comision_neta",
                "ajuste_aplicado", "comision_neta_con_ajuste",
            ]
            sort_col = "comision_neta_con_ajuste"
        else:
            cols_display = [
                "vendedor", "ventas_brutas", "ventas_netas",
                "cobranzas", "comision_venta", "comision_cobranza",
                "comision_bruta", "comision_neta",
            ]
            sort_col = "comision_neta"

        df_display = df[cols_display].copy()
        df_display = df_display.sort_values(sort_col, ascending=False)
        fmt_dict = {
            "ventas_brutas": "{:,.2f}",
            "ventas_netas": "{:,.2f}",
            "cobranzas": "{:,.2f}",
            "comision_venta": "{:,.2f}",
            "comision_cobranza": "{:,.2f}",
            "comision_bruta": "{:,.2f}",
            "comision_neta": "{:,.0f}",
        }
        col_config = {
            "vendedor": st.column_config.TextColumn("Vendedor"),
            "ventas_brutas": st.column_config.NumberColumn("Ventas brutas"),
            "ventas_netas": st.column_config.NumberColumn("Ventas netas"),
            "cobranzas": st.column_config.NumberColumn("Cobranzas"),
            "comision_venta": st.column_config.NumberColumn(
                "Com. venta (2,35%)",
                help="Sobre ventas netas (sin IVA).",
            ),
            "comision_cobranza": st.column_config.NumberColumn(
                "Com. cobranza (3%)",
                help="Sobre cobranzas (importe directo de Contabilium).",
            ),
            "comision_bruta": st.column_config.NumberColumn("Bruta"),
            "comision_neta": st.column_config.NumberColumn(
                "Neta del mes",
                help="Comisión del período sin ajuste retroactivo.",
            ),
        }
        if ajuste is not None:
            fmt_dict["ajuste_aplicado"] = "{:,.2f}"
            fmt_dict["comision_neta_con_ajuste"] = "{:,.0f}"
            col_config["ajuste_aplicado"] = st.column_config.NumberColumn(
                f"Ajuste {prev_label}",
                help="Comisión por cobranzas tardías de M-1 (3%). "
                     "Solo positivos se suman; negativos quedan en alerta.",
            )
            col_config["comision_neta_con_ajuste"] = st.column_config.NumberColumn(
                "TOTAL a pagar",
                help="Comisión del mes + ajuste positivo.",
            )

        st.dataframe(
            df_display.style.format(fmt_dict),
            use_container_width=True,
            hide_index=True,
            column_config=col_config,
        )

        # ---------- Casos especiales ----------
        with st.expander("Casos especiales (auditar antes de guardar)"):
            # Ventas excluidas
            excl = ventas["excluidas"]
            st.markdown("**Ventas excluidas del cálculo**")
            st.write(f"- Por vendedor OP* (Jesica/Valeria): {excl['vendedor_op']}")
            st.write(f"- Por estado Cancelada: {excl['cancelada']}")
            st.write(
                f"- Por vendedor sin clientes vinculados: {excl['vendedor_invalido']}"
            )
            bruto_inv = ventas.get("bruto_excluido_invalido", {})
            if bruto_inv:
                st.markdown("Detalle del bruto excluido por vendedor inválido:")
                for v, m in bruto_inv.items():
                    st.write(f"  - `{v}`: ${m:,.2f}")

            # Cobranzas huérfanas
            huer = cobranzas["huerfanas_a_mario"]
            st.markdown(f"**Cobranzas con RUT inexistente → MARIO ({len(huer)})**")
            if huer:
                df_huer = pd.DataFrame(
                    huer, columns=["RUT", "Razón social", "Número", "Importe"]
                )
                st.dataframe(
                    df_huer.style.format({"Importe": "{:,.2f}"}),
                    use_container_width=True,
                    hide_index=True,
                )

            # Cobranzas descartadas
            desc = cobranzas["descartadas_sin_vendedor"]
            st.markdown(
                f"**Cobranzas descartadas (cliente sin vendedor asignado) "
                f"({len(desc)})**"
            )
            if desc:
                df_desc = pd.DataFrame(
                    desc, columns=["RUT", "Razón social", "Número", "Importe"]
                )
                st.dataframe(
                    df_desc.style.format({"Importe": "{:,.2f}"}),
                    use_container_width=True,
                    hide_index=True,
                )

            # Monedas no UYU
            mon_v = ventas.get("monedas_no_uyu", [])
            mon_c = cobranzas.get("monedas_no_uyu", [])
            if mon_v or mon_c:
                st.markdown("**⚠️ Operaciones con moneda distinta de UYU**")
                for nro, mon in mon_v:
                    st.write(f"- Venta {nro}: moneda={mon}")
                for nro, mon in mon_c:
                    st.write(f"- Cobranza {nro}: moneda={mon}")

        # ---------- Ajuste retroactivo (solo si aplica) ----------
        if ajuste is not None:
            n_tar = len([c for c in ajuste["cambios"] if c["tipo"].startswith("tardía")])
            n_anu = len([c for c in ajuste["cambios"] if c["tipo"] == "anulada"])
            n_mod = len([c for c in ajuste["cambios"] if c["tipo"] == "modificada"])

            # Banner si hay anuladas (alerta)
            if ajuste.get("vendedores_con_ajuste_negativo"):
                negativos = ajuste["vendedores_con_ajuste_negativo"]
                lineas = [
                    f"  - {v}: ${m:,.2f}" for v, m in negativos.items()
                ]
                st.warning(
                    f"⚠️ Hay {n_anu} cobranza(s) anulada(s) en {prev_label} "
                    f"(estaban pagadas pero ya no figuran en Contabilium). "
                    f"NO se descuentan del pago, pero **revisá manualmente**:\n\n"
                    + "\n".join(lineas)
                )

            with st.expander(
                f"Ajuste retroactivo de {prev_label} "
                f"({n_tar} tardía/s, {n_anu} anulada/s, {n_mod} modificada/s)"
            ):
                st.caption(
                    f"Cobranzas de **{prev_label}** que cambiaron desde la "
                    f"última liquidación. Total cobranzas en API: "
                    f"${ajuste['total_actualizada']:,.2f} · "
                    f"Total guardado en Sheet: ${ajuste['total_orig']:,.2f} · "
                    f"Delta: ${ajuste['total_actualizada'] - ajuste['total_orig']:+,.2f}"
                )
                if ajuste["cambios"]:
                    df_aj = pd.DataFrame(ajuste["cambios"])
                    st.dataframe(
                        df_aj.style.format({
                            "importe_original": "{:,.2f}",
                            "importe_nuevo": "{:,.2f}",
                            "delta_importe": "{:+,.2f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.info("Sin cambios — todas las cobranzas de M-1 están al día.")

                # Tardías huérfanas
                hue_tar = ajuste.get("tardias_huerfanas_a_mario", [])
                if hue_tar:
                    st.markdown(
                        f"**Tardías que cayeron a MARIO por RUT inexistente "
                        f"({len(hue_tar)})**"
                    )
                    df_h = pd.DataFrame(hue_tar)
                    st.dataframe(df_h, use_container_width=True, hide_index=True)

                # Tardías descartadas
                desc_tar = ajuste.get("tardias_descartadas", [])
                if desc_tar:
                    st.markdown(
                        f"**Tardías descartadas (cliente sin vendedor) "
                        f"({len(desc_tar)})**"
                    )
                    df_d = pd.DataFrame(desc_tar)
                    st.dataframe(df_d, use_container_width=True, hide_index=True)
        elif ajuste_msg:
            st.info(ajuste_msg)

        # ---------- Descarga xlsx ----------
        st.divider()
        st.markdown("### Descargar liquidación")
        try:
            # build_xlsx_bytes ya soporta el parámetro `ajuste` (legacy):
            # si está, agrega columnas y una hoja "Ajuste retroactivo".
            xlsx_buf = commissions.build_xlsx_bytes(
                resumen_eff if ajuste is not None else resumen,
                ventas, cobranzas, periodo_label,
                ajuste=ajuste,
            )
            _dl_col, _ = st.columns([1, 3])
            with _dl_col:
                st.download_button(
                    label="Descargar liquidación.xlsx",
                    data=xlsx_buf.getvalue(),
                    file_name=f"liquidacion_{periodo_label}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        except Exception as e:  # noqa: BLE001
            st.error(f"Error generando xlsx: {e}")

        # ---------- Guardar en histórico ----------
        st.divider()
        st.markdown("### Guardar en histórico")
        st.caption(
            f"Va a agregar **{len(resumen)}** fila(s) al Sheet del período "
            f"**{periodo_label}**. Si el período ya está, hay que activar "
            "la sobreescritura explícitamente para que no duplique."
        )
        col_s1, col_s2, _ = st.columns([1, 1, 2])
        with col_s1:
            sobreescribir = st.checkbox(
                "Sobreescribir si ya existe", value=False, key="sobreescribir"
            )
        with col_s2:
            _do_guardar = st.button(
                "Guardar en histórico",
                key="btn_guardar",
                use_container_width=True,
            )
        if _do_guardar:
                try:
                    secrets_g = dict(st.secrets["gsheets"])

                    # 1. Guardar agregados en `historico` (con o sin ajuste).
                    # Si hay ajuste, persistimos la comisión total final
                    # (con ajuste positivo aplicado), no la del mes "puro".
                    if ajuste is not None:
                        # Re-mappear resumen_eff para que el campo
                        # "comision_neta" guardado refleje el TOTAL pagado.
                        resumen_para_guardar = []
                        for r in resumen_eff:
                            resumen_para_guardar.append({
                                **r,
                                "comision_neta": r.get(
                                    "comision_neta_con_ajuste", r["comision_neta"]
                                ),
                            })
                    else:
                        resumen_para_guardar = resumen

                    stats = gsheets.write_historico_periodo(
                        secrets_g, periodo_label, resumen_para_guardar,
                        sobreescribir=sobreescribir,
                    )

                    # 2. Guardar cobranzas individuales del mes M.
                    cobranzas_filas = comisiones_ajuste.cobranzas_para_persistir(
                        cobranzas
                    )
                    stats_c = gsheets.write_cobranzas_periodo(
                        secrets_g, periodo_label, cobranzas_filas
                    )

                    # 3. Si hubo ajuste, actualizar snapshot de M-1 con
                    # las cobranzas API actuales (incluye las tardías),
                    # para que la próxima vez no se re-detecten.
                    cobranzas_prev_filas = []
                    if ajuste is not None:
                        prev_y_, prev_m_ = _periodo_anterior(sel_y, sel_m)
                        prev_desde_, prev_hasta_ = _rango_mes(prev_y_, prev_m_)
                        # Pull las cobranzas de M-1 con el mapa actual y
                        # asignar reglas (huerfana/descartada/normal),
                        # igual que el cálculo del mes corriente — para
                        # mantener consistencia del Sheet.
                        session_p = _api_session()
                        session_p, cobr_prev = comisiones_data.cargar_cobranzas_desde_api(
                            session_p, prev_desde_, prev_hasta_,
                            res["mapa_clientes"],
                        )
                        cobranzas_prev_filas = comisiones_ajuste.cobranzas_para_persistir(
                            cobr_prev
                        )
                        gsheets.write_cobranzas_periodo(
                            secrets_g, prev_label, cobranzas_prev_filas
                        )

                    _read_historico_cached.clear()
                    msg = (
                        f"OK. Período {periodo_label}: agregadas "
                        f"{stats['filas_agregadas']} fila(s), reemplazadas "
                        f"{stats['filas_eliminadas']}. Cobranzas guardadas: "
                        f"{stats_c['filas_agregadas']}."
                    )
                    if cobranzas_prev_filas:
                        msg += (
                            f" Snapshot de {prev_label} actualizado con "
                            f"{len(cobranzas_prev_filas)} cobranzas."
                        )
                    st.success(msg)
                except gsheets.PeriodoYaExisteError as e:
                    st.warning(str(e))
                except gsheets.GsheetsError as e:
                    st.error(f"Error con Google Sheets: {e}")


# ---------------------------------------------------------------------
# SECCIÓN HISTÓRICO
# ---------------------------------------------------------------------

if seccion == _SECCIONES[1]:
    st.markdown("### Histórico acumulado de comisiones")
    st.caption(
        "Tabla actualizada automáticamente cada vez que tocás "
        "**Guardar en histórico**. Vive en Google Sheet — podés "
        "abrirla directamente desde Drive si necesitás editar manual."
    )

    try:
        df_hist = _read_historico_cached()
    except gsheets.CredencialesError as e:
        st.error(f"Credenciales de Google Sheets mal configuradas: {e}")
        df_hist = pd.DataFrame()
    except gsheets.GsheetsError as e:
        st.error(f"Error con Google Sheets: {e}")
        df_hist = pd.DataFrame()
    except Exception as e:  # noqa: BLE001
        st.error(f"Error inesperado: {type(e).__name__}: {e}")
        df_hist = pd.DataFrame()

    if df_hist.empty:
        st.info(
            "El histórico está vacío. Calculá un mes y tocá "
            "**Guardar en histórico**."
        )
    else:
        # KPIs del histórico
        n_periodos = df_hist["periodo"].nunique()
        n_vendedores = df_hist["vendedor"].nunique()
        total_pagado = int(df_hist["comision_neta"].sum())
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Períodos", f"{n_periodos:,}")
        col_b.metric("Vendedores únicos", f"{n_vendedores:,}")
        col_c.metric("Total acumulado (UYU)", f"{total_pagado:,.0f}")

        st.markdown("#### Tabla cronológica")
        df_hist_show = df_hist.sort_values(["periodo", "vendedor"], ascending=[False, True])
        st.dataframe(
            df_hist_show.style.format(
                {
                    "ventas": "{:,.2f}",
                    "cobranzas": "{:,.2f}",
                    "comision_neta": "{:,.0f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

        # Pivot vendedor × período
        st.markdown("#### Pivot vendedor × período")
        pivot = df_hist.pivot_table(
            index="vendedor",
            columns="periodo",
            values="comision_neta",
            aggfunc="sum",
            fill_value=0,
        )
        pivot = pivot[sorted(pivot.columns)]
        # Total a la derecha
        pivot["TOTAL"] = pivot.sum(axis=1)
        # Total fila
        pivot.loc["TOTAL"] = pivot.sum()
        st.dataframe(
            pivot.style.format("{:,.0f}"),
            use_container_width=True,
        )
