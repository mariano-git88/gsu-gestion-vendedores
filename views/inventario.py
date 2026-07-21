"""
views/inventario.py — Tab de Inventario.

Muestra para cada SKU (producto o combo) su stock actual y las semanas
de stock resultantes bajo 3 cortes de venta semanal promedio:

  - Último mes (últimos 30 días)
  - Últimos 3 meses (default para criticidad)
  - Mejor mes calendario de los últimos 12

Destaca en rojo los SKUs con <4 semanas según el corte de 3 meses
(umbral confirmado por Mariano 2026-04-18).

Fuentes:
  - df_productos (API modo API) con columna `stock` del endpoint
    `/api/conceptos/search`.
  - df_combos (API) con columna `stock` calculada derivando desde
    componentes (discovery + decisión 2026-04-18).
  - df_hist (session_state.df_hist12) con el histórico 12 meses
    cargado por el botón opt-in de la sidebar.

Si falta el histórico, muestra un aviso y no rompe.
Si la fuente es Manual (xlsx sin stock), degrada con aviso.
"""

from io import BytesIO

import pandas as pd
import streamlit as st

import api_loader
import gsheets
import metrics


@st.cache_data(ttl=1800, show_spinner=False)
def _stock_snapshots(_dia: str) -> pd.DataFrame:
    """Log de fotos diarias de stock (tab `stock_snapshots` del Sheet del
    dashboard). Cacheado por día. Devuelve DataFrame vacío si no hay Sheet
    configurado o la lectura falla — la vista nunca debe romper por esto."""
    empty = pd.DataFrame({"fecha": pd.Series(dtype="datetime64[ns]"),
                          "sku": pd.Series(dtype="object"),
                          "stock": pd.Series(dtype="float")})
    try:
        sec = dict(st.secrets.get("gsheets", {}))
    except Exception:  # noqa: BLE001
        return empty
    if not sec:
        return empty
    try:
        return gsheets.read_stock_snapshots(sec)
    except Exception:  # noqa: BLE001
        return empty


@st.cache_data(ttl=1800, show_spinner=False)
def _novedades_stock(_api_session, _dia: str) -> pd.DataFrame:
    """SKUs con movimiento de stock en los últimos 7 días (feed Novedades).

    Cacheado por día (`_dia` fuerza refresco diario). `_api_session` lleva
    guion bajo para que Streamlit no intente hashear el objeto de sesión.
    Devuelve DataFrame vacío si no hay sesión o si la API falla — el flag
    de "repuesto reciente" es informativo y nunca debe romper la vista.
    """
    cols = ["sku", "stock", "disponible", "fecha_mod"]
    if _api_session is None:
        return pd.DataFrame(columns=cols)
    try:
        _, df = api_loader.get_stock_novedades(_api_session, dias=7)
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=cols)


def render(
    df_sem: pd.DataFrame,
    df_mes: pd.DataFrame,
    df_clientes: pd.DataFrame,
    health_sem: dict | None = None,
    health_mes: dict | None = None,
    api_session=None,
) -> None:
    """Firma uniforme con las otras vistas. df_sem/df_mes (facturación
    fresca de la semana y el mes en curso) se usan para suplementar el
    histórico 12m en la detección de stock muerto; `api_session` alimenta
    el feed de novedades de stock (marca de repuestos recientes). El resto
    va contra session_state (df_productos, df_combos, df_hist12)."""
    del df_clientes, health_sem, health_mes

    st.subheader("Inventario")
    st.caption(
        "Stock actual por SKU (productos y combos consolidado) y **semanas "
        "de stock** bajo 3 cortes de venta semanal promedio. El stock de "
        "combos se calcula derivando de sus componentes — lo que permite "
        "armar efectivamente. Los SKUs con **menos de 4 semanas** de stock "
        "según el corte de 3 meses se marcan **críticos** (en rojo). El "
        "**valor de stock** está en **UYU netos sin IVA** (precio de "
        "venta dividido 1.22), comparable con los montos del Resumen."
    )

    # Degradación — Modo Manual no tiene stock
    if st.session_state.get("fuente_activa") != "api":
        st.info(
            "La tab de Inventario solo funciona en **modo API**. El Modo "
            "Manual Secundario no incluye el campo `Stock` del maestro "
            "de conceptos de Contabilium."
        )
        return

    df_productos = st.session_state.get("df_productos")
    df_combos = st.session_state.get("df_combos")
    df_hist12 = st.session_state.get("df_hist12")

    if df_productos is None or df_productos.empty:
        st.info(
            "No hay maestro de productos cargado. Tocá **Sincronizar** "
            "en la sidebar."
        )
        return

    if "stock" not in df_productos.columns:
        st.warning(
            "**El maestro de productos cacheado no tiene la columna `stock`.** "
            "Esto pasa si sincronizaste con una versión anterior del código "
            "y el caché sigue vigente.\n\n"
            "Solución: tocá **'Resync forzado (bypass caché)'** en la "
            "sidebar y volvé a **Sincronizar**."
        )
        return

    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para calcular semanas de stock hace falta el **histórico de "
            "12 meses**. Cargalo desde la sidebar (botón 'Cargar histórico')."
        )
        return

    # ===== Calcular tabla consolidada =====
    inv = metrics.inventario_semanas_stock(
        df_productos, df_combos, df_hist12,
    )

    if inv.empty:
        st.info("No hay datos para mostrar.")
        return

    # ===== KPIs arriba =====
    n_total = len(inv)
    n_criticos = int(inv["critico"].sum())
    stock_total_unidades = float(inv["stock"].sum())
    valor_total = float(inv["valor_stock"].sum()) if "valor_stock" in inv.columns else 0.0
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric(
        "SKUs totales",
        f"{n_total:,}",
        help="Productos + combos con stock registrado en Contabilium.",
    )
    col_b.metric(
        "SKUs críticos",
        f"{n_criticos:,}",
        delta=f"{n_criticos / n_total * 100:.1f}% del total" if n_total else None,
        delta_color="inverse" if n_criticos > 0 else "off",
        help=(
            "SKUs con menos de 4 semanas de stock según el corte de "
            "últimos 3 meses. Los SKUs sin venta en 3 meses NO se "
            "cuentan como críticos (no hay demanda)."
        ),
    )
    col_c.metric(
        "Unidades en stock",
        f"{stock_total_unidades:,.0f}",
        help="Suma de unidades en stock de todos los SKUs.",
    )
    col_d.metric(
        "Valor de stock (UYU)",
        f"{valor_total:,.0f}",
        help=(
            "Suma de stock × precio neto de cada SKU. UYU sin IVA "
            "(PrecioFinal / 1.22). Comparable con los montos del Resumen."
        ),
    )

    # ===== Stock muerto (con stock, sin ventas en N días) =====
    st.divider()
    st.markdown("### 🪦 Stock muerto")
    st.caption(
        "Artículos **con stock** que **no vendieron ni una unidad** en la "
        "ventana elegida. Sirve para detectar mercadería inmovilizada / a "
        "discontinuar. Se toma la venta bruta (una devolución no cuenta como "
        "venta). Tablas descargables completas."
    )

    # Ventas para detectar "muerto": el histórico 12m (opt-in, que
    # «Sincronizar» NO refresca) SUPLEMENTADO con la facturación fresca de
    # la semana y el mes en curso. Así una venta reciente —incluso de hoy—
    # se cuenta y el SKU no aparece muerto por dato viejo. Como
    # stock_muerto solo mira "vendió sí/no" (conjunto) + última fecha, la
    # posible superposición de filas entre las tres fuentes no afecta.
    _cols_v = ["sku", "fecha", "unidades"]
    _fuentes_v = [
        d[_cols_v] for d in (df_hist12, df_mes, df_sem)
        if d is not None and not d.empty and set(_cols_v).issubset(d.columns)
    ]
    df_ventas = (
        pd.concat(_fuentes_v, ignore_index=True) if _fuentes_v else df_hist12
    )

    # Cobertura de datos: hasta qué fecha llegan las ventas contadas (ya
    # con la data fresca sumada). Si aun así no llega a hoy, avisamos.
    _cobertura = pd.to_datetime(df_ventas["fecha"], errors="coerce").max()
    if pd.notna(_cobertura):
        st.caption(
            f"Ventas contadas hasta **{_cobertura.strftime('%d/%m/%Y')}** "
            "(histórico 12m + facturación fresca de la semana y el mes)."
        )
        if _cobertura.normalize() < pd.Timestamp.today().normalize():
            st.warning(
                f"⚠️ Las ventas contadas llegan hasta el "
                f"{_cobertura.strftime('%d/%m/%Y')}. Si hubo ventas después, "
                "un artículo puede figurar acá por error. Tocá "
                "**«Sincronizar»** (trae la semana y el mes al día); para el "
                "tramo más viejo, **«Recargar histórico (12 meses)»**."
            )

    ventanas = [30, 60, 120]
    tablas_muerto = {d: metrics.stock_muerto(inv, df_ventas, d) for d in ventanas}

    # Falsos positivos por DISPONIBILIDAD: un artículo puede no haber
    # vendido en la ventana porque no tuvo stock para vender, no porque la
    # demanda esté muerta. Dos señales:
    #  1) repuesto_reciente — tuvo movimiento de stock en los últimos 7
    #     días (feed Novedades). Cubre el pasado inmediato.
    #  2) sin_stock_ventana — estuvo sin stock la mayor parte de la ventana
    #     según el log de fotos diarias (`stock_snapshots`). Cubre el caso
    #     general, y se vuelve preciso a medida que el log acumula historia.
    _hoy = pd.Timestamp.today().normalize()
    _nov = _novedades_stock(api_session, _hoy.strftime("%Y-%m-%d"))
    _skus_repuestos = set(_nov["sku"].astype(str)) if not _nov.empty else set()
    _snaps = _stock_snapshots(_hoy.strftime("%Y-%m-%d"))
    for d in ventanas:
        t = tablas_muerto[d]
        if t.empty:
            t["repuesto_reciente"] = pd.Series(dtype=bool)
            t["sin_stock_ventana"] = pd.Series(dtype=bool)
            t["no_disponible"] = pd.Series(dtype=bool)
            continue
        _skus_sin = metrics.skus_sin_stock_en_ventana(
            _snaps, d, t["sku"].astype(str).tolist(), hoy=_hoy
        )
        t["repuesto_reciente"] = t["sku"].astype(str).isin(_skus_repuestos)
        t["sin_stock_ventana"] = t["sku"].astype(str).isin(_skus_sin)
        t["no_disponible"] = t["repuesto_reciente"] | t["sin_stock_ventana"]

    col_m1, col_m2 = st.columns([1, 2])
    _ventana_sel = col_m1.radio(
        "Ventana",
        options=ventanas,
        index=1,  # default 60 días
        format_func=lambda d: f"{d} días",
        horizontal=True,
        key="inv_muerto_ventana",
    )
    _ocultar_no_disp = col_m2.checkbox(
        "Ocultar los que no estuvieron disponibles (repuestos recientes o "
        "sin stock en la ventana)",
        value=True,
        key="inv_muerto_ocultar_nodisp",
        help="Oculta artículos que no vendieron porque no tuvieron stock, no "
             "porque la demanda esté muerta: repuestos con movimiento de "
             "stock esta semana (feed de 7 días) o que estuvieron sin stock "
             "la mayor parte de la ventana (log de stock diario, se vuelve "
             "preciso con las semanas).",
    )
    _excluir_sin_valor = col_m2.checkbox(
        "Excluir artículos sin valor de stock ($0, típicamente marketing/SC)",
        value=False,
        key="inv_muerto_excl0",
        help="Oculta ítems con valor de stock 0 (precio $0), que suelen ser "
             "material promocional, no producto comercial.",
    )

    def _filtrar_muerto(t: pd.DataFrame) -> pd.DataFrame:
        if t.empty:
            return t
        if _ocultar_no_disp and "no_disponible" in t.columns:
            t = t[~t["no_disponible"]]
        if _excluir_sin_valor and "valor_stock" in t.columns:
            t = t[t["valor_stock"] > 0]
        return t

    km = st.columns(3)
    for i, d in enumerate(ventanas):
        _n = len(_filtrar_muerto(tablas_muerto[d]))
        _n_nd = int(tablas_muerto[d]["no_disponible"].sum()) \
            if not tablas_muerto[d].empty else 0
        km[i].metric(
            f"Sin ventas {d} días",
            f"{_n:,}",
            help=f"SKUs con stock que no vendieron nada en los últimos {d} "
                 f"días (con los filtros activos). {_n_nd} se descartan por "
                 f"disponibilidad (repuestos / sin stock en la ventana).",
        )

    tabla_muerto = _filtrar_muerto(tablas_muerto[_ventana_sel])

    _col_cfg_muerto = {
        "sku": st.column_config.TextColumn("SKU", width="small"),
        "nombre": st.column_config.TextColumn("Nombre"),
        "tipo": st.column_config.TextColumn("Tipo", width="small"),
        "sub_rubro": st.column_config.TextColumn("Sub-rubro", width="small"),
        "stock": st.column_config.NumberColumn("Stock", format="%.0f"),
        "precio": None,
        "valor_stock": st.column_config.NumberColumn(
            "Valor (UYU)", format="%.0f", help="Stock × precio neto sin IVA."
        ),
        "ultima_venta": st.column_config.DateColumn(
            "Última venta", format="DD/MM/YYYY",
            help="Última venta en el histórico. Vacío = nunca vendido en 12m.",
        ),
        "dias_sin_venta": st.column_config.NumberColumn(
            "Días sin venta", format="%.0f",
            help="Días desde la última venta. Vacío = nunca vendido.",
        ),
        "repuesto_reciente": st.column_config.CheckboxColumn(
            "Repuesto 7d", width="small",
            help="Tuvo movimiento de stock en los últimos 7 días "
                 "(posible reposición reciente).",
        ),
        "sin_stock_ventana": st.column_config.CheckboxColumn(
            "Sin stock", width="small",
            help="Estuvo sin stock la mayor parte de la ventana según el "
                 "log de stock diario (no podía vender).",
        ),
        "no_disponible": None,  # flag interno combinado, no se muestra
    }

    if tabla_muerto.empty:
        st.success(
            f"No hay artículos con stock sin ventas en los últimos "
            f"{_ventana_sel} días con los filtros actuales."
        )
    else:
        # Con el filtro de disponibilidad activo, las columnas-flag quedan
        # todas en False → se sacan para no meter ruido. Si el usuario las
        # muestra, se dejan visibles para distinguir por qué entran.
        _mostrar = tabla_muerto
        _flags = ["repuesto_reciente", "sin_stock_ventana", "no_disponible"]
        if _ocultar_no_disp:
            _mostrar = _mostrar.drop(
                columns=[c for c in _flags if c in _mostrar.columns]
            )
        elif "no_disponible" in _mostrar.columns:
            _mostrar = _mostrar.drop(columns=["no_disponible"])
        st.dataframe(
            _mostrar, use_container_width=True, hide_index=True,
            column_config=_col_cfg_muerto,
        )
        valor_inmov = float(tabla_muerto["valor_stock"].sum()) \
            if "valor_stock" in tabla_muerto.columns else 0.0
        st.caption(
            f"{len(tabla_muerto):,} artículos sin ventas en {_ventana_sel} "
            f"días. Valor de stock inmovilizado: $ {valor_inmov:,.0f} UYU."
        )

    # Descargas: CSV de la ventana seleccionada + Excel con las 3 ventanas.
    col_dl1, col_dl2 = st.columns(2)
    col_dl1.download_button(
        f"⬇️ CSV — sin ventas {_ventana_sel}d",
        data=tabla_muerto.to_csv(index=False).encode("utf-8"),
        file_name=f"stock_muerto_{_ventana_sel}d.csv",
        mime="text/csv",
        use_container_width=True,
    )
    _buf_muerto = BytesIO()
    with pd.ExcelWriter(_buf_muerto, engine="openpyxl") as _w:
        for d in ventanas:
            _filtrar_muerto(tablas_muerto[d]).to_excel(
                _w, index=False, sheet_name=f"{d} dias"
            )
    col_dl2.download_button(
        "⬇️ Excel — 30 / 60 / 120 días",
        data=_buf_muerto.getvalue(),
        file_name="stock_muerto.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    # ===== Filtros =====
    st.divider()
    col_f1, col_f2, col_f3 = st.columns(3)

    # Filtro por tipo (Producto / Combo / Todos)
    tipos_opts = ["(Todos)"] + sorted(inv["tipo"].dropna().unique().tolist())
    _tipo = col_f1.selectbox("Tipo", options=tipos_opts, key="inv_tipo")

    # Filtro por sub-rubro
    sr_opts = ["(Todos)"] + sorted(
        inv["sub_rubro"].dropna().astype(str).unique().tolist()
    )
    _sub_rubro = col_f2.selectbox("Sub-rubro", options=sr_opts, key="inv_sr")

    # Filtro de solo críticos
    _solo_criticos = col_f3.checkbox(
        "Solo críticos",
        value=False,
        key="inv_solo_criticos",
        help="Ocultar SKUs que no están en nivel crítico.",
    )

    vista = inv.copy()
    if _tipo != "(Todos)":
        vista = vista[vista["tipo"] == _tipo]
    if _sub_rubro != "(Todos)":
        vista = vista[vista["sub_rubro"].astype(str) == _sub_rubro]
    if _solo_criticos:
        vista = vista[vista["critico"]]

    # ===== Tabla principal =====
    st.divider()
    st.markdown("### SKUs y semanas de stock")
    st.caption(
        "Columnas `sem_...` muestran las semanas de stock estimadas bajo "
        "cada corte: **sem_ult_mes** (ventana corta, volátil), "
        "**sem_3m** (default para criticidad) y **sem_mejor_12m** "
        "(conservador — asume que la demanda pico se repite). Valores "
        "`<NA>` significan que no hubo ventas en ese corte."
    )

    if vista.empty:
        st.info("No hay SKUs que coincidan con los filtros.")
        return

    # Formato y orden de columnas para display
    display_cols = [
        "sku", "nombre", "tipo", "sub_rubro", "stock", "valor_stock",
        "venta_sem_ultimo_mes", "venta_sem_ultimos_3m", "venta_sem_mejor_mes",
        "semanas_ultimo_mes", "semanas_ultimos_3m", "semanas_mejor_mes",
        "critico",
    ]
    # Caches viejos previos a la versión que agregó `valor_stock` no
    # tienen la columna; degradar elegantemente.
    if "valor_stock" not in vista.columns:
        display_cols = [c for c in display_cols if c != "valor_stock"]

    # Styling: filas críticas con fondo rojo muy suave
    def _highlight_critico(row: pd.Series) -> list[str]:
        if row.get("critico", False):
            return ["background-color: #F8E4E0"] * len(row)
        return [""] * len(row)

    styled = (
        vista[display_cols]
        .style
        .apply(_highlight_critico, axis=1)
        .format(
            {
                "stock": "{:,.0f}",
                "valor_stock": "{:,.0f}",
                "venta_sem_ultimo_mes": "{:,.1f}",
                "venta_sem_ultimos_3m": "{:,.1f}",
                "venta_sem_mejor_mes": "{:,.1f}",
                "semanas_ultimo_mes": "{:.1f}",
                "semanas_ultimos_3m": "{:.1f}",
                "semanas_mejor_mes": "{:.1f}",
            }
        )
    )

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "sku": st.column_config.TextColumn("SKU", width="small"),
            "nombre": st.column_config.TextColumn("Nombre"),
            "tipo": st.column_config.TextColumn("Tipo", width="small"),
            "sub_rubro": st.column_config.TextColumn("Sub-rubro", width="small"),
            "stock": st.column_config.NumberColumn("Stock"),
            "valor_stock": st.column_config.NumberColumn(
                "Valor (UYU)",
                help="Stock × precio neto sin IVA.",
            ),
            "venta_sem_ultimo_mes": st.column_config.NumberColumn(
                "Vta/sem ú.mes",
                help="Venta semanal promedio últimos 30 días.",
            ),
            "venta_sem_ultimos_3m": st.column_config.NumberColumn(
                "Vta/sem 3m",
                help="Venta semanal promedio últimos 90 días (default para criticidad).",
            ),
            "venta_sem_mejor_mes": st.column_config.NumberColumn(
                "Vta/sem mejor-12m",
                help="Venta semanal promedio del mejor mes calendario de los últimos 12.",
            ),
            "semanas_ultimo_mes": st.column_config.NumberColumn(
                "sem ú.mes",
                help="Stock / venta semanal últimos 30 días.",
            ),
            "semanas_ultimos_3m": st.column_config.NumberColumn(
                "sem 3m",
                help="Stock / venta semanal últimos 90 días. El corte usado para marcar crítico.",
            ),
            "semanas_mejor_mes": st.column_config.NumberColumn(
                "sem mejor-12m",
                help="Stock / venta semanal del mejor mes (escenario más demandante).",
            ),
            "critico": st.column_config.CheckboxColumn(
                "Crítico",
                help="True si sem 3m < 4 semanas.",
            ),
        },
    )

    st.caption(
        f"Mostrando {len(vista):,} de {n_total:,} SKU(s). "
        f"Críticos visibles: {int(vista['critico'].sum()):,}."
    )
