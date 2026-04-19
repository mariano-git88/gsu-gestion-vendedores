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

# =====================================================================
# ANÁLISIS LONGITUDINAL (requieren histórico de 12 meses)
# =====================================================================

def clientes_dormidos(
    df_hist: pd.DataFrame,
    df_clientes: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
    umbral_dias: int = 90,
) -> pd.DataFrame:
    """
    Lista de clientes en cartera que no recibieron una FAC de su
    vendedor asignado en los últimos `umbral_dias` (default 90).

    Criterio "dormido" = la fecha de su última FAC es anterior a
    `hoy - umbral_dias`, considerando solo ventas del mismo vendedor
    asignado (match estricto). Los clientes que NUNCA compraron
    también aparecen (con `dias_sin_comprar` = None y fecha nula).

    Args:
        df_hist: facturación de los últimos 12 meses ya procesada
            (`transforms.prepare_facturacion`). Si es None o vacío,
            la función devuelve DataFrame vacío.
        df_clientes: maestro de cartera.
        hoy: referencia temporal (default = pd.Timestamp.today().normalize()).
        umbral_dias: a partir de cuántos días sin comprar se considera
            dormido. Default 90 (confirmado por Mariano 2026-04-18).

    Devuelve DataFrame con columnas:
      vendedor, documento, razon_social, ultima_fecha_compra,
      dias_sin_comprar
    Ordenado por vendedor y días descendente (más dormido arriba).
    """
    cols = [
        "vendedor", "documento", "razon_social",
        "ultima_fecha_compra", "dias_sin_comprar",
    ]
    if df_hist is None or df_hist.empty:
        return pd.DataFrame(columns=cols)

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    # Cartera única con razón social
    cartera = (
        df_clientes[["vendedor", "documento", "razon_social"]]
        .dropna(subset=["vendedor", "documento"])
        .drop_duplicates(subset=["vendedor", "documento"])
    )

    # Última FAC por (vendedor, documento) — match estricto
    df_fac = df_hist[df_hist["tipo"] == TIPO_FAC].copy()
    if not df_fac.empty:
        df_fac["fecha"] = pd.to_datetime(df_fac["fecha"], errors="coerce")
        ultima = (
            df_fac.groupby(["vendedor", "documento"], as_index=False)["fecha"]
            .max()
            .rename(columns={"fecha": "ultima_fecha_compra"})
        )
    else:
        ultima = pd.DataFrame(
            columns=["vendedor", "documento", "ultima_fecha_compra"]
        )

    result = cartera.merge(ultima, on=["vendedor", "documento"], how="left")
    result["dias_sin_comprar"] = result["ultima_fecha_compra"].apply(
        lambda d: (hoy - d).days if pd.notna(d) else None
    )

    # Filtrar: dormidos (>umbral) o nunca compraron (NaT)
    mask_dormido = (
        result["dias_sin_comprar"].isna()
        | (result["dias_sin_comprar"] > umbral_dias)
    )
    result = result[mask_dormido][cols]

    # Orden: primero los que tienen más días dormidos; los NaN al final
    return (
        result.sort_values(
            ["vendedor", "dias_sin_comprar"],
            ascending=[True, False],
            na_position="last",
        )
        .reset_index(drop=True)
    )


def clientes_nuevos(
    df_mes: pd.DataFrame,
    df_hist: pd.DataFrame,
    df_clientes: pd.DataFrame,
    inicio_mes_actual: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Clientes con primera FAC en el mes actual (período `df_mes`) que
    NO tenían FAC previas en los 12 meses anteriores (según `df_hist`).

    Match estricto: la compra del mes actual tiene que ser del
    vendedor asignado al cliente. Un cliente que compró por primera
    vez a un vendedor distinto al asignado no se cuenta como "nuevo"
    para ese vendedor.

    Args:
        df_mes: facturación del mes actual, ya procesada.
        df_hist: facturación de los 12 meses anteriores. Debe cubrir
            `inicio_mes_actual - 12 meses` → `inicio_mes_actual`.
        df_clientes: maestro de cartera.
        inicio_mes_actual: primer día del mes actual (para filtrar el
            histórico a "meses previos"). Default: primer día del mes
            de hoy.

    Devuelve DataFrame con columnas:
      vendedor, documento, razon_social, primera_compra, monto_mes
    Ordenado por vendedor y monto_mes descendente.
    """
    cols = [
        "vendedor", "documento", "razon_social",
        "primera_compra", "monto_mes",
    ]
    if df_mes is None or df_mes.empty:
        return pd.DataFrame(columns=cols)

    if inicio_mes_actual is None:
        hoy = pd.Timestamp.today().normalize()
        inicio_mes_actual = pd.Timestamp(hoy.year, hoy.month, 1)

    cartera = (
        df_clientes[["vendedor", "documento", "razon_social"]]
        .dropna(subset=["vendedor", "documento"])
        .drop_duplicates(subset=["vendedor", "documento"])
    )

    # Compradores del mes (FAC, match estricto con cartera)
    fac_mes_cartera = _fac_en_cartera_propia(df_mes, df_clientes)
    if fac_mes_cartera.empty:
        return pd.DataFrame(columns=cols)

    fac_mes_cartera = fac_mes_cartera.copy()
    fac_mes_cartera["fecha"] = pd.to_datetime(
        fac_mes_cartera["fecha"], errors="coerce"
    )

    # Sumar monto y tomar fecha mínima (primera compra) por cliente
    compradores = (
        fac_mes_cartera.groupby(["vendedor", "documento"], as_index=False)
        .agg(primera_compra=("fecha", "min"), monto_mes=("monto", "sum"))
    )

    # Compradores previos en el histórico (cualquier FAC propia antes
    # del inicio del mes actual)
    if df_hist is None or df_hist.empty:
        previos = pd.DataFrame(columns=["vendedor", "documento"])
    else:
        fac_hist = df_hist[df_hist["tipo"] == TIPO_FAC].copy()
        fac_hist["fecha"] = pd.to_datetime(fac_hist["fecha"], errors="coerce")
        fac_hist = fac_hist[fac_hist["fecha"] < inicio_mes_actual]
        # Match estricto: vendedor de la operación = vendedor asignado
        fac_hist_cartera = fac_hist.merge(
            cartera[["vendedor", "documento"]],
            on=["vendedor", "documento"],
            how="inner",
        )
        previos = (
            fac_hist_cartera[["vendedor", "documento"]]
            .drop_duplicates()
        )

    # Nuevos = compradores del mes SIN compras previas
    nuevos = compradores.merge(
        previos.assign(__had_before=1),
        on=["vendedor", "documento"],
        how="left",
    )
    nuevos = nuevos[nuevos["__had_before"].isna()].drop(columns="__had_before")

    # Traer razón social
    nuevos = nuevos.merge(cartera, on=["vendedor", "documento"], how="left")
    nuevos["monto_mes"] = nuevos["monto_mes"].astype(float).round(2)

    return (
        nuevos[cols]
        .sort_values(
            ["vendedor", "monto_mes"],
            ascending=[True, False],
        )
        .reset_index(drop=True)
    )


def tasa_retencion(
    df_hist: pd.DataFrame,
    df_clientes: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Tasa de retención por vendedor.

    Definición (acordada con Mariano 2026-04-18):
      - A = clientes del vendedor que compraron (FAC propia) en el
        mes calendario de `hoy - 6 meses`.
      - B = subset de A que también compró (FAC propia) en los
        últimos 3 meses (últimos 90 días desde `hoy`).
      - retención_pct = |B| / |A| × 100. Si |A| = 0, retención = 0.

    Match estricto en ambos conjuntos (vendedor operación = vendedor
    asignado).

    Args:
        df_hist: histórico que cubra al menos `hoy - 6m` hasta `hoy`.
        df_clientes: maestro de cartera.
        hoy: referencia temporal (default = pd.Timestamp.today().normalize()).

    Devuelve DataFrame con columnas:
      vendedor, clientes_hace_6m, clientes_retenidos_3m, retencion_pct
    Ordenado por retencion_pct descendente.
    """
    cols = [
        "vendedor", "clientes_hace_6m",
        "clientes_retenidos_3m", "retencion_pct",
    ]
    if df_hist is None or df_hist.empty:
        return pd.DataFrame(columns=cols)

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    # Ventana "hace 6 meses": el mes calendario correspondiente.
    # Si hoy es 2026-04-18, "hace 6 meses" = octubre 2025 (mes completo).
    m_6m = hoy - pd.DateOffset(months=6)
    inicio_6m = pd.Timestamp(m_6m.year, m_6m.month, 1)
    # Último día del mes "hace 6 meses"
    if m_6m.month == 12:
        fin_6m = pd.Timestamp(m_6m.year, 12, 31)
    else:
        fin_6m = pd.Timestamp(m_6m.year, m_6m.month + 1, 1) - pd.Timedelta(days=1)

    # Ventana "últimos 3 meses": 90 días hacia atrás desde hoy.
    inicio_3m = hoy - pd.Timedelta(days=90)

    df_fac = df_hist[df_hist["tipo"] == TIPO_FAC].copy()
    if df_fac.empty:
        return pd.DataFrame(columns=cols)
    df_fac["fecha"] = pd.to_datetime(df_fac["fecha"], errors="coerce")

    fac_en_cartera = _fac_en_cartera_propia(df_fac, df_clientes)
    if fac_en_cartera.empty:
        return pd.DataFrame(columns=cols)
    fac_en_cartera = fac_en_cartera.copy()
    fac_en_cartera["fecha"] = pd.to_datetime(
        fac_en_cartera["fecha"], errors="coerce"
    )

    # A: (vendedor, documento) que compraron hace 6 meses
    mask_6m = (
        (fac_en_cartera["fecha"] >= inicio_6m)
        & (fac_en_cartera["fecha"] <= fin_6m)
    )
    a_set = (
        fac_en_cartera[mask_6m][["vendedor", "documento"]]
        .drop_duplicates()
    )

    # B: (vendedor, documento) que compraron en los últimos 3 meses
    mask_3m = fac_en_cartera["fecha"] >= inicio_3m
    b_set = (
        fac_en_cartera[mask_3m][["vendedor", "documento"]]
        .drop_duplicates()
    )

    # Retenidos = A ∩ B
    retenidos = a_set.merge(b_set, on=["vendedor", "documento"], how="inner")

    a_por_vendedor = (
        a_set.groupby("vendedor", as_index=False)["documento"]
        .nunique()
        .rename(columns={"documento": "clientes_hace_6m"})
    )
    r_por_vendedor = (
        retenidos.groupby("vendedor", as_index=False)["documento"]
        .nunique()
        .rename(columns={"documento": "clientes_retenidos_3m"})
    )

    result = a_por_vendedor.merge(r_por_vendedor, on="vendedor", how="left")
    result["clientes_retenidos_3m"] = (
        result["clientes_retenidos_3m"].fillna(0).astype(int)
    )
    result["retencion_pct"] = result.apply(
        lambda r: round(r["clientes_retenidos_3m"] / r["clientes_hace_6m"] * 100, 2)
        if r["clientes_hace_6m"] > 0 else 0.0,
        axis=1,
    )

    return (
        result[cols]
        .sort_values("retencion_pct", ascending=False)
        .reset_index(drop=True)
    )


def frecuencia_compra_por_cliente(
    df_hist: pd.DataFrame,
    df_clientes: pd.DataFrame,
) -> pd.DataFrame:
    """
    Para cada cliente con ≥2 FAC en el histórico, el promedio de días
    entre compras consecutivas del mismo vendedor asignado (match
    estricto).

    Cálculo: para cada (vendedor, documento), ordenar fechas
    ascendentemente y calcular el promedio de las diferencias
    consecutivas. El resultado es un proxy de "cada cuántos días me
    compra este cliente". Valores bajos = clientes frecuentes.

    Se descartan clientes con una sola FAC (no se puede calcular un
    "intervalo entre compras"). Los clientes con 0 FAC tampoco aparecen.

    Args:
        df_hist: histórico 12m ya procesado.
        df_clientes: maestro (para traer razón social).

    Devuelve DataFrame con columnas:
      vendedor, documento, razon_social, n_compras,
      dias_promedio_entre_compras, ultima_compra
    Ordenado por dias_promedio_entre_compras ascendente (más frecuentes arriba).
    """
    cols = [
        "vendedor", "documento", "razon_social",
        "n_compras", "dias_promedio_entre_compras", "ultima_compra",
    ]
    if df_hist is None or df_hist.empty:
        return pd.DataFrame(columns=cols)

    df_fac = df_hist[df_hist["tipo"] == TIPO_FAC].copy()
    fac_en_cartera = _fac_en_cartera_propia(df_fac, df_clientes)
    if fac_en_cartera.empty:
        return pd.DataFrame(columns=cols)

    fac_en_cartera = fac_en_cartera.copy()
    fac_en_cartera["fecha"] = pd.to_datetime(
        fac_en_cartera["fecha"], errors="coerce"
    )

    # Colapsar a 1 fila por (vendedor, documento, fecha) — si un día
    # hay 2 comprobantes, contamos como una sola compra para el
    # cálculo de frecuencia.
    por_dia = (
        fac_en_cartera.groupby(["vendedor", "documento", "fecha"], as_index=False)
        .size()
        .drop(columns="size", errors="ignore")
    )

    resultados = []
    for (vendedor, documento), grupo in por_dia.groupby(["vendedor", "documento"]):
        fechas = grupo["fecha"].sort_values().reset_index(drop=True)
        n_compras = len(fechas)
        if n_compras < 2:
            continue
        diffs = fechas.diff().dropna().dt.days
        dias_promedio = round(float(diffs.mean()), 1)
        resultados.append(
            {
                "vendedor": vendedor,
                "documento": documento,
                "n_compras": n_compras,
                "dias_promedio_entre_compras": dias_promedio,
                "ultima_compra": fechas.iloc[-1],
            }
        )

    if not resultados:
        return pd.DataFrame(columns=cols)

    result = pd.DataFrame(resultados)

    # Razón social
    cartera = (
        df_clientes[["vendedor", "documento", "razon_social"]]
        .dropna(subset=["vendedor", "documento"])
        .drop_duplicates(subset=["vendedor", "documento"])
    )
    result = result.merge(cartera, on=["vendedor", "documento"], how="left")

    return (
        result[cols]
        .sort_values("dias_promedio_entre_compras", ascending=True)
        .reset_index(drop=True)
    )


# =====================================================================
# INVENTARIO (requiere histórico 12m + stock en productos/combos)
# =====================================================================
#
# Stock viene del detalle de conceptos en Contabilium (discovery
# 2026-04-18). La venta semanal promedio se calcula sobre `df_hist`
# (histórico 12 meses). Las "semanas de stock" = stock / venta_sem.
# Umbral crítico = <4 semanas (confirmado por Mariano 2026-04-18).

SEMANAS_POR_MES = 4.345  # (30.44 días / 7)
CRITICIDAD_SEMANAS = 4.0  # umbral para marcar crítico


def _venta_unidades_por_sku_en_rango(
    df_hist: pd.DataFrame,
    desde: pd.Timestamp,
    hasta: pd.Timestamp,
) -> pd.DataFrame:
    """Suma unidades por SKU (FAC + NCF netean) entre `desde` y `hasta`.

    Devuelve DataFrame con columnas: sku, unidades. Unidades puede ser
    negativa si las devoluciones superaron las ventas (caso raro pero
    posible); en ese caso la venta semanal se clampa a 0 en el caller.
    """
    if df_hist.empty:
        return pd.DataFrame(columns=["sku", "unidades"])
    df = df_hist.copy()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    mask = (df["fecha"] >= desde) & (df["fecha"] <= hasta)
    sub = df[mask]
    if sub.empty:
        return pd.DataFrame(columns=["sku", "unidades"])
    return (
        sub.groupby("sku", as_index=False)["unidades"]
        .sum()
        .rename(columns={"unidades": "unidades"})
    )


def ventas_semanales_por_sku(
    df_hist: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Venta semanal promedio por SKU bajo 3 cortes temporales:
      - ultimo_mes: unidades últimos 30 días / 4.345
      - ultimos_3m: unidades últimos 90 días / (90/7)
      - mejor_mes_12m: de los últimos 12 meses calendario, el mes con
        MAYOR unidades netas, dividido por 4.345.

    Los 3 valores se devuelven siempre, aunque el usuario solo use uno
    para marcar criticidad.

    Valores negativos (devoluciones netas) se clampan a 0 — "venta
    semanal" debajo de cero no tiene sentido para el cálculo de
    semanas de stock.

    Devuelve DataFrame con columnas:
      sku, venta_sem_ultimo_mes, venta_sem_ultimos_3m, venta_sem_mejor_mes
    """
    cols = [
        "sku",
        "venta_sem_ultimo_mes",
        "venta_sem_ultimos_3m",
        "venta_sem_mejor_mes",
    ]
    if df_hist is None or df_hist.empty:
        return pd.DataFrame(columns=cols)

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    # --- Corte 1: últimos 30 días ---
    v1 = _venta_unidades_por_sku_en_rango(
        df_hist, hoy - pd.Timedelta(days=30), hoy
    )
    v1["venta_sem_ultimo_mes"] = (v1["unidades"] / SEMANAS_POR_MES).clip(lower=0)
    v1 = v1[["sku", "venta_sem_ultimo_mes"]]

    # --- Corte 2: últimos 90 días ---
    v2 = _venta_unidades_por_sku_en_rango(
        df_hist, hoy - pd.Timedelta(days=90), hoy
    )
    v2["venta_sem_ultimos_3m"] = (v2["unidades"] / (90 / 7)).clip(lower=0)
    v2 = v2[["sku", "venta_sem_ultimos_3m"]]

    # --- Corte 3: mejor mes calendario de los últimos 12 ---
    # Para cada (sku, año, mes), sumar unidades. Tomar el máximo por sku.
    df = df_hist.copy()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    limite_inferior = hoy - pd.DateOffset(months=12)
    df = df[df["fecha"] >= limite_inferior]
    if df.empty:
        v3 = pd.DataFrame(columns=["sku", "venta_sem_mejor_mes"])
    else:
        df["_ym"] = df["fecha"].dt.to_period("M")
        mensuales = (
            df.groupby(["sku", "_ym"], as_index=False)["unidades"].sum()
        )
        mejor = (
            mensuales.groupby("sku", as_index=False)["unidades"]
            .max()
            .rename(columns={"unidades": "venta_sem_mejor_mes"})
        )
        mejor["venta_sem_mejor_mes"] = (
            mejor["venta_sem_mejor_mes"] / SEMANAS_POR_MES
        ).clip(lower=0)
        v3 = mejor

    # Outer join de los 3 cortes sobre sku
    result = v1.merge(v2, on="sku", how="outer").merge(v3, on="sku", how="outer")
    for c in [
        "venta_sem_ultimo_mes", "venta_sem_ultimos_3m", "venta_sem_mejor_mes"
    ]:
        result[c] = result[c].fillna(0.0).round(2)

    return result[cols].reset_index(drop=True)


def inventario_semanas_stock(
    df_productos: pd.DataFrame,
    df_combos: pd.DataFrame,
    df_hist: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Tabla consolidada de inventario: para cada SKU (productos y combos)
    muestra stock actual, venta semanal promedio bajo 3 cortes, y
    semanas de stock resultantes.

    Args:
        df_productos: maestro de productos con columnas sku, nombre,
            sub_rubro, rubro, stock.
        df_combos: maestro de combos con columnas sku, nombre, stock
            (ya calculado derivado de componentes).
        df_hist: histórico de 12 meses ya procesado.
        hoy: referencia temporal.

    Devuelve DataFrame con columnas:
      sku, nombre, tipo, sub_rubro, stock,
      venta_sem_ultimo_mes, venta_sem_ultimos_3m, venta_sem_mejor_mes,
      semanas_ultimo_mes, semanas_ultimos_3m, semanas_mejor_mes,
      critico   (True si semanas_ultimos_3m < CRITICIDAD_SEMANAS)
    Ordenado por semanas_ultimos_3m ascendente (más críticos arriba).

    Notas:
      - `tipo` = "Producto" o "Combo".
      - SKUs sin venta en el corte → semanas = infinito (representado
        como pd.NA). El criticidad flag usa el corte de 3 meses —
        si no hay venta, NO se marca crítico porque no tiene sentido.
      - Stock = 0 con venta positiva → semanas = 0 (ya se acabó).
    """
    cols_out = [
        "sku", "nombre", "tipo", "sub_rubro", "stock",
        "venta_sem_ultimo_mes", "venta_sem_ultimos_3m", "venta_sem_mejor_mes",
        "semanas_ultimo_mes", "semanas_ultimos_3m", "semanas_mejor_mes",
        "critico",
    ]
    if df_productos is None and df_combos is None:
        return pd.DataFrame(columns=cols_out)

    # Unificar productos y combos con columna tipo
    partes = []
    if df_productos is not None and not df_productos.empty:
        p = df_productos[["sku", "nombre", "sub_rubro", "stock"]].copy()
        p["tipo"] = "Producto"
        partes.append(p)
    if df_combos is not None and not df_combos.empty:
        c = df_combos[["sku", "nombre", "stock"]].copy()
        c["sub_rubro"] = "COMBO"
        c["tipo"] = "Combo"
        partes.append(c)
    if not partes:
        return pd.DataFrame(columns=cols_out)

    base = pd.concat(partes, ignore_index=True)

    # Trae venta semanal por sku (3 cortes)
    if df_hist is None or df_hist.empty:
        ventas = pd.DataFrame(columns=[
            "sku", "venta_sem_ultimo_mes",
            "venta_sem_ultimos_3m", "venta_sem_mejor_mes",
        ])
    else:
        ventas = ventas_semanales_por_sku(df_hist, hoy=hoy)

    result = base.merge(ventas, on="sku", how="left")
    for c in [
        "venta_sem_ultimo_mes", "venta_sem_ultimos_3m", "venta_sem_mejor_mes"
    ]:
        result[c] = result[c].fillna(0.0)

    # Semanas de stock por corte. stock / venta_semanal. Si venta=0 →
    # semanas = NA (infinito). Si venta>0 y stock=0 → semanas = 0.
    def _semanas(stock, venta):
        return round(stock / venta, 1) if venta > 0 else pd.NA

    result["semanas_ultimo_mes"] = result.apply(
        lambda r: _semanas(r["stock"], r["venta_sem_ultimo_mes"]), axis=1
    )
    result["semanas_ultimos_3m"] = result.apply(
        lambda r: _semanas(r["stock"], r["venta_sem_ultimos_3m"]), axis=1
    )
    result["semanas_mejor_mes"] = result.apply(
        lambda r: _semanas(r["stock"], r["venta_sem_mejor_mes"]), axis=1
    )

    # Crítico: semanas de stock (corte 3 meses, el default) < umbral.
    # Si no hay venta en 3 meses, NO es crítico (no hay demanda).
    result["critico"] = result["semanas_ultimos_3m"].apply(
        lambda s: (s is not pd.NA) and (pd.notna(s)) and (s < CRITICIDAD_SEMANAS)
    )

    # Ordenar: primero los críticos por semanas ascendente; después el resto
    # por semanas descendente (SKUs más saturados abajo). SKUs sin venta
    # al final.
    def _sort_key(row):
        s = row["semanas_ultimos_3m"]
        if pd.isna(s):
            return (2, 0)  # sin venta: al final
        if row["critico"]:
            return (0, float(s))  # críticos primero, ascendente
        return (1, float(s))  # OK después, ascendente también

    result["_k"] = result.apply(_sort_key, axis=1)
    result = result.sort_values("_k").drop(columns="_k").reset_index(drop=True)

    return result[cols_out]


# =====================================================================
# COBRANZAS (requieren campos saldo / fecha_vencimiento en el df)
# =====================================================================
#
# Las 4 funciones de cobranzas operan sobre DataFrames que tienen las
# columnas `saldo`, `fecha_vencimiento`, `condicion_venta` agregadas
# por `api_loader.load_fc_api` (discovery 2026-04-18: el detalle del
# comprobante ya trae saldo y vencimiento, no hace falta pipeline
# separado de cobranzas). El Modo Manual Secundario no soporta estas
# métricas — el xlsx no tiene saldo ni vencimiento.
#
# IMPORTANTE: el DF viene con una fila por ITEM del comprobante. Los
# campos saldo/fecha_vencimiento están replicados en todas las filas
# del mismo comprobante. Antes de sumar saldos hay que **colapsar a
# una fila por comprobante** con `_deuda_viva_por_comprobante`.


def _deuda_viva_por_comprobante(df_fc: pd.DataFrame) -> pd.DataFrame:
    """
    Colapsa `df_fc` a una fila por comprobante con saldo > 0.

    Cada fila representa un comprobante único con su saldo pendiente,
    fecha de emisión y fecha de vencimiento. Se filtra a `tipo == FAC`
    (las NCF no se cuentan como deuda; ya netean al emitirse contra la
    FAC original en el ERP).

    Devuelve DataFrame con columnas:
      id_comprobante, vendedor, documento, razon_social, fecha,
      fecha_vencimiento, saldo, condicion_venta
    """
    cols = [
        "id_comprobante", "vendedor", "documento", "razon_social",
        "fecha", "fecha_vencimiento", "saldo", "condicion_venta",
    ]
    if df_fc.empty or "saldo" not in df_fc.columns:
        return pd.DataFrame(columns=cols)

    df = df_fc[df_fc["tipo"] == TIPO_FAC].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Una fila por comprobante (tomamos first — los campos de
    # cobranza están replicados en cada item del mismo cid).
    colapsado = (
        df.groupby("id_comprobante", as_index=False)
        .agg(
            vendedor=("vendedor", "first"),
            documento=("documento", "first"),
            razon_social=("razon_social", "first"),
            fecha=("fecha", "first"),
            fecha_vencimiento=("fecha_vencimiento", "first"),
            saldo=("saldo", "first"),
            condicion_venta=("condicion_venta", "first"),
        )
    )
    # Filtramos a deuda viva (saldo > 0).
    return colapsado[colapsado["saldo"] > 0][cols].reset_index(drop=True)


def _bucket_aging(dias: int | None) -> str:
    """Clasifica días desde vencimiento en buckets.

    Convención:
      - dias < 0: comprobante aún no vencido → "Al día".
      - 0 <= dias <= 30: "0-30" (recién vencido).
      - 31 <= dias <= 60: "31-60".
      - 61 <= dias <= 90: "61-90".
      - dias > 90: "90+" (crónico).
      - None (sin fecha de vencimiento): "Sin vencimiento".
    """
    if dias is None or pd.isna(dias):
        return "Sin vencimiento"
    d = int(dias)
    if d < 0:
        return "Al día"
    if d <= 30:
        return "0-30"
    if d <= 60:
        return "31-60"
    if d <= 90:
        return "61-90"
    return "90+"


BUCKETS_ORDEN = ["Al día", "0-30", "31-60", "61-90", "90+", "Sin vencimiento"]


def aging_por_cliente(
    df_fc: pd.DataFrame, hoy: pd.Timestamp | None = None
) -> pd.DataFrame:
    """
    Matriz de aging por cliente: para cada cliente con deuda viva,
    cuánto debe en cada bucket (0-30 / 31-60 / 61-90 / 90+ / Al día /
    Sin vencimiento).

    El bucket se determina por los días entre `hoy` y
    `fecha_vencimiento` de cada comprobante. Los comprobantes sin
    fecha de vencimiento caen en "Sin vencimiento" (bucket informativo
    — no significa "en mora", solo que el ERP no registró plazo).

    Devuelve DataFrame con columnas:
      vendedor, documento, razon_social,
      al_dia, b_0_30, b_31_60, b_61_90, b_90_mas, sin_vencimiento,
      deuda_total
    Ordenado por deuda_total descendente.
    """
    cols_out = [
        "vendedor", "documento", "razon_social",
        "al_dia", "b_0_30", "b_31_60", "b_61_90", "b_90_mas",
        "sin_vencimiento", "deuda_total",
    ]
    deuda = _deuda_viva_por_comprobante(df_fc)
    if deuda.empty:
        return pd.DataFrame(columns=cols_out)

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    deuda = deuda.copy()
    deuda["fecha_vencimiento"] = pd.to_datetime(
        deuda["fecha_vencimiento"], errors="coerce"
    )
    deuda["dias"] = (hoy - deuda["fecha_vencimiento"]).dt.days
    deuda["bucket"] = deuda["dias"].apply(_bucket_aging)

    # Sumar saldo por (vendedor, documento, bucket)
    pivoted = (
        deuda.groupby(
            ["vendedor", "documento", "razon_social", "bucket"],
            as_index=False,
        )["saldo"]
        .sum()
        .pivot_table(
            index=["vendedor", "documento", "razon_social"],
            columns="bucket",
            values="saldo",
            fill_value=0.0,
        )
        .reset_index()
    )

    # Asegurar todas las columnas (algunos buckets pueden no aparecer)
    for b in BUCKETS_ORDEN:
        if b not in pivoted.columns:
            pivoted[b] = 0.0

    # Renombrar a claves snake_case para que sean amables en la vista
    rename_buckets = {
        "Al día": "al_dia",
        "0-30": "b_0_30",
        "31-60": "b_31_60",
        "61-90": "b_61_90",
        "90+": "b_90_mas",
        "Sin vencimiento": "sin_vencimiento",
    }
    pivoted = pivoted.rename(columns=rename_buckets)
    pivoted["deuda_total"] = pivoted[
        ["al_dia", "b_0_30", "b_31_60", "b_61_90", "b_90_mas", "sin_vencimiento"]
    ].sum(axis=1).round(2)
    for c in ["al_dia", "b_0_30", "b_31_60", "b_61_90", "b_90_mas", "sin_vencimiento"]:
        pivoted[c] = pivoted[c].round(2)

    return (
        pivoted[cols_out]
        .sort_values("deuda_total", ascending=False)
        .reset_index(drop=True)
    )


def top_deudores(
    df_fc: pd.DataFrame, n: int = 20
) -> pd.DataFrame:
    """
    Top N clientes con mayor deuda viva (saldo > 0 sumado por cliente).

    Se suma el saldo de todos los comprobantes FAC del mismo cliente,
    independiente de vendedor. Incluye una columna con la cantidad de
    comprobantes pendientes para dar contexto.

    Devuelve DataFrame con columnas:
      documento, razon_social, vendedor, deuda_total,
      comprobantes_pendientes, comprobante_mas_viejo
    Ordenado por deuda_total descendente.
    """
    cols_out = [
        "documento", "razon_social", "vendedor",
        "deuda_total", "comprobantes_pendientes",
        "comprobante_mas_viejo",
    ]
    deuda = _deuda_viva_por_comprobante(df_fc)
    if deuda.empty:
        return pd.DataFrame(columns=cols_out)

    deuda = deuda.copy()
    deuda["fecha"] = pd.to_datetime(deuda["fecha"], errors="coerce")

    agregado = (
        deuda.groupby(
            ["documento", "razon_social", "vendedor"],
            as_index=False,
        )
        .agg(
            deuda_total=("saldo", "sum"),
            comprobantes_pendientes=("id_comprobante", "nunique"),
            comprobante_mas_viejo=("fecha", "min"),
        )
    )
    agregado["deuda_total"] = agregado["deuda_total"].round(2)

    return (
        agregado[cols_out]
        .sort_values("deuda_total", ascending=False)
        .head(n)
        .reset_index(drop=True)
    )


def dias_promedio_deuda_por_vendedor(
    df_fc: pd.DataFrame, hoy: pd.Timestamp | None = None
) -> pd.DataFrame:
    """
    Para cada vendedor, días promedio de sus comprobantes con deuda
    viva — calculado como `hoy - fecha_emision`. Es un proxy del DSO
    clásico, más simple de calcular sin llamar al endpoint de cobranzas.

    Valores altos sugieren que el vendedor deja envejecer sus
    facturas pendientes; valores bajos indican rotación rápida o
    poca deuda vieja.

    Se ponderan todos los comprobantes por igual (no por monto). Si
    se quisiera ponderar por saldo, habría que hacer un promedio
    ponderado — se deja para una iteración posterior si el número
    "peso monto" tuviera más sentido operativo.

    Devuelve DataFrame con columnas:
      vendedor, comprobantes_pendientes, deuda_total,
      dias_promedio_deuda
    Ordenado por dias_promedio_deuda descendente (peor arriba).
    """
    cols_out = [
        "vendedor", "comprobantes_pendientes",
        "deuda_total", "dias_promedio_deuda",
    ]
    deuda = _deuda_viva_por_comprobante(df_fc)
    if deuda.empty:
        return pd.DataFrame(columns=cols_out)

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    deuda = deuda.copy()
    deuda["fecha"] = pd.to_datetime(deuda["fecha"], errors="coerce")
    deuda["dias_deuda"] = (hoy - deuda["fecha"]).dt.days

    result = (
        deuda.groupby("vendedor", as_index=False)
        .agg(
            comprobantes_pendientes=("id_comprobante", "nunique"),
            deuda_total=("saldo", "sum"),
            dias_promedio_deuda=("dias_deuda", "mean"),
        )
    )
    result["deuda_total"] = result["deuda_total"].round(2)
    result["dias_promedio_deuda"] = result["dias_promedio_deuda"].round(1)

    return (
        result[cols_out]
        .sort_values("dias_promedio_deuda", ascending=False)
        .reset_index(drop=True)
    )


def deuda_vencida_vs_corriente(
    df_fc: pd.DataFrame, hoy: pd.Timestamp | None = None
) -> dict:
    """
    Resumen de deuda viva total, partida en vencida vs corriente.

    - **Deuda vencida** = saldo de comprobantes con `fecha_vencimiento
      < hoy` (ya pasó el plazo).
    - **Deuda corriente** = saldo de comprobantes con
      `fecha_vencimiento >= hoy` o sin vencimiento registrado.

    Devuelve dict:
      {
        "total": float,
        "vencida": float,
        "corriente": float,
        "pct_vencida": float,  # vencida / total × 100
        "n_vencidos": int,     # cantidad de comprobantes vencidos
        "n_corrientes": int,
      }
    """
    vacio = {
        "total": 0.0, "vencida": 0.0, "corriente": 0.0,
        "pct_vencida": 0.0, "n_vencidos": 0, "n_corrientes": 0,
    }
    deuda = _deuda_viva_por_comprobante(df_fc)
    if deuda.empty:
        return vacio

    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    deuda = deuda.copy()
    deuda["fecha_vencimiento"] = pd.to_datetime(
        deuda["fecha_vencimiento"], errors="coerce"
    )
    vencidos = (
        deuda["fecha_vencimiento"].notna()
        & (deuda["fecha_vencimiento"] < hoy)
    )

    total = float(deuda["saldo"].sum())
    vencida = float(deuda.loc[vencidos, "saldo"].sum())
    corriente = total - vencida
    pct = (vencida / total * 100) if total > 0 else 0.0

    return {
        "total": round(total, 2),
        "vencida": round(vencida, 2),
        "corriente": round(corriente, 2),
        "pct_vencida": round(pct, 2),
        "n_vencidos": int(vencidos.sum()),
        "n_corrientes": int((~vencidos).sum()),
    }


def comparativa_temporal(
    df_actual: pd.DataFrame,
    df_prev: pd.DataFrame | None = None,
    df_yoy: pd.DataFrame | None = None,
) -> dict:
    """
    Calcula totales comparativos entre el período actual, el mes
    anterior y el mismo mes del año pasado.

    Los 3 DataFrames deben estar procesados por el pipeline habitual
    (`transforms.prepare_facturacion`) y, cuando se comparan, haber
    sido recortados al mismo número de días (ver
    `app._rango_mes_comparativo_mismo_dia`). Si no se recortaron, la
    comparación numérica sigue siendo útil pero no es apples-to-apples.

    Args:
        df_actual: facturación del período de referencia (típicamente
            el mes en curso recortado a hoy).
        df_prev: facturación del mes anterior, recortada al mismo día.
            None si no se pulleó (Modo Manual).
        df_yoy: facturación del mismo mes año pasado, recortada al
            mismo día. None si no se pulleó.

    Devuelve dict:
      {
        "monto_actual": float,
        "monto_prev": float | None,
        "monto_yoy": float | None,
        "delta_mom_pct": float | None,  # % vs mes anterior
        "delta_yoy_pct": float | None,  # % vs mismo mes año pasado
        "tickets_actual": int,
        "tickets_prev": int | None,
        "tickets_yoy": int | None,
      }
    Valores None cuando no hay dato comparativo disponible o cuando el
    período comparativo tiene monto 0 (no se puede dividir).
    """
    def _monto_total(df):
        if df is None or df.empty:
            return None
        return float(df["monto"].sum())

    def _tickets(df):
        if df is None or df.empty or "id_comprobante" not in df.columns:
            return None
        return int(df["id_comprobante"].nunique())

    def _delta_pct(actual, comp):
        if actual is None or comp is None or comp == 0:
            return None
        return round((actual - comp) / abs(comp) * 100, 2)

    monto_actual = _monto_total(df_actual) or 0.0
    monto_prev = _monto_total(df_prev)
    monto_yoy = _monto_total(df_yoy)

    return {
        "monto_actual": monto_actual,
        "monto_prev": monto_prev,
        "monto_yoy": monto_yoy,
        "delta_mom_pct": _delta_pct(monto_actual, monto_prev),
        "delta_yoy_pct": _delta_pct(monto_actual, monto_yoy),
        "tickets_actual": _tickets(df_actual) or 0,
        "tickets_prev": _tickets(df_prev),
        "tickets_yoy": _tickets(df_yoy),
    }


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
