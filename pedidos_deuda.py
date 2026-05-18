"""
pedidos_deuda.py — Identificación del cliente de un pedido contra
Contabilium (read-only). Módulo de lógica, sin Streamlit, igual que
`facturador.py` respecto de `facturador_app.py`.

Resuelve el "Nro. Cliente" que el vendedor escribe en el Excel
(ej. 4382) al cliente real de Contabilium vía el campo `Codigo`, cuyo
formato es `0XXXX-C` (5 dígitos con relleno + sufijo `-C`).

Mapeo validado contra el ERP real: el código `0XXXX-C` resuelve al
cliente correcto, cruzado contra su RUT y razón social como red de
seguridad (no se hardcodean datos de clientes acá: el repo es público).

NO escribe nada. La deuda vencida por cliente NO se re-inventa: se reusa
exactamente la maquinaria del dashboard de vendedores —
`api_loader.load_fc_api` (pull de facturación cacheable, igual que
`_api_sync_fc_historico` de app.py) + `metrics.aging_por_cliente`
(buckets 0-30/31-60/61-90/90+) — y se hace el join por `documento` (RUT),
que es la llave canónica de clientes en todo el proyecto.

Caveat honesto: `load_fc_api` pullea por FechaEmisión, así que la deuda
vencida que se ve es la de comprobantes emitidos dentro de la ventana
(default 12 meses). Una factura impaga emitida hace más de la ventana no
aparece — misma limitación que la tab Cobranzas del dashboard. La
ventana es un parámetro.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

import api_loader
import metrics


def codigo_contabilium(nro) -> str | None:
    """Convierte el 'Nro. Cliente' del Excel al `Codigo` de Contabilium.

    7 → "00007-C" · "123" → "00123-C". Devuelve None si no hay
    dígitos (celda vacía o texto raro), para que el llamador lo trate
    como "no identificable" en vez de cruzar cualquier cosa.
    """
    digitos = re.sub(r"\D", "", str(nro or ""))
    if not digitos:
        return None
    return f"{int(digitos):05d}-C"


def cargar_mapa_clientes(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, dict[str, dict]]:
    """Pull paginado de /api/clientes/search → {Codigo: {id, rut, razon_social}}.

    ~1000 clientes, un solo pull. El llamador lo cachea (cambia poco).
    """
    session, items = api_loader.api_paginate(session, "/api/clientes/search")
    mapa: dict[str, dict] = {}
    for c in items:
        cod = str(c.get("Codigo") or "").strip()
        if not cod:
            continue
        mapa[cod] = {
            "id": c.get("Id"),
            "rut": str(c.get("NroDoc") or "").strip(),
            "razon_social": str(c.get("RazonSocial") or "").strip(),
            "codigo": cod,
        }
    return session, mapa


def _norm(s: str) -> str:
    """Normaliza para comparar nombres: sin acentos, sin símbolos, minúsculas."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def identificar(nro_cliente, nombre_excel: str, mapa: dict[str, dict]) -> dict:
    """Identifica el cliente de un pedido y chequea que el nombre cuadre.

    Estados posibles:
      - "sin_codigo"     : el Excel no trae un Nro. Cliente usable.
      - "no_encontrado"  : el código no existe en Contabilium.
      - "revisar_nombre" : el código existe pero la razón social NO se
                           parece al nombre que escribió el vendedor
                           (posible Nro. Cliente equivocado → revisar).
      - "ok"             : código encontrado y nombre consistente.
    """
    cod = codigo_contabilium(nro_cliente)
    if cod is None:
        return {"estado": "sin_codigo", "codigo": None}
    cli = mapa.get(cod)
    if cli is None:
        return {"estado": "no_encontrado", "codigo": cod}
    a, b = _norm(nombre_excel), _norm(cli["razon_social"])
    coincide = bool(a) and bool(b) and (
        a in b or b in a or (len(a) >= 6 and len(b) >= 6 and a[:6] == b[:6])
    )
    return {
        "estado": "ok" if coincide else "revisar_nombre",
        "codigo": cod,
        **cli,
    }


_BUCKETS_VENC = ["b_0_30", "b_31_60", "b_61_90", "b_90_mas"]
_PEOR_ORDEN = [
    ("b_90_mas", "90+ días"),
    ("b_61_90", "61-90 días"),
    ("b_31_60", "31-60 días"),
    ("b_0_30", "0-30 días"),
]


def deuda_por_documento(
    session: api_loader.ApiSession, meses_atras: int = 12
) -> tuple[api_loader.ApiSession, dict[str, dict], list]:
    """Deuda viva por cliente (RUT), reusando la maquinaria del dashboard.

    Pullea facturación de los últimos `meses_atras` meses con
    `api_loader.load_fc_api` (mismo payload/parser que el dashboard) y
    corre `metrics.aging_por_cliente`. Devuelve:

      (sesión, {documento(RUT): {deuda_total, vencida, b_90_mas,
                                 peor_bucket}}, errores_del_pull)

    `vencida` = suma de los buckets 0-30/31-60/61-90/90+ (lo que ya pasó
    de plazo). `peor_bucket` es el tramo más viejo con saldo > 0.
    El pull es pesado (N+1): el llamador lo cachea (ttl ~24h).
    """
    hoy = dt.date.today()
    # Primero del mes, `meses_atras` meses para atrás (ventana limpia).
    total_m = (hoy.year * 12 + (hoy.month - 1)) - int(meses_atras)
    desde = dt.date(total_m // 12, total_m % 12 + 1, 1)
    session, df, errores = api_loader.load_fc_api(
        session, desde.isoformat(), hoy.isoformat()
    )
    aging = metrics.aging_por_cliente(df)
    out: dict[str, dict] = {}
    if aging.empty:
        return session, out, errores

    cols = _BUCKETS_VENC + ["al_dia", "sin_vencimiento", "deuda_total"]
    g = aging.groupby("documento", as_index=False)[cols].sum()
    for _, r in g.iterrows():
        doc = str(r["documento"]).strip()
        if not doc:
            continue
        vencida = float(sum(r[b] for b in _BUCKETS_VENC))
        peor = next(
            (etq for col, etq in _PEOR_ORDEN if float(r[col]) > 0.005), None
        )
        out[doc] = {
            "deuda_total": round(float(r["deuda_total"]), 2),
            "vencida": round(vencida, 2),
            "b_90_mas": round(float(r["b_90_mas"]), 2),
            "peor_bucket": peor,
        }
    return session, out, errores
