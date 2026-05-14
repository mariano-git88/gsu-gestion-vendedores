"""
listas.py — Lógica de comparación de listas de precios UY vs AR.

UY: se trae en vivo desde la API de Contabilium (reutiliza
`api_loader.load_productos_api`). El precio devuelto por esa función
es el `PrecioFinal` de Contabilium dividido por 1.22, asumiendo IVA UY
22%. Es el precio "neto sin IVA" del único nivel de precio que
Contabilium UY expone vía `/api/conceptos/search`.

AR: se sube como xlsx con el formato estándar de "Lista_Marketing"
exportado del sistema interno de Suprabond AR. Para la comparación se
usa la columna `ListaPrecio` (precio mayorista al comercio, neto sin
IVA AR), validado con Mariano el 2026-05-14. Las otras columnas de
precio (`PrecioSugerido`, `PRECIO_SUGERIDO_ONLINE`) se ignoran.

Caveat de comparabilidad:
    El precio UY que se compara contra `ListaPrecio` AR es el `PrecioFinal`
    de Contabilium neto. Si en Contabilium UY existen múltiples niveles
    de precio (lista mayorista vs final), esta comparación está
    sesgada — `PrecioFinal` puede ser el PVP UY. Esto se debe revisar
    si los deltas resultantes son sospechosamente altos en forma
    sistemática.

Conversión de moneda:
    Se hace con dos tipos de cambio que el usuario ingresa
    manualmente: ARS/USD y UYU/USD. La comparación se puede pedir en
    USD o en UYU (toggle en la app).

Análisis por ancla:
    Para un SKU ancla dado, se calculan ratios `precio_sku /
    precio_ancla` en cada lista. La idea es que dentro de una
    categoría los precios se definen alrededor de un SKU
    representativo; comparar la estructura relativa (ratios) detecta
    desvíos independientemente del nivel absoluto.
"""

from __future__ import annotations

import pandas as pd

import api_loader
from subrubros import RUBROS, SUBRUBROS


# Mapping del xlsx AR. Si el export del sistema interno cambia los
# nombres de columna, ajustar acá.
COLS_AR: dict[str, str] = {
    "sku": "Producto_id",
    "marca": "Marca_Id",
    "categoria_ar": "DescripcionGrupo",
    "nombre_ar": "Descripcion",
    "precio_ars": "ListaPrecio",
}


def _norm_sku(s: object) -> str:
    """SKUs en ambas listas se normalizan a uppercase + strip para que
    el cruce no falle por casing o espacios al borde."""
    return str(s or "").strip().upper()


def parse_xlsx_ar(file_or_path) -> pd.DataFrame:
    """Parsea el xlsx exportado de Suprabond AR a un DataFrame canónico.

    Devuelve columnas: sku, marca, categoria_ar, nombre_ar, precio_ars.

    Drops filas sin SKU o sin precio positivo. Si hay SKUs duplicados
    (raro), se queda con la última ocurrencia.
    """
    df = pd.read_excel(file_or_path)
    faltantes = [c for c in COLS_AR.values() if c not in df.columns]
    if faltantes:
        raise ValueError(
            f"Columnas faltantes en el xlsx AR: {faltantes}. "
            f"Esperadas: {list(COLS_AR.values())}."
        )
    rename = {v: k for k, v in COLS_AR.items()}
    out = df.rename(columns=rename)[list(COLS_AR.keys())].copy()
    out["sku"] = out["sku"].map(_norm_sku)
    out["precio_ars"] = pd.to_numeric(out["precio_ars"], errors="coerce")
    out = out[(out["sku"] != "") & out["precio_ars"].notna() & (out["precio_ars"] > 0)]
    out = out.drop_duplicates(subset=["sku"], keep="last").reset_index(drop=True)
    return out


def load_lista_uy(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, pd.DataFrame]:
    """Trae la lista UY en vivo desde Contabilium.

    Wrapper sobre `api_loader.load_productos_api` que aplica los
    mappings canónicos del proyecto (subrubros.py) y renombra columnas
    para evitar colisiones con el lado AR.

    Devuelve (session, df) con columnas:
        sku, nombre_uy, rubro, sub_rubro, precio_uyu
    """
    session, df = api_loader.load_productos_api(
        session, subrubros_map=SUBRUBROS, rubros_map=RUBROS
    )
    df = df.copy()
    df["sku"] = df["sku"].map(_norm_sku)
    df = df.rename(columns={"nombre": "nombre_uy", "precio": "precio_uyu"})
    df = df[["sku", "nombre_uy", "rubro", "sub_rubro", "precio_uyu"]]
    df = df[df["sku"] != ""].reset_index(drop=True)
    return session, df


def cruzar_listas(
    df_uy: pd.DataFrame,
    df_ar: pd.DataFrame,
    equivalencias: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Outer join por SKU. Agrega columna `presencia` con valores
    "ambas" / "solo_uy" / "solo_ar" para filtrar después en la app.

    `equivalencias` es un mapping `sku_ar → sku_uy` (ambos normalizados
    a uppercase) que permite cruzar productos cuyo código difiere entre
    listas pero se confirmó manualmente que son el mismo SKU. Antes del
    merge, los SKUs de AR se reemplazan por su equivalente UY; el SKU
    original de AR se preserva en la columna `sku_ar_original` para
    trazabilidad.
    """
    df_uy_eff = df_uy.copy()
    df_ar_eff = df_ar.copy()
    # Forzar dtype object en sku de ambos lados. pd.read_excel suele
    # devolver ArrowStringArray y .map() del normalizador no siempre
    # downgradea — el merge entre dtypes incompatibles puede dar
    # TypeError en algunas versiones de pandas.
    df_uy_eff["sku"] = df_uy_eff["sku"].astype(object)
    df_ar_eff["sku"] = df_ar_eff["sku"].astype(object)
    df_ar_eff["sku_ar_original"] = df_ar_eff["sku"]
    if equivalencias:
        mapping = {_norm_sku(k): _norm_sku(v) for k, v in equivalencias.items()}
        # Series.replace con dict tiene comportamiento dependiente del
        # dtype (ArrowStringArray no matchea). map con fallback es
        # type-agnostic y siempre devuelve dtype object.
        df_ar_eff["sku"] = df_ar_eff["sku"].map(lambda s: mapping.get(s, s))
        # Si dos SKUs AR mapearan al mismo SKU UY (caso raro), nos
        # quedamos con el último — la app valida unicidad al crear la
        # equivalencia, así que esto es defensivo.
        df_ar_eff = df_ar_eff.drop_duplicates(subset=["sku"], keep="last")

    df = df_uy_eff.merge(df_ar_eff, on="sku", how="outer", indicator=True)
    # _merge es Categorical; .map(dict) sobre Categorical tira TypeError
    # en pandas < 2.2. astype(str) lo convierte a object antes del map.
    df["presencia"] = df["_merge"].astype(str).map(
        {"both": "ambas", "left_only": "solo_uy", "right_only": "solo_ar"}
    )
    return df.drop(columns=["_merge"])


def convertir_a_moneda(
    df: pd.DataFrame,
    fx_ars_usd: float,
    fx_uyu_usd: float,
    moneda: str = "USD",
) -> pd.DataFrame:
    """Calcula precios y delta en la moneda elegida.

    fx_ars_usd: cuántos ARS vale 1 USD.
    fx_uyu_usd: cuántos UYU vale 1 USD.
    moneda: "USD" o "UYU".

    Agrega columnas:
        precio_uy_cmp: precio UY en la moneda elegida.
        precio_ar_cmp: precio AR en la moneda elegida.
        delta_pct:     (precio_uy_cmp − precio_ar_cmp) / precio_ar_cmp × 100.
                       Positivo = UY más caro que AR.
        moneda_comparacion: "USD" o "UYU".

    Filas con precio NaN en alguna lista quedan con NaN en delta_pct.
    """
    if moneda not in ("USD", "UYU"):
        raise ValueError(f"moneda inválida: {moneda!r}, esperado 'USD' o 'UYU'")
    if fx_ars_usd <= 0 or fx_uyu_usd <= 0:
        raise ValueError("Los tipos de cambio deben ser positivos")

    out = df.copy()
    if moneda == "USD":
        out["precio_uy_cmp"] = out["precio_uyu"] / fx_uyu_usd
        out["precio_ar_cmp"] = out["precio_ars"] / fx_ars_usd
    else:  # UYU
        out["precio_uy_cmp"] = out["precio_uyu"]
        out["precio_ar_cmp"] = out["precio_ars"] / fx_ars_usd * fx_uyu_usd

    out["delta_pct"] = (
        (out["precio_uy_cmp"] - out["precio_ar_cmp"]) / out["precio_ar_cmp"] * 100
    )
    out["moneda_comparacion"] = moneda
    return out


def calcular_ratios_ancla(
    df_cruzado: pd.DataFrame,
    sku_ancla: str,
    skus_a_comparar: list[str] | None = None,
) -> pd.DataFrame:
    """Calcula ratios de cada SKU contra el ancla en ambas listas.

    Requiere que `df_cruzado` ya tenga `precio_uy_cmp` y `precio_ar_cmp`
    (correr `convertir_a_moneda` antes). El SKU ancla debe estar en
    ambas listas con precio positivo.

    Columnas devueltas:
        sku, nombre_uy, rubro, sub_rubro, categoria_ar, marca,
        precio_uyu, precio_uyu_teorico,
        precio_uy_cmp, precio_ar_cmp,
        ratio_uy, ratio_ar, delta_ratio, delta_ratio_pct

    Interpretación:
        ratio_uy = precio_uy_sku / precio_uy_ancla
        ratio_ar = precio_ar_sku / precio_ar_ancla
        delta_ratio = ratio_uy − ratio_ar
        delta_ratio_pct = (ratio_uy / ratio_ar − 1) × 100

        delta_ratio > 0 → el SKU está relativamente más caro en UY que
        en AR respecto al ancla. < 0 → relativamente más barato. ≈ 0
        → la estructura relativa coincide entre ambas listas.

        precio_uyu_teorico = precio_uyu_ancla × ratio_ar. Es el precio
        en UYU que el SKU debería tener si la estructura UY replicara
        exactamente la de AR (anclando ambas en el mismo SKU). Comparar
        contra precio_uyu real cuantifica el desvío en pesos.
    """
    sku_ancla = _norm_sku(sku_ancla)
    fila_ancla = df_cruzado[df_cruzado["sku"] == sku_ancla]
    if fila_ancla.empty:
        raise ValueError(f"SKU ancla {sku_ancla!r} no aparece en la lista cruzada")
    ancla = fila_ancla.iloc[0]

    precio_uy_ancla = ancla.get("precio_uy_cmp")
    precio_ar_ancla = ancla.get("precio_ar_cmp")
    if pd.isna(precio_uy_ancla) or pd.isna(precio_ar_ancla):
        raise ValueError(
            f"SKU ancla {sku_ancla!r} no tiene precio en ambas listas "
            f"(UY={precio_uy_ancla!r}, AR={precio_ar_ancla!r})"
        )
    if precio_uy_ancla <= 0 or precio_ar_ancla <= 0:
        raise ValueError(f"SKU ancla {sku_ancla!r} con precio no positivo")

    precio_uyu_ancla = ancla.get("precio_uyu")
    if pd.isna(precio_uyu_ancla) or precio_uyu_ancla <= 0:
        raise ValueError(
            f"SKU ancla {sku_ancla!r} sin precio UYU válido "
            f"(precio_uyu={precio_uyu_ancla!r})"
        )

    if skus_a_comparar is None:
        df = df_cruzado.copy()
    else:
        skus_set = {_norm_sku(s) for s in skus_a_comparar}
        df = df_cruzado[df_cruzado["sku"].isin(skus_set)].copy()

    df["ratio_uy"] = df["precio_uy_cmp"] / precio_uy_ancla
    df["ratio_ar"] = df["precio_ar_cmp"] / precio_ar_ancla
    df["delta_ratio"] = df["ratio_uy"] - df["ratio_ar"]
    df["delta_ratio_pct"] = (df["ratio_uy"] / df["ratio_ar"] - 1) * 100
    df["precio_uyu_teorico"] = precio_uyu_ancla * df["ratio_ar"]

    cols = [
        "sku", "nombre_uy", "rubro", "sub_rubro",
        "categoria_ar", "marca",
        "precio_uyu", "precio_uyu_teorico",
        "precio_uy_cmp", "precio_ar_cmp",
        "ratio_uy", "ratio_ar", "delta_ratio", "delta_ratio_pct",
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].copy()
    # El ancla primero, después el resto ordenado por ratio_uy descendente
    # (los más caros relativos al ancla arriba).
    es_ancla = df["sku"] == sku_ancla
    df = pd.concat(
        [df[es_ancla], df[~es_ancla].sort_values("ratio_uy", ascending=False)],
        ignore_index=True,
    )
    return df
