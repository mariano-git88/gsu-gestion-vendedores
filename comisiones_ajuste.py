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

El output es compatible con `commissions.merge_commissions_with_adjustment`
del legacy — esa función ya maneja la regla "si ajuste < 0, NO descontar".
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

import api_loader
from commissions import TASA_COBRANZA, VENDEDOR_HUERFANAS


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

def calcular_ajuste(
    diferencias: dict,
    mapa_clientes_actual: dict[str, str | None],
    cobranzas_api_total: float = 0.0,
    cobranzas_sheet_total: float = 0.0,
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

    Returns:
        Dict compatible con merge_commissions_with_adjustment:
          - ajuste_comision_por_vendedor: {vendedor: monto_comisión_3pct}
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
    ajuste: dict[str, float] = defaultdict(float)
    delta_importe: dict[str, float] = defaultdict(float)
    cambios: list[dict] = []
    huerfanas_tardias: list[dict] = []
    descartadas_tardias: list[dict] = []

    # ----- TARDÍAS: cartera actual -----
    for t in diferencias["tardias"]:
        rut = t["rut"]
        importe = t["importe"]
        if rut not in mapa_clientes_actual:
            ajuste[VENDEDOR_HUERFANAS] += importe * TASA_COBRANZA
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
            ajuste[v] += importe * TASA_COBRANZA
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
            ajuste[v] += -a["importe"] * TASA_COBRANZA
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
            ajuste[v] += m["delta"] * TASA_COBRANZA
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

    ajuste_por_vendedor = {v: round(m, 2) for v, m in ajuste.items()}
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
