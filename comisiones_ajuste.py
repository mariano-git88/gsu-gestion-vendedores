"""
comisiones_ajuste.py — Ajuste retroactivo del mes anterior.

Detecta diferencias entre las cobranzas del mes M-1 que están hoy en
la API de Contabilium vs las que se registraron en el Sheet la última
vez que se liquidó M-1. Las diferencias generan un ajuste a aplicar
en el pago del mes M.

Tipos de diferencia:
  - Tardía: cobranza en API pero no en Sheet → ajuste positivo (sumar
    al pago del mes corriente). Se asigna con la cartera ACTUAL
    (huérfana → MARIO, sin vendedor → descartada, normal → vendedor
    asignado).
  - Anulada: cobranza en Sheet pero no en API → ajuste negativo
    (alerta, NO se descuenta — regla del legacy 2026-04-09).
  - Modificada: cobranza en ambos pero importe distinto → ajuste por
    el delta. Mantiene el vendedor que estaba en el Sheet (no se
    re-asigna).

El ajuste NO es `delta × 3%`. La comisión por cobranza de v1.2 es por
tramos (0% hasta $700.000, 3% hasta $1,5M, 4% sobre el excedente), así que
el ajuste es el **marginal sobre la base de M-1**:

    comision_cobranza(base_M1 + delta) − comision_cobranza(base_M1)

Es decir: qué comisión habría cobrado el vendedor en M-1 si esas cobranzas
se hubieran considerado, menos la que efectivamente cobró. Es la misma
fórmula que usa `commissions.compute_retroactive_adjustment` (camino legacy
xlsx); se había perdido al portar el módulo a la API, y con la tasa plana el
ajuste solo acertaba si el vendedor caía en el tramo del medio — pagaba de
más por debajo del umbral y de menos por encima de $1,5M.

`base_M1` es el total de cobranzas por vendedor del snapshot de M-1 que hay
en el Sheet: exactamente la base sobre la que se liquidó ese mes.

El output es compatible con `commissions.merge_commissions_with_adjustment`
del legacy — esa función ya maneja la regla "si ajuste < 0, NO descontar".
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

import api_loader
from commissions import (
    ESQUEMA_VIGENTE,
    UMBRAL_COBRANZA_PLENO,
    UMBRAL_COBRANZA_TIER_ALTO,
    VENDEDOR_HUERFANAS,
    comision_cobranza,
)


# =====================================================================
# Explicación para el vendedor
# =====================================================================

# Texto corto para mostrar cada mes que haya ajuste. Lo usa RRHH tal cual
# al explicarle el recibo a un vendedor — por eso está en segunda persona y
# sin jerga. Si cambian los umbrales de `commissions.py`, cambiar acá también.
EXPLICACION_AJUSTE = (
    "El ajuste se calcula con el escalón en el que quedaste ESE mes, no con "
    "el de hoy. Si las cobranzas que entraron tarde te hacen cruzar los "
    "$700.000, comisiona solo la parte que pasó esa línea; si te hacen cruzar "
    "$1.500.000, esa parte pasa al 4%. Si aun sumándolas quedaste abajo de "
    "$700.000, el ajuste da cero."
)


def cruces_de_escalon(ajuste: dict) -> list[dict]:
    """Vendedores cuyo ajuste los movió de escalón (o los dejó sin ninguno).

    Es lo que hay que poder explicar cuando alguien pregunta por qué su
    ajuste no es el 3% de lo que entró tarde. Devuelve una lista de
    {vendedor, tipo, base, final}, con `tipo` en:
      - "cruza_umbral":    pasó los $700.000 gracias a las tardías.
      - "cruza_tier_alto": pasó el $1,5M, así que parte va al 4%.
      - "sigue_bajo_umbral": ni con las tardías llegó — ajuste cero.
    Los vendedores que se quedaron dentro del mismo tramo no aparecen: ahí
    el ajuste es el 3% parejo y no hay nada que explicar.
    """
    base = ajuste.get("base_cobranzas_por_vendedor", {})
    out: list[dict] = []
    for v, d in ajuste.get("delta_importe_por_vendedor", {}).items():
        if d <= 0:
            continue
        b = base.get(v, 0.0)
        f = b + d
        if f <= UMBRAL_COBRANZA_PLENO:
            tipo = "sigue_bajo_umbral"
        elif b <= UMBRAL_COBRANZA_PLENO:
            tipo = "cruza_umbral"
        elif b <= UMBRAL_COBRANZA_TIER_ALTO < f:
            tipo = "cruza_tier_alto"
        else:
            continue
        out.append({"vendedor": v, "tipo": tipo, "base": b, "final": f})
    return out


# =====================================================================
# Detección
# =====================================================================

def detectar_diferencias(
    cobranzas_api: list[dict],
    cobranzas_sheet_df: pd.DataFrame,
) -> dict:
    """Compara cobranzas API vs Sheet para un mismo mes.

    Args:
        cobranzas_api: lista de items tal como los devuelve el endpoint
            de cobranzas (con keys 'Numero', 'NroDocumento',
            'RazonSocial', 'ImporteTotal', 'Fecha', 'Moneda').
        cobranzas_sheet_df: DataFrame con `gsheets.COBRANZAS_PAGADAS_COLUMNS`,
            ya filtrado al período M-1.

    Returns:
        dict con keys 'tardias', 'anuladas', 'modificadas' — cada uno
        es lista de dicts con info suficiente para reportar y calcular.
    """
    # Indexar por numero (clave estable de cobranza)
    api_por_nro: dict[str, dict] = {}
    for c in cobranzas_api:
        nro = str(c.get("Numero") or "").strip()
        if nro:
            api_por_nro[nro] = c

    sheet_por_nro: dict[str, dict] = {}
    if not cobranzas_sheet_df.empty:
        for _, r in cobranzas_sheet_df.iterrows():
            nro = str(r["numero"]).strip()
            if nro:
                sheet_por_nro[nro] = dict(r)

    nros_api = set(api_por_nro.keys())
    nros_sheet = set(sheet_por_nro.keys())

    tardias = []
    for nro in nros_api - nros_sheet:
        item = api_por_nro[nro]
        tardias.append({
            "numero": item.get("Numero"),
            "rut": str(item.get("NroDocumento") or "").strip(),
            "razon": item.get("RazonSocial"),
            "importe": api_loader.parse_monto_uy(item.get("ImporteTotal")),
            "fecha": item.get("Fecha"),
        })

    anuladas = []
    for nro in nros_sheet - nros_api:
        row = sheet_por_nro[nro]
        anuladas.append({
            "numero": nro,
            "vendedor": row.get("vendedor", ""),
            "rut": row.get("rut_cliente", ""),
            "razon": row.get("razon_social", ""),
            "importe": float(row.get("importe", 0.0)),
            "fecha": row.get("fecha_cobranza", ""),
        })

    modificadas = []
    for nro in nros_api & nros_sheet:
        item = api_por_nro[nro]
        row = sheet_por_nro[nro]
        importe_api = api_loader.parse_monto_uy(item.get("ImporteTotal"))
        importe_sheet = float(row.get("importe", 0.0))
        if abs(importe_api - importe_sheet) > 0.01:
            modificadas.append({
                "numero": nro,
                "vendedor": row.get("vendedor", ""),
                "rut": row.get("rut_cliente", ""),
                "razon": row.get("razon_social", ""),
                "importe_old": importe_sheet,
                "importe_new": importe_api,
                "delta": importe_api - importe_sheet,
                "fecha": row.get("fecha_cobranza", ""),
            })

    return {
        "tardias": tardias,
        "anuladas": anuladas,
        "modificadas": modificadas,
    }


# =====================================================================
# Cálculo del ajuste
# =====================================================================

def base_cobranzas_desde_sheet(cobranzas_sheet_df) -> dict[str, float]:
    """Total de cobranzas por vendedor del snapshot de M-1.

    Es la base sobre la que se liquidó ese mes — la que determina en qué
    tramo cae el ajuste. Usa el vendedor guardado en el Sheet, no la cartera
    actual: lo que importa acá es cuánto cobró efectivamente cada uno.
    Las filas sin vendedor (cliente descartado) no suman a nadie.
    """
    base: dict[str, float] = defaultdict(float)
    if cobranzas_sheet_df is None or cobranzas_sheet_df.empty:
        return {}
    for _, r in cobranzas_sheet_df.iterrows():
        v = str(r.get("vendedor") or "").strip()
        if v:
            base[v] += float(r.get("importe", 0.0) or 0.0)
    return dict(base)


def calcular_ajuste(
    diferencias: dict,
    mapa_clientes_actual: dict[str, str | None],
    cobranzas_api_total: float = 0.0,
    cobranzas_sheet_total: float = 0.0,
    base_cobranzas_por_vendedor: dict[str, float] | None = None,
    esquema: str = ESQUEMA_VIGENTE,
) -> dict:
    """Aplica reglas y devuelve un dict en el formato que espera
    `commissions.merge_commissions_with_adjustment`.

    Args:
        diferencias: output de `detectar_diferencias`.
        mapa_clientes_actual: dict {RUT: vendedor_email | None} (de
            `comisiones_data.cargar_clientes_para_comisiones`). Solo
            se usa para asignar las TARDÍAS.
        cobranzas_api_total: monto total de cobranzas en API M-1
            (para reporte).
        cobranzas_sheet_total: monto total de cobranzas en Sheet M-1
            (para reporte).
        base_cobranzas_por_vendedor: total de cobranzas de M-1 por vendedor
            tal como se liquidó (de `base_cobranzas_desde_sheet`). Determina
            en qué tramo cae el ajuste. Si no se pasa, se asume base cero —
            que bajo v1.2 deja el ajuste en 0 hasta los $700.000.
        esquema: "v1.2" (vigente, tramos) o "v1" (plano, para comparar).

    Returns:
        Dict compatible con merge_commissions_with_adjustment:
          - ajuste_comision_por_vendedor: {vendedor: comisión marginal
            sobre la base de M-1}
            (puede tener positivos y negativos; merge_commissions_with_adjustment
            aplica solo los positivos al pago final)
          - vendedores_con_ajuste_negativo: {vendedor: monto} subconjunto
            con valores < 0 (para alertar al usuario, no descontar).
          - cambios: lista de filas detalladas para el reporte.
          - total_orig: total de cobranzas en Sheet M-1.
          - total_actualizada: total de cobranzas en API M-1.
          - tardias_huerfanas_a_mario: lista de tardías que cayeron a MARIO.
          - tardias_descartadas: lista de tardías sin vendedor en cartera.
    """
    delta_importe: dict[str, float] = defaultdict(float)
    cambios: list[dict] = []
    huerfanas_tardias: list[dict] = []
    descartadas_tardias: list[dict] = []

    # ----- TARDÍAS: cartera actual -----
    for t in diferencias["tardias"]:
        rut = t["rut"]
        importe = t["importe"]
        if rut not in mapa_clientes_actual:
            delta_importe[VENDEDOR_HUERFANAS] += importe
            huerfanas_tardias.append(t)
            cambios.append({
                "tipo": "tardía huérfana",
                "numero": t["numero"],
                "codigo": rut,
                "razon": t["razon"],
                "importe_original": 0.0,
                "importe_nuevo": importe,
                "delta_importe": importe,
                "asignacion": f"→ {VENDEDOR_HUERFANAS}",
            })
        elif mapa_clientes_actual[rut] is None:
            descartadas_tardias.append(t)
            cambios.append({
                "tipo": "tardía descartada",
                "numero": t["numero"],
                "codigo": rut,
                "razon": t["razon"],
                "importe_original": 0.0,
                "importe_nuevo": importe,
                "delta_importe": importe,
                "asignacion": "(sin vendedor)",
            })
        else:
            v = mapa_clientes_actual[rut]
            delta_importe[v] += importe
            cambios.append({
                "tipo": "tardía",
                "numero": t["numero"],
                "codigo": rut,
                "razon": t["razon"],
                "importe_original": 0.0,
                "importe_nuevo": importe,
                "delta_importe": importe,
                "asignacion": v,
            })

    # ----- ANULADAS: vendedor del Sheet, ajuste NEGATIVO -----
    for a in diferencias["anuladas"]:
        v = a["vendedor"] or ""
        if v:
            delta_importe[v] += -a["importe"]
        cambios.append({
            "tipo": "anulada",
            "numero": a["numero"],
            "codigo": a["rut"],
            "razon": a["razon"],
            "importe_original": a["importe"],
            "importe_nuevo": 0.0,
            "delta_importe": -a["importe"],
            "asignacion": v,
        })

    # ----- MODIFICADAS: vendedor del Sheet, ajuste por delta -----
    for m in diferencias["modificadas"]:
        v = m["vendedor"] or ""
        if v:
            delta_importe[v] += m["delta"]
        cambios.append({
            "tipo": "modificada",
            "numero": m["numero"],
            "codigo": m["rut"],
            "razon": m["razon"],
            "importe_original": m["importe_old"],
            "importe_nuevo": m["importe_new"],
            "delta_importe": m["delta"],
            "asignacion": v,
        })

    # El ajuste es el MARGINAL sobre la base de M-1, no delta × 3%: la
    # comisión de cobranza v1.2 es por tramos, así que los mismos $10.000
    # tardíos valen 0, $300 o $400 según dónde haya quedado el vendedor ese
    # mes. Misma fórmula que `commissions.compute_retroactive_adjustment`.
    base = dict(base_cobranzas_por_vendedor or {})
    ajuste_por_vendedor = {}
    for v, d in delta_importe.items():
        b = base.get(v, 0.0)
        ajuste_por_vendedor[v] = round(
            comision_cobranza(b + d, esquema) - comision_cobranza(b, esquema), 2
        )
    delta_por_vendedor = {v: round(m, 2) for v, m in delta_importe.items()}
    # Cubrir el caso de que `build_xlsx_bytes` itere sobre las claves
    # de ajuste_por_vendedor: cada vendedor que está en una tiene que
    # estar en la otra. Por construcción ya lo están, pero resync por las dudas.
    for v in ajuste_por_vendedor:
        delta_por_vendedor.setdefault(v, 0.0)

    vendedores_negativos = {
        v: m for v, m in ajuste_por_vendedor.items() if m < -0.005
    }

    return {
        "ajuste_comision_por_vendedor": ajuste_por_vendedor,
        "delta_importe_por_vendedor": delta_por_vendedor,
        "vendedores_con_ajuste_negativo": vendedores_negativos,
        "cambios": cambios,
        "total_orig": round(cobranzas_sheet_total, 2),
        "total_actualizada": round(cobranzas_api_total, 2),
        "base_cobranzas_por_vendedor": {
            v: round(base.get(v, 0.0), 2) for v in ajuste_por_vendedor
        },
        "tardias_huerfanas_a_mario": huerfanas_tardias,
        "tardias_descartadas": descartadas_tardias,
    }


# =====================================================================
# Helper para construir la lista de cobranzas a guardar en el Sheet
# =====================================================================

def cobranzas_para_persistir(
    cobranzas_dict: dict,
) -> list[dict]:
    """Convierte el dict que devuelve `cargar_cobranzas_desde_api` a
    la lista de filas que `gsheets.write_cobranzas_periodo` espera.

    Itera por todos los detalles agregados (un dict por vendedor en
    cobranzas['detalle']) y los aplana. Las descartadas también se
    incluyen como filas con vendedor='' (importe pagado = 0 para esas,
    pero se registra el numero para que en una corrida futura no se
    re-detecte como tardía).
    """
    out = []
    for v, lista in cobranzas_dict.get("detalle", {}).items():
        for c in lista:
            out.append({
                "numero": c.get("numero", ""),
                "vendedor": v,  # MARIO si era huérfana, vendedor real si era directa
                "rut_cliente": c.get("codigo", ""),  # legado usa "codigo" pero contiene el RUT
                "razon_social": c.get("razon", ""),
                "fecha_cobranza": c.get("fecha", ""),
                "importe": float(c.get("importe", 0.0)),
            })
    # Descartadas (cliente sin vendedor) — registrar con vendedor=""
    for rut, razon, nro, imp in cobranzas_dict.get("descartadas_sin_vendedor", []):
        out.append({
            "numero": nro,
            "vendedor": "",
            "rut_cliente": rut,
            "razon_social": razon,
            "fecha_cobranza": "",
            "importe": float(imp),
        })
    return out
