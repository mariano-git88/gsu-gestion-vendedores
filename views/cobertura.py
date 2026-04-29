"""
views/cobertura.py — Métricas de cobertura de clientes.

Tres bloques:
  1. Cobertura general por vendedor (cuántos de sus clientes asignados
     recibieron al menos una venta FAC en el período).
  2. Cobertura desagregada por sub-rubro (de la cartera de cada vendedor,
     a cuántos les vendió algo de cada sub-rubro).
  3. Cobertura para un SKU específico (selector).

Recordar la regla del manual: las NCF NUNCA cuentan para cobertura,
ni siquiera las que son devoluciones reales. Esa lógica vive en
metrics.py — esta vista solo muestra los resultados.
"""

import pandas as pd
import streamlit as st

import metrics


def render(
    df_sem: pd.DataFrame,
    df_mes: pd.DataFrame,
    df_clientes: pd.DataFrame,
    health_sem: dict | None = None,
    health_mes: dict | None = None,
    df_clientes_act: pd.DataFrame | None = None,
) -> None:
    """
    Args:
        df_sem, df_mes, df_clientes: ver views/resumen.py.
        health_sem, health_mes: por consistencia de firma con resumen.py.
            Esta vista no los usa actualmente.
        df_clientes_act: cartera depurada (clientes activos 12m). Se usa
            para todas las métricas de cobertura/penetración. La sub-
            sección "Clientes inactivos" usa la cartera completa.
    """
    del health_sem, health_mes  # actualmente no se usan acá

    if df_clientes_act is None:
        df_clientes_act = df_clientes

    st.subheader("Cobertura de clientes")

    # ----- Selector de timeframe -----
    # "Trimestre" aparece sólo si el modo API sincronizó un trimestre
    # (session_state.df_tri). En Modo Manual el trimestre no existe.
    df_tri = st.session_state.get("df_tri")
    opciones = ["Semana", "Mes"]
    if df_tri is not None and not df_tri.empty:
        opciones.append("Trimestre")

    timeframe = st.radio(
        "Período",
        options=opciones,
        horizontal=True,
        key="cobertura_tf",
    )
    if timeframe == "Semana":
        df = df_sem
    elif timeframe == "Mes":
        df = df_mes
    else:
        df = df_tri

    if df.empty:
        st.info("No hay datos para el período seleccionado.")
        # Las secciones de histórico (dormidos / nuevos) no dependen
        # del período seleccionado; saltamos a ellas.
        _render_secciones_historicas(df_mes, df_clientes)
        return

    # ----- Bloque 1: cobertura general -----
    st.markdown("### Cobertura general por vendedor")
    st.caption(
        "De los clientes asignados a cada vendedor, cuántos recibieron "
        "al menos una venta FAC en el período. Las NCF no cuentan para "
        "esta métrica. **Conc. 80%**: cuántos clientes concentran el 80% "
        "de la venta del vendedor (cuanto más bajo, más concentrado el "
        "riesgo). **Mix top-3**: los 3 sub-rubros con mayor participación "
        "en la venta del vendedor."
    )
    cob = metrics.cobertura_por_vendedor(df, df_clientes_act)
    if cob.empty:
        st.info("Sin datos de cobertura general.")
    else:
        st.dataframe(
            cob.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
            column_config={
                "concentracion_80": st.column_config.NumberColumn(
                    "Conc. 80%",
                    help="N clientes que concentran el 80% de la venta",
                ),
                "mix_top3": st.column_config.TextColumn(
                    "Mix top-3",
                    help="Los 3 sub-rubros con mayor % de venta",
                ),
            },
        )

    # ----- Bloque 2: cobertura por sub-rubro -----
    st.divider()
    st.markdown("### Cobertura por sub-rubro")
    st.caption(
        "Para cada (vendedor, sub-rubro), cuántos clientes asignados al "
        "vendedor compraron al menos un producto de ese sub-rubro."
    )
    cob_sr = metrics.cobertura_por_sub_rubro(df, df_clientes_act)
    if cob_sr.empty:
        st.info("Sin datos de cobertura por sub-rubro.")
    else:
        st.dataframe(
            cob_sr.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )

    # ----- Bloque 3: cobertura por SKU específico -----
    st.divider()
    st.markdown("### Cobertura por SKU específico")
    st.caption(
        "Para un SKU dado: cuántos de los clientes asignados a cada "
        "vendedor lo compraron en el período (con FAC)."
    )

    skus = sorted(df["sku"].dropna().astype(str).unique().tolist())
    if not skus:
        st.info("Sin SKUs para mostrar en el período seleccionado.")
        return

    sku_sel = st.selectbox(
        "Seleccionar SKU",
        options=skus,
        key="cobertura_sku_sel",
    )
    cob_sku = metrics.cobertura_por_sku(df, df_clientes_act, sku_sel)
    if cob_sku.empty:
        st.info("Sin datos de cobertura para el SKU seleccionado.")
    else:
        st.dataframe(
            cob_sku.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )

    # ----- Bloque 4: clientes que no compraron este SKU en el mes -----
    # Esta sección SIEMPRE usa df_mes (lo que va del mes), independiente
    # del selector de Semana/Mes de arriba. Reutiliza el sku_sel del bloque
    # anterior para evitar duplicar UI.
    st.divider()
    st.markdown("### Clientes que NO compraron este SKU en el mes")
    st.caption(
        "De la cartera asignada a cada vendedor, los clientes que NO "
        "recibieron una venta tipo FAC del SKU seleccionado en lo que va "
        "del mes. **Siempre sobre el mes**, independiente del selector "
        "de período de arriba. El criterio es estricto: si el cliente le "
        "compró el SKU a otro vendedor distinto al asignado, igual aparece."
    )

    # Edge case: el SKU seleccionado puede no existir en df_mes
    # (por ejemplo, si arriba está seleccionada Semana y se eligió un
    # SKU que solo facturó esta semana). Avisar antes de calcular.
    if sku_sel not in set(df_mes["sku"].dropna().astype(str).unique()):
        st.info(
            f"El SKU `{sku_sel}` no aparece en lo que va del mes. "
            "No se puede calcular la lista de no-compradores sobre el mes."
        )
    else:
        no_compradores = metrics.clientes_sin_compra_sku(df_mes, df_clientes_act, sku_sel)
        if no_compradores.empty:
            st.success(
                "Todos los clientes en cartera ya compraron este SKU "
                "este mes (de su vendedor asignado)."
            )
        else:
            st.dataframe(
                no_compradores,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"Total: {len(no_compradores)} clientes sin compra del SKU.")

    _render_secciones_historicas(df_mes, df_clientes)


def _render_secciones_historicas(
    df_mes: pd.DataFrame, df_clientes: pd.DataFrame
) -> None:
    """
    Renderiza los bloques que dependen del histórico 12m:
      - Clientes dormidos (últimos 90 días)
      - Clientes nuevos del mes (sin compras previas en 12m)

    Independientes del selector de período de la tab (se calculan
    siempre sobre el histórico + `df_mes`).
    """
    # ----- Bloque 5: Clientes dormidos (requiere histórico 12m) -----
    st.divider()
    st.markdown("### Clientes dormidos")
    st.caption(
        "Clientes en cartera que **no reciben una FAC de su vendedor "
        "asignado desde hace más de 90 días**. Incluye también los que "
        "nunca compraron (caso `Nunca`). La lista se calcula sobre el "
        "histórico de 12 meses — cargarlo desde la sidebar si no está."
    )
    df_hist12 = st.session_state.get("df_hist12")
    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para ver esta sección, cargá el **histórico de 12 meses** "
            "desde la sidebar (botón 'Cargar histórico')."
        )
    else:
        dormidos = metrics.clientes_dormidos(df_hist12, df_clientes)
        if dormidos.empty:
            st.success(
                "No hay clientes dormidos — todos los clientes en cartera "
                "compraron en los últimos 90 días."
            )
        else:
            _dormidos_vendedores = sorted(
                dormidos["vendedor"].dropna().astype(str).unique().tolist()
            )
            _dormidos_opts = ["(Todos)"] + _dormidos_vendedores
            _dormidos_sel = st.selectbox(
                "Filtrar por vendedor",
                options=_dormidos_opts,
                key="dormidos_vendedor_sel",
            )
            vista = (
                dormidos
                if _dormidos_sel == "(Todos)"
                else dormidos[dormidos["vendedor"] == _dormidos_sel]
            )
            # Display: "Nunca" para los clientes sin compras
            vista = vista.copy()
            vista["dias_sin_comprar"] = vista["dias_sin_comprar"].apply(
                lambda v: "Nunca" if pd.isna(v) else int(v)
            )
            vista["ultima_fecha_compra"] = vista["ultima_fecha_compra"].apply(
                lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
            )
            st.dataframe(
                vista,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"Total: {len(vista)} cliente(s) dormido(s).")

    # ----- Bloque 6: Clientes nuevos del mes -----
    st.divider()
    st.markdown("### Clientes nuevos del mes")
    st.caption(
        "Clientes con primera compra (FAC de su vendedor asignado) en "
        "lo que va del mes y **sin compras en los 12 meses previos**. "
        "Requiere el histórico cargado."
    )
    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para ver esta sección, cargá el **histórico de 12 meses** "
            "desde la sidebar."
        )
    else:
        nuevos = metrics.clientes_nuevos(df_mes, df_hist12, df_clientes)
        if nuevos.empty:
            st.info(
                "No hay clientes nuevos en el mes actual (según el "
                "criterio 'sin compras en los 12 meses previos')."
            )
        else:
            _nuevos_vendedores = sorted(
                nuevos["vendedor"].dropna().astype(str).unique().tolist()
            )
            _nuevos_opts = ["(Todos)"] + _nuevos_vendedores
            _nuevos_sel = st.selectbox(
                "Filtrar por vendedor",
                options=_nuevos_opts,
                key="nuevos_vendedor_sel",
            )
            vista_nuevos = (
                nuevos
                if _nuevos_sel == "(Todos)"
                else nuevos[nuevos["vendedor"] == _nuevos_sel]
            )
            vista_nuevos = vista_nuevos.copy()
            vista_nuevos["primera_compra"] = vista_nuevos["primera_compra"].apply(
                lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
            )
            st.dataframe(
                vista_nuevos.style.format({"monto_mes": "{:,.0f}"}),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"Total: {len(vista_nuevos)} cliente(s) nuevo(s).")

    # ----- Bloque 7: Clientes inactivos (12m, requiere histórico) -----
    st.divider()
    st.markdown("### Clientes inactivos (sin compra en 12m)")
    st.caption(
        "Clientes en cartera **sin FAC de ningún vendedor en los últimos "
        "12 meses** (incluye los que nunca compraron). El toggle de la "
        "sidebar **'Excluir clientes inactivos'** los saca del denominador "
        "de cobertura/penetración. Útil para depurar cartera o exportar "
        "una lista para revisar baja."
    )
    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para ver esta sección, cargá el **histórico de 12 meses** "
            "desde la sidebar."
        )
        return

    inactivos = metrics.clientes_inactivos_12m(df_clientes, df_hist12)
    if inactivos.empty:
        st.success(
            "No hay clientes inactivos — toda la cartera tuvo al menos "
            "una FAC en los últimos 12 meses."
        )
        return

    _vendedores_inact = sorted(
        inactivos["vendedor_asignado"].dropna().astype(str).unique().tolist()
    )
    _sel_inact = st.selectbox(
        "Filtrar por vendedor asignado",
        options=["(Todos)"] + _vendedores_inact,
        key="inactivos_vendedor_sel",
    )
    vista_inact = (
        inactivos
        if _sel_inact == "(Todos)"
        else inactivos[inactivos["vendedor_asignado"] == _sel_inact]
    )
    vista_inact = vista_inact.copy()
    vista_inact["fecha_ultima_compra"] = vista_inact[
        "fecha_ultima_compra"
    ].apply(
        lambda d: "Nunca compró" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
    )
    st.dataframe(
        vista_inact.style.format({"monto_12m": "{:,.0f}"}),
        use_container_width=True,
        hide_index=True,
        column_config={
            "documento": st.column_config.TextColumn("Documento"),
            "razon_social": st.column_config.TextColumn("Razón Social"),
            "vendedor_asignado": st.column_config.TextColumn("Vendedor asignado"),
            "fecha_ultima_compra": st.column_config.TextColumn("Última compra"),
            "monto_12m": st.column_config.NumberColumn(
                "Monto 12m (UYU)",
                help="Suma de FAC del cliente en los últimos 12 meses (debería ser 0).",
            ),
        },
    )
    st.caption(f"Total: {len(vista_inact)} cliente(s) inactivo(s).")

    # Export del listado de inactivos como xlsx
    csv_buf = vista_inact.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Descargar inactivos.csv",
        data=csv_buf,
        file_name="clientes_inactivos_12m.csv",
        mime="text/csv",
        use_container_width=False,
    )
