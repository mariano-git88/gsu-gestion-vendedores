"""
metrics.py — Cálculo de ventas y cobertura para el dashboard.

Recibe los DataFrames preparados por `transforms.py` (con todas las
clasificaciones aplicadas y columnas en snake_case) y devuelve
DataFrames listos para mostrar en las vistas Streamlit.

Funciones puras: no importa streamlit, no escribe a disco. El cache de
UI se aplica en `app.py` envolviendo estas funciones con
`@st.cache_data`.

Reglas críticas implementadas (claude.md.txt, sección "Métricas del
dashboard"):

  - **Monto/unidades**: se calcula sobre TODAS las filas (FAC + NCF con
    sku). Las NCF tienen `monto` y `unidades` negativos y netean
    correctamente con las FAC. Eso es el comportamiento deseado.

  - **Cobertura de clientes**: SOLO cuenta filas con `tipo == 'FAC'`.
    Las NCF nunca cuentan para cobertura, ni siquiera las devoluciones
    reales. Es regla explícita del manual.

  - **Match cliente ↔ vendedor para cobertura**: el vendedor de la
    OPERACIÓN tiene que ser el mismo que el vendedor ASIGNADO al
    cliente en cartera. Si V1 tiene a C1 en cartera pero V2 le facturó
    a C1, ese cliente no cuenta como "con venta" para V1 (V1 no le
    vendió) ni para V2 (no está en su cartera). Es matching estricto
    via merge por `(vendedor, documento)`.
"""

from __future__ import annotations

import pandas as pd

TIPO_FAC = "FAC"


# =====================================================================
# Helpers internos
# =====================================================================

def _cartera_unica(df_clientes: pd.DataFrame) -> pd.DataFrame:
    """
    Devuelve los pares únicos `(vendedor, documento)` del maestro de
    clientes, sin nulos. Es el universo contra el que se evalúan
    todas las métricas de cobertura.
    """
    return (
        df_clientes[["vendedor", "documento"]]
        .dropna()
        .drop_duplicates()
        .reset_index(drop=True)
    )


def _clientes_asignados_por_vendedor(df_clientes: pd.DataFrame) -> pd.DataFrame:
    """
    Para cada vendedor, count de clientes (documentos únicos) en cartera.
    Devuelve DataFrame con columnas: vendedor, clientes_asignados.
    """
    return (
        df_clientes.dropna(subset=["vendedor", "documento"])
        .groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_asignados")
    )


def _fac_en_cartera_propia(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Subset de facturación que cumple AMBAS condiciones:
      1. tipo == 'FAC' (regla del manual: NCF no cuentan para cobertura).
      2. el (vendedor de la operación, documento del cliente) está en la
         cartera del MISMO vendedor en clientes.xlsx.

    Es el insumo común de las tres funciones de cobertura.
    """
    df_fac = df_fc[df_fc["tipo"] == TIPO_FAC]
    cartera = _cartera_unica(df_clientes)
    return df_fac.merge(cartera, on=["vendedor", "documento"], how="inner")


def _calcular_pct_cobertura(con_venta: int, asignados: int) -> float:
    """Cobertura = con_venta / asignados, en % redondeado a 2 decimales."""
    if asignados == 0:
        return 0.0
    return round(con_venta / asignados * 100, 2)


# =====================================================================
# VENTAS
# =====================================================================

def ventas_por_vendedor(df_fc: pd.DataFrame) -> pd.DataFrame:
    """
    Totales de venta por vendedor.

    Suma `monto` y `unidades` agrupando por `vendedor`. Las NCF
    (devoluciones reales) se incluyen en la suma con sus valores
    negativos, neteando con las FAC.

    Agrega también `tickets` (count de comprobantes distintos vía
    `id_comprobante`) y `ticket_promedio` = monto_total / tickets.
    Si `id_comprobante` no existe en el input (casos viejos), devuelve
    `tickets = 0` y `ticket_promedio = 0.0` — nunca rompe.

    Devuelve DataFrame con columnas:
      vendedor, monto_total, unidades_totales, tickets, ticket_promedio

    Ordenado por monto_total descendente.
    """
    base_cols = [
        "vendedor", "monto_total", "unidades_totales",
        "tickets", "ticket_promedio",
    ]
    if df_fc.empty:
        return pd.DataFrame(columns=base_cols)

    agg = (
        df_fc.groupby("vendedor", dropna=False)
        .agg(monto_total=("monto", "sum"), unidades_totales=("unidades", "sum"))
        .reset_index()
    )

    if "id_comprobante" in df_fc.columns:
        tickets = (
            df_fc.groupby("vendedor", dropna=False)["id_comprobante"]
            .nunique()
            .reset_index(name="tickets")
        )
        agg = agg.merge(tickets, on="vendedor", how="left")
    else:
        agg["tickets"] = 0

    agg["tickets"] = agg["tickets"].fillna(0).astype(int)
    agg["ticket_promedio"] = agg.apply(
        lambda r: round(r["monto_total"] / r["tickets"], 2)
        if r["tickets"] > 0 else 0.0,
        axis=1,
    )

    return (
        agg[base_cols]
        .sort_values("monto_total", ascending=False)
        .reset_index(drop=True)
    )


def ventas_por_vendedor_y_sub_rubro(df_fc: pd.DataFrame) -> pd.DataFrame:
    """
    Desglose de ventas por (vendedor, sub_rubro).

    Útil para pivotar a una matriz vendedor × sub_rubro en la vista.
    Las NCF netean en su sub_rubro correspondiente (mismo SKU).

    Devuelve DataFrame con columnas:
      vendedor, sub_rubro, monto, unidades
    """
    if df_fc.empty:
        return pd.DataFrame(columns=["vendedor", "sub_rubro", "monto", "unidades"])

    return (
        df_fc.groupby(["vendedor", "sub_rubro"], dropna=False)
        .agg(monto=("monto", "sum"), unidades=("unidades", "sum"))
        .reset_index()
        .sort_values(["vendedor", "monto"], ascending=[True, False])
        .reset_index(drop=True)
    )


def filtrar_por_sku(df_fc: pd.DataFrame, sku: str) -> pd.DataFrame:
    """
    Filtra el DataFrame de facturación a las filas de un SKU exacto.

    Útil para componer otras métricas: por ejemplo, llamar a
    `ventas_por_vendedor(filtrar_por_sku(df, 'PROD123'))` da la venta
    de ese SKU por vendedor.
    """
    return df_fc[df_fc["sku"] == sku].copy().reset_index(drop=True)


def filtrar_por_combo(
    df_fc: pd.DataFrame, sku_combo: str | None = None
) -> pd.DataFrame:
    """
    Filtra a filas de combos.

    - Sin argumento: todas las filas con `sub_rubro == 'COMBO'`.
    - Con `sku_combo`: solo el combo específico.

    Notar que después de `transforms.classify_skus()`, los combos
    quedan marcados con `sub_rubro = 'COMBO'`. No depende de listar
    SKUs uno por uno.
    """
    df = df_fc[df_fc["sub_rubro"] == "COMBO"]
    if sku_combo is not None:
        df = df[df["sku"] == sku_combo]
    return df.copy().reset_index(drop=True)


# =====================================================================
# COBERTURA
# =====================================================================

def cobertura_por_vendedor(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Cobertura general por vendedor.

    Para cada vendedor en cartera, calcula:
      - clientes_asignados: cuántos clientes distintos tiene en cartera.
      - clientes_con_venta: cuántos de esos clientes recibieron al
        menos una venta tipo FAC del MISMO vendedor en el período.
      - cobertura_pct: clientes_con_venta / clientes_asignados (%).
      - concentracion_80: cuántos clientes concentran el 80% del monto
        vendido por el vendedor (Pareto propio). Cuanto más bajo, más
        concentrado — señal de riesgo. Si el vendedor no tiene ventas,
        el valor es 0.
      - mix_top3: string con los 3 sub-rubros más vendidos y su %,
        formato "A 85% · BA 10% · resto 5%".

    Solo se reportan vendedores que aparecen en `clientes.xlsx` (los
    que tienen cartera). Vendedores que facturan pero no tienen
    cartera (los huérfanos que detecta `check_vendedores_sin_cartera`)
    NO aparecen acá — se reportan en el panel de salud.

    NCF nunca cuentan para la cobertura; para `concentracion_80` y
    `mix_top3` tampoco, por consistencia con la "foto de venta"
    que mide esas dos (Pareto y mix son sobre ventas positivas).
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)
    fac_en_cartera = _fac_en_cartera_propia(df_fc, df_clientes)

    con_venta = (
        fac_en_cartera.groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta")
    )

    result = asignados.merge(con_venta, on="vendedor", how="left")
    result["clientes_con_venta"] = (
        result["clientes_con_venta"].fillna(0).astype(int)
    )
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta"], r["clientes_asignados"]
        ),
        axis=1,
    )

    # --- Concentración Pareto por vendedor ---
    concentracion = _concentracion_80_por_vendedor(fac_en_cartera)
    result = result.merge(concentracion, on="vendedor", how="left")
    result["concentracion_80"] = (
        result["concentracion_80"].fillna(0).astype(int)
    )

    # --- Mix top-3 de sub-rubro por vendedor ---
    mix = _mix_top3_por_vendedor(fac_en_cartera)
    result = result.merge(mix, on="vendedor", how="left")
    result["mix_top3"] = result["mix_top3"].fillna("—")

    return result.sort_values("cobertura_pct", ascending=False).reset_index(drop=True)


def _concentracion_80_por_vendedor(
    fac_en_cartera: pd.DataFrame,
) -> pd.DataFrame:
    """
    Para cada vendedor, cuántos clientes concentran el 80% de su venta.

    Se calcula sobre `fac_en_cartera` (FAC del vendedor en su propia
    cartera — match estricto). Ordena clientes por monto descendente,
    acumula, y cuenta cuántos hacen falta para llegar al 80%.

    Devuelve DataFrame con columnas: vendedor, concentracion_80.
    """
    if fac_en_cartera.empty:
        return pd.DataFrame(columns=["vendedor", "concentracion_80"])

    por_cliente = (
        fac_en_cartera.groupby(["vendedor", "documento"], as_index=False)["monto"]
        .sum()
    )
    # Descartar clientes con monto <= 0 (puede pasar si NCF > FAC por cliente).
    por_cliente = por_cliente[por_cliente["monto"] > 0]

    resultados = []
    for vendedor, grupo in por_cliente.groupby("vendedor"):
        g = grupo.sort_values("monto", ascending=False).reset_index(drop=True)
        total = float(g["monto"].sum())
        if total <= 0:
            resultados.append({"vendedor": vendedor, "concentracion_80": 0})
            continue
        acumulado = g["monto"].cumsum() / total
        # +1 porque iloc 0 es el primer cliente y ya representa X%.
        n = int((acumulado < 0.8).sum()) + 1
        resultados.append({"vendedor": vendedor, "concentracion_80": n})

    return pd.DataFrame(resultados)


def _mix_top3_por_vendedor(
    fac_en_cartera: pd.DataFrame,
) -> pd.DataFrame:
    """
    Para cada vendedor, string con los 3 sub-rubros más vendidos y su %.

    Formato: "A 85% · BA 10% · resto 5%". Si hay <3 sub-rubros, muestra
    los que haya. Si el total es 0, devuelve string vacío.

    Devuelve DataFrame con columnas: vendedor, mix_top3.
    """
    if fac_en_cartera.empty:
        return pd.DataFrame(columns=["vendedor", "mix_top3"])

    por_sr = (
        fac_en_cartera.groupby(["vendedor", "sub_rubro"], as_index=False)["monto"]
        .sum()
    )

    resultados = []
    for vendedor, grupo in por_sr.groupby("vendedor"):
        g = grupo[grupo["monto"] > 0].sort_values("monto", ascending=False)
        total = float(g["monto"].sum())
        if total <= 0:
            resultados.append({"vendedor": vendedor, "mix_top3": ""})
            continue
        top3 = g.head(3)
        partes = [
            f"{row.sub_rubro} {row.monto / total * 100:.0f}%"
            for row in top3.itertuples(index=False)
        ]
        resto_monto = total - float(top3["monto"].sum())
        if resto_monto > 0:
            partes.append(f"resto {resto_monto / total * 100:.0f}%")
        resultados.append({"vendedor": vendedor, "mix_top3": " · ".join(partes)})

    return pd.DataFrame(resultados)


def cobertura_por_sub_rubro(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Cobertura desagregada por sub_rubro.

    Para cada (vendedor, sub_rubro), cuántos clientes asignados al
    vendedor recibieron al menos una venta FAC de un producto de ese
    sub_rubro (del mismo vendedor).

    El denominador (`clientes_asignados`) es el TOTAL de clientes en
    cartera del vendedor, NO los que tienen venta de ese sub_rubro.
    Esto es a propósito: la métrica responde "del total de mi cartera,
    a cuántos les vendí algo de este sub_rubro".

    Devuelve DataFrame con columnas:
      vendedor, sub_rubro, clientes_asignados,
      clientes_con_venta_sub_rubro, cobertura_pct
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)
    fac_en_cartera = _fac_en_cartera_propia(df_fc, df_clientes)

    con_venta_sr = (
        fac_en_cartera.groupby(["vendedor", "sub_rubro"])["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta_sub_rubro")
    )

    result = con_venta_sr.merge(asignados, on="vendedor", how="left")
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta_sub_rubro"], r["clientes_asignados"]
        ),
        axis=1,
    )
    result = result[
        [
            "vendedor",
            "sub_rubro",
            "clientes_asignados",
            "clientes_con_venta_sub_rubro",
            "cobertura_pct",
        ]
    ]
    return result.sort_values(
        ["vendedor", "cobertura_pct"], ascending=[True, False]
    ).reset_index(drop=True)


def clientes_sin_compra_sku(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame, sku: str
) -> pd.DataFrame:
    """
    Lista de clientes en cartera que NO recibieron una venta tipo FAC del
    `sku` específico de su vendedor asignado en el período cubierto por
    `df_fc`.

    Es el complemento de `cobertura_por_sku`: si esa función dice "el
    vendedor V cubre 60% de su cartera con el SKU X", esta función te da
    el 40% restante — los clientes específicos a los que ese vendedor
    todavía no les vendió el producto.

    Match estricto (consistente con `cobertura_por_sku`): un cliente
    cuenta como "comprador" SOLO si su vendedor asignado le vendió el
    SKU. Si lo compró a otro vendedor, sigue apareciendo en esta lista
    como "no comprador" (la oportunidad de venta para el vendedor
    asignado sigue abierta).

    NCF nunca cuentan para esta métrica.

    Devuelve DataFrame con columnas:
      vendedor, documento, razon_social

    Ordenado por vendedor y razón social, para que sea cómodo de leer
    en una reunión.
    """
    # Cartera única por (vendedor, documento), trayendo razón social
    cartera = (
        df_clientes[["vendedor", "documento", "razon_social"]]
        .dropna(subset=["vendedor", "documento"])
        .drop_duplicates(subset=["vendedor", "documento"])
    )

    # Compradores estrictos: pares (vendedor_op, documento) con al menos
    # una FAC del SKU específico
    df_fac_sku = df_fc[(df_fc["tipo"] == TIPO_FAC) & (df_fc["sku"] == sku)]
    compradores = df_fac_sku[["vendedor", "documento"]].drop_duplicates()

    # Anti-join: cartera menos compradores. Usamos un marker temporal
    # para identificar las filas sin match.
    merged = cartera.merge(
        compradores.assign(__match=1),
        on=["vendedor", "documento"],
        how="left",
    )
    no_compradores = merged[merged["__match"].isna()].drop(columns="__match")

    return (
        no_compradores[["vendedor", "documento", "razon_social"]]
        .sort_values(["vendedor", "razon_social"], na_position="last")
        .reset_index(drop=True)
    )


def cobertura_por_sku(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame, sku: str
) -> pd.DataFrame:
    """
    Cobertura para un SKU específico.

    Para cada vendedor con cartera, cuántos de sus clientes asignados
    compraron el SKU (con FAC) en el período.

    Devuelve DataFrame con columnas:
      vendedor, clientes_asignados, clientes_con_venta_sku, cobertura_pct
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)

    # Filtrar fc al SKU específico, en FAC, en cartera del propio vendedor
    df_fac_sku = df_fc[(df_fc["tipo"] == TIPO_FAC) & (df_fc["sku"] == sku)]
    cartera = _cartera_unica(df_clientes)
    fac_sku_cartera = df_fac_sku.merge(
        cartera, on=["vendedor", "documento"], how="inner"
    )

    con_venta_sku = (
        fac_sku_cartera.groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta_sku")
    )

    result = asignados.merge(con_venta_sku, on="vendedor", how="left")
    result["clientes_con_venta_sku"] = (
        result["clientes_con_venta_sku"].fillna(0).astype(int)
    )
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta_sku"], r["clientes_asignados"]
        ),
        axis=1,
    )
    return result.sort_values("cobertura_pct", ascending=False).reset_index(drop=True)


# =====================================================================
# ANÁLISIS PROFUNDO (vista "Análisis")
# =====================================================================

def penetracion_por_sub_rubro_pivot(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Devuelve la matriz de penetración por sub-rubro: vendedores en filas,
    sub-rubros en columnas, % de clientes cubiertos en cada celda.

    Es una versión pivotada de `cobertura_por_sub_rubro` pensada para
    mostrarse como tabla con styling de heatmap (rojo→verde según el %).
    Solo incluye sub-rubros que aparecen en el período (no rellena con
    columnas vacías).

    Match estricto, NCF no cuentan — hereda todas las reglas de
    `cobertura_por_sub_rubro`.

    Devuelve:
        DataFrame con index=vendedor, columns=sub_rubro, valores=%.
        Si no hay datos, devuelve DataFrame vacío.
    """
    cob_sr = cobertura_por_sub_rubro(df_fc, df_clientes)
    if cob_sr.empty:
        return pd.DataFrame()

    pivot = cob_sr.pivot(
        index="vendedor",
        columns="sub_rubro",
        values="cobertura_pct",
    ).fillna(0.0)

    # Ordenar columnas: las de mayor cobertura promedio primero (más
    # útil para el ojo cuando hay muchos sub-rubros).
    pivot = pivot[pivot.mean().sort_values(ascending=False).index]

    return pivot


def heatmap_cliente_sub_rubro(
    df_fc: pd.DataFrame,
    df_clientes: pd.DataFrame,
    vendedor: str,
    top_n: int = 30,
) -> pd.DataFrame:
    """
    Heatmap cliente × sub-rubro para un vendedor específico.

    Para los TOP N clientes del vendedor (por monto comprado en el
    período), devuelve una matriz con sub-rubros en columnas y monto
    vendido en cada celda. Permite ver de un vistazo qué sub-rubros
    "le compra cada cliente" y dónde están los huecos para hacer
    cross-sell.

    Match estricto: solo cuenta filas con `tipo == FAC` Y donde
    `(vendedor_op, documento)` matchea con la cartera del vendedor.
    Las ventas a clientes de otros vendedores no aparecen.

    Args:
        vendedor: el email del vendedor cuya cartera se va a explorar.
        top_n: cuántos clientes mostrar (los de mayor monto del período).

    Devuelve un DataFrame con columnas:
        razon_social, documento, [sub_rubro_1, sub_rubro_2, ...]

    Cada celda de sub_rubro es el monto vendido al cliente en ese
    sub_rubro durante el período. Las celdas vacías (sin compra) son 0.

    Si el vendedor no tiene clientes con compras → DataFrame vacío.
    """
    # Cartera del vendedor (solo documentos, sin razon_social para evitar
    # conflicto de nombres en el merge — la traemos aparte vía dict)
    cartera_v = (
        df_clientes[df_clientes["vendedor"] == vendedor]
        .dropna(subset=["documento"])
        .drop_duplicates(subset="documento")
    )
    if cartera_v.empty:
        return pd.DataFrame()

    docs_cartera = set(cartera_v["documento"])
    rs_map = cartera_v.set_index("documento")["razon_social"].to_dict()

    # FAC del vendedor a clientes en su cartera
    df_fac = df_fc[
        (df_fc["vendedor"] == vendedor)
        & (df_fc["tipo"] == TIPO_FAC)
        & (df_fc["documento"].isin(docs_cartera))
    ]
    if df_fac.empty:
        return pd.DataFrame()

    # Sumar monto por (cliente, sub_rubro)
    grouped = df_fac.groupby(
        ["documento", "sub_rubro"], as_index=False
    )["monto"].sum()

    # Pivot a matriz cliente × sub_rubro
    pivot = (
        grouped.pivot(index="documento", columns="sub_rubro", values="monto")
        .fillna(0)
    )

    # Top N clientes por monto total
    pivot["__total"] = pivot.sum(axis=1)
    top = pivot.sort_values("__total", ascending=False).head(top_n).drop(columns="__total")

    # Reordenar columnas: las de mayor monto total primero
    if len(top) > 0:
        col_order = top.sum().sort_values(ascending=False).index
        top = top[col_order]

    # Agregar razon_social como primera columna
    top = top.reset_index()
    top.insert(0, "razon_social", top["documento"].map(rs_map))

    return top


DIAS_SEMANA_ES = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}


def ventas_por_dia_semana(
    df_fc: pd.DataFrame,
    vendedor: str | None = None,
) -> pd.DataFrame:
    """
    Ventas agrupadas por día de la semana (lunes a domingo, en español).

    Args:
        vendedor: si se pasa, filtra al vendedor específico. None = todos.

    Suma `monto` y count de comprobantes distintos por día. Las NCF
    netean; los días que queden en 0 aparecen igual con valor 0 para
    que el bar chart no tenga huecos.

    Devuelve DataFrame con columnas:
      dia, dia_orden, monto, tickets
    ordenado por `dia_orden` (Lunes primero).
    """
    cols = ["dia", "dia_orden", "monto", "tickets"]
    if df_fc.empty:
        return pd.DataFrame(columns=cols)

    df = df_fc.copy()
    if vendedor is not None:
        df = df[df["vendedor"] == vendedor]
        if df.empty:
            return pd.DataFrame(columns=cols)

    fechas = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.assign(__wd=fechas.dt.weekday)

    monto = (
        df.groupby("__wd", dropna=True)["monto"]
        .sum()
        .reset_index(name="monto")
    )

    if "id_comprobante" in df.columns:
        tickets = (
            df.groupby("__wd", dropna=True)["id_comprobante"]
            .nunique()
            .reset_index(name="tickets")
        )
        out = monto.merge(tickets, on="__wd", how="left")
    else:
        out = monto.assign(tickets=0)

    # Rellenar los 7 días aunque no haya datos, para que el gráfico sea prolijo.
    full = pd.DataFrame({"__wd": list(range(7))})
    out = full.merge(out, on="__wd", how="left").fillna(0)
    out["dia"] = out["__wd"].map(DIAS_SEMANA_ES)
    out["dia_orden"] = out["__wd"].astype(int)
    out["monto"] = out["monto"].astype(float).round(2)
    out["tickets"] = out["tickets"].astype(int)

    return out[cols].sort_values("dia_orden").reset_index(drop=True)


def ventas_por_quincena(
    df_fc: pd.DataFrame,
    vendedor: str | None = None,
) -> pd.DataFrame:
    """
    Ventas agrupadas por quincena dentro del mes: "1-15" vs "16-fin".

    Útil para detectar patrones de cierre (vendedores que concentran
    toda la venta en la última quincena "empujando el cierre" vs los
    que venden parejo).

    Args:
        vendedor: si se pasa, filtra al vendedor específico. None = todos.

    Devuelve DataFrame con columnas:
      quincena, monto, tickets
    ordenado con "1-15" primero.
    """
    cols = ["quincena", "monto", "tickets"]
    if df_fc.empty:
        return pd.DataFrame(columns=cols)

    df = df_fc.copy()
    if vendedor is not None:
        df = df[df["vendedor"] == vendedor]
        if df.empty:
            return pd.DataFrame(columns=cols)

    fechas = pd.to_datetime(df["fecha"], errors="coerce")
    dia_mes = fechas.dt.day
    quincena = dia_mes.where(dia_mes.isna(), (dia_mes <= 15).map({True: "1-15", False: "16-fin"}))
    df = df.assign(__q=quincena)

    monto = (
        df.groupby("__q", dropna=True)["monto"]
        .sum()
        .reset_index(name="monto")
    )

    if "id_comprobante" in df.columns:
        tickets = (
            df.groupby("__q", dropna=True)["id_comprobante"]
            .nunique()
            .reset_index(name="tickets")
        )
        out = monto.merge(tickets, on="__q", how="left")
    else:
        out = monto.assign(tickets=0)

    # Asegurar las dos quincenas en el output, aunque estén vacías.
    full = pd.DataFrame({"__q": ["1-15", "16-fin"]})
    out = full.merge(out, on="__q", how="left").fillna(0)
    out["quincena"] = out["__q"]
    out["monto"] = out["monto"].astype(float).round(2)
    out["tickets"] = out["tickets"].astype(int)

    return out[cols].reset_index(drop=True)


def pareto_clientes(
    df_fc: pd.DataFrame,
    df_clientes: pd.DataFrame,
    vendedor: str | None = None,
) -> pd.DataFrame:
    """
    Análisis de Pareto de clientes.

    Ordena los clientes por monto descendente y calcula el porcentaje
    individual y acumulado. Permite identificar el "top vital" — los
    pocos clientes que generan la mayor parte de la venta.

    Args:
        vendedor: si es None, calcula el Pareto global (todos los
            vendedores juntos). Si es un email específico, calcula
            solo sobre la cartera de ese vendedor.

    Match estricto, NCF no cuentan.

    Devuelve un DataFrame con columnas:
        - vendedor (solo si vendedor=None, para identificar quién atiende cada cliente)
        - documento
        - razon_social
        - monto
        - pct_individual
        - pct_acumulado

    Si no hay datos → DataFrame vacío.
    """
    df_fac = df_fc[df_fc["tipo"] == TIPO_FAC]
    cartera_idx = (
        df_clientes.dropna(subset=["vendedor", "documento"])
        .drop_duplicates(subset=["vendedor", "documento"])
    )
    rs_map = (
        cartera_idx.drop_duplicates(subset="documento")
        .set_index("documento")["razon_social"]
        .to_dict()
    )

    if vendedor is not None:
        cartera_idx = cartera_idx[cartera_idx["vendedor"] == vendedor]
        df_fac = df_fac[df_fac["vendedor"] == vendedor]

    cartera_keys = cartera_idx[["vendedor", "documento"]]
    fac_en_cartera = df_fac.merge(
        cartera_keys, on=["vendedor", "documento"], how="inner"
    )

    if fac_en_cartera.empty:
        return pd.DataFrame()

    # Sumar por cliente
    if vendedor is None:
        grouped = (
            fac_en_cartera.groupby(["vendedor", "documento"], as_index=False)["monto"]
            .sum()
        )
    else:
        grouped = (
            fac_en_cartera.groupby(["documento"], as_index=False)["monto"].sum()
        )

    # Razón social
    grouped["razon_social"] = grouped["documento"].map(rs_map)

    # Ordenar por monto descendente y calcular pct
    grouped = grouped.sort_values("monto", ascending=False).reset_index(drop=True)
    total = float(grouped["monto"].sum())
    if total == 0:
        return pd.DataFrame()

    grouped["pct_individual"] = (grouped["monto"] / total * 100).round(2)
    grouped["pct_acumulado"] = grouped["pct_individual"].cumsum().round(2)

    # Reordenar columnas
    if vendedor is None:
        cols = ["vendedor", "documento", "razon_social", "monto", "pct_individual", "pct_acumulado"]
    else:
        cols = ["documento", "razon_social", "monto", "pct_individual", "pct_acumulado"]
    return grouped[cols]
