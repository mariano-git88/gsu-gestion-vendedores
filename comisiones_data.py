"""
comisiones_data.py — Cargadores API para el módulo de Comisiones.

Reemplaza a `load_clientes` / `load_ventas` / `load_cobranzas` del
`commissions.py` (que leen xlsx) con sus equivalentes contra la API
REST de Contabilium UY. Producen las MISMAS estructuras de salida
(diccionarios) que esperan `compute_commissions` y los builders del
módulo `commissions.py` — así la lógica de cálculo queda intacta.

Endpoints utilizados (descubiertos en discovery sesión 12, 2026-05-01):
  - /api/ordenesventa/search   — listado paginado, ya trae Vendedor.
  - /api/cobranzas/search       — requiere ?count=50 obligatorio.
  - /api/clientes/search        — vía load_clientes_api de api_loader.

Cambio respecto al schema legacy del xlsx:
  - El xlsx usaba `Codigo` (id interno de Contabilium) como llave
    cliente. La API de cobranzas NO expone ese campo, solo
    `NroDocumento` (RUT). El mapa de clientes ahora se construye
    con **RUT como llave**. La regla "cobranza con código inexistente
    → MARIO" sigue aplicando, evaluada contra RUT.
  - `Importe Total Neto` del xlsx → `ImporteTotal` de la API (mismo
    valor según confirmación de Mariano 2026-05-01: usar directo,
    sin dividir por 1.22).

Funciones puras: no importan streamlit, no escriben a disco. El
caching se aplica desde `comisiones_app.py` envolviéndolas con
`@st.cache_data`.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import api_loader
from commissions import (
    DIVISOR_IVA,
    ESTADOS_EXCLUIDOS,
    VENDEDOR_HUERFANAS,
    VENDEDORES_EXCLUIDOS_OP,
)
from vendedores import VENDEDORES


def _parse_fecha_uy_a_iso(s: str | None) -> str:
    """Convierte 'DD/MM/YYYY' (formato locale de la API) a 'YYYY-MM-DD'.

    Devuelve string vacío si el input es None o vacío. Si no se puede
    parsear, devuelve el valor crudo (resilient).
    """
    if not s:
        return ""
    try:
        partes = str(s).split("/")
        if len(partes) != 3:
            return str(s)
        d, m, y = partes
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    except (ValueError, AttributeError):
        return str(s)


# =====================================================================
# Cargador de clientes (mapa RUT → vendedor + set de vendedores válidos)
# =====================================================================

def cargar_clientes_para_comisiones(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, dict[str, str | None], set[str]]:
    """Pullea el maestro de clientes y construye los lookups que el
    cálculo de comisiones necesita.

    Returns (session, mapa, valid_vendors):
        - mapa: dict {NroDoc (RUT): vendedor_email | None}
          None significa "cliente sin vendedor asignado" — esas
          cobranzas se descartan según la regla original.
        - valid_vendors: set de vendedores que aparecen al menos una
          vez como Vendedor Asignado con valor no nulo. Las ventas de
          vendedores fuera de este set se excluyen del cálculo (regla
          decisión 2026-04-09 del proyecto Comisiones).

    Vendedores con email que arranca con "ID_" se tratan como None
    (son IDs no mapeados — clientes huérfanos de ex-vendedores).
    """
    session, df_cli = api_loader.load_clientes_api(
        session, vendedores_map=VENDEDORES,
    )

    mapa: dict[str, str | None] = {}
    for _, r in df_cli.iterrows():
        doc = str(r["documento"] or "").strip()
        if not doc:
            continue
        vend = r["vendedor"]
        # Normalizar: vacío o "ID_<n>" → None (no comisionable)
        if not vend or str(vend).startswith("ID_"):
            vend = None
        mapa[doc] = vend

    valid_vendors = {v for v in mapa.values() if v is not None}
    return session, mapa, valid_vendors


# =====================================================================
# Cargador de ventas (órdenes de venta del período)
# =====================================================================

def cargar_ventas_desde_api(
    session: api_loader.ApiSession,
    fecha_desde: str,
    fecha_hasta: str,
    valid_vendors: set[str],
) -> tuple[api_loader.ApiSession, dict]:
    """Pullea órdenes del período y devuelve la estructura legacy de
    `commissions.load_ventas`.

    Args:
        fecha_desde, fecha_hasta: 'YYYY-MM-DD'.
        valid_vendors: set de vendedores con clientes vinculados (de
            `cargar_clientes_para_comisiones`).

    Returns:
        Dict con keys:
          - brutas: {vendedor: total con IVA}
          - netas:  {vendedor: total sin IVA = bruto / 1.22}
          - detalle: {vendedor: [filas...]}
          - excluidas: {vendedor_op, cancelada, vendedor_invalido}
          - bruto_excluido_invalido: {vendedor: monto bruto excluido}
          - monedas_no_uyu: [(numero_orden, moneda)]
    """
    path = (
        f"/api/ordenesventa/search"
        f"?fechaDesde={fecha_desde}&fechaHasta={fecha_hasta}"
    )
    session, items = api_loader.api_paginate(session, path)

    ventas_brutas: dict = defaultdict(float)
    ventas_netas: dict = defaultdict(float)
    detalle: dict = defaultdict(list)
    excluidas = {"vendedor_op": 0, "cancelada": 0, "vendedor_invalido": 0}
    bruto_excluido_invalido: dict = defaultdict(float)
    monedas_no_uyu: list = []

    for it in items:
        vend = (it.get("Vendedor") or "").strip()
        estado = it.get("Estado", "")
        moneda = it.get("Moneda", "")

        # Validación de moneda — todo debe ser UYU.
        if moneda and str(moneda).upper() != "UYU":
            monedas_no_uyu.append((it.get("NumeroOrden"), moneda))
            continue

        # Filtros en orden — deben aplicarse en este orden para que
        # los contadores coincidan con el flujo legacy.
        if vend in VENDEDORES_EXCLUIDOS_OP:
            excluidas["vendedor_op"] += 1
            continue
        if estado in ESTADOS_EXCLUIDOS:
            excluidas["cancelada"] += 1
            continue
        if vend not in valid_vendors:
            excluidas["vendedor_invalido"] += 1
            bruto_excluido_invalido[vend] += api_loader.parse_monto_uy(
                it.get("Total")
            )
            continue

        total_b = api_loader.parse_monto_uy(it.get("Total"))
        total_n = total_b / DIVISOR_IVA
        ventas_brutas[vend] += total_b
        ventas_netas[vend] += total_n
        detalle[vend].append(
            {
                "numero": it.get("NumeroOrden"),
                "fecha": _parse_fecha_uy_a_iso(it.get("FechaCreacion")),
                "comprador": it.get("Comprador"),
                "estado": estado,
                "total_bruto": total_b,
                "total_neto": total_n,
            }
        )

    return session, {
        "brutas": dict(ventas_brutas),
        "netas": dict(ventas_netas),
        "detalle": dict(detalle),
        "excluidas": excluidas,
        "bruto_excluido_invalido": dict(bruto_excluido_invalido),
        "monedas_no_uyu": monedas_no_uyu,
    }


# =====================================================================
# Cargador de cobranzas
# =====================================================================

def cargar_cobranzas_desde_api(
    session: api_loader.ApiSession,
    fecha_desde: str,
    fecha_hasta: str,
    mapa_clientes: dict[str, str | None],
) -> tuple[api_loader.ApiSession, dict]:
    """Pullea cobranzas del período y devuelve la estructura legacy de
    `commissions.load_cobranzas`.

    El endpoint `/api/cobranzas/search` tiene una API rota que NO
    respeta paginación (ni `page`, ni `skip`, ni `offset`, ni `count`
    como tamaño) — siempre devuelve las primeras 50 cobranzas del
    rango. Pero **SÍ filtra correctamente por fecha** cuando se pasa
    un rango chico. Workaround: dividir el rango en días y hacer un
    GET por día en paralelo. Ver discovery 2026-05-01 (smoke 5).

    Cada día tiene un tope de 50 cobranzas por response del servidor.
    Si algún día llega a 50, hay riesgo de cobranzas perdidas y la
    función levanta `ApiError` con mensaje útil.

    Llave de match para asignar vendedor: `NroDocumento` (RUT). Si el
    RUT no está en `mapa_clientes` → cobranza huérfana → MARIO. Si
    está pero el cliente no tiene vendedor → se descarta.

    Returns:
        Dict con keys:
          - por_vend: {vendedor: total cobrado}
          - detalle:  {vendedor: [filas...]}
          - huerfanas_a_mario: [(rut, razon, numero, importe)]
          - descartadas_sin_vendedor: [(rut, razon, numero, importe)]
          - monedas_no_uyu: [(numero, moneda)]

    En `detalle` y los reportes la columna se sigue llamando "codigo"
    aunque ahora sea el RUT — el rename en `commissions.py` y los
    builders es opcional y se puede hacer en una iteración futura.
    """
    # Pre-refrescar el token antes del batch paralelo (igual patrón
    # que load_fc_api en api_loader). Evita race conditions si varios
    # threads detectan token expirado al mismo tiempo.
    session = api_loader._refrescar_si_expirado(session)

    # Lista de fechas ISO entre desde y hasta (inclusive).
    fd = date.fromisoformat(fecha_desde)
    fh = date.fromisoformat(fecha_hasta)
    if fh < fd:
        raise ValueError(f"fecha_hasta ({fecha_hasta}) < fecha_desde ({fecha_desde})")
    n_dias = (fh - fd).days + 1
    fechas = [(fd + timedelta(days=i)).isoformat() for i in range(n_dias)]

    items: list[dict] = []
    session_snapshot = session

    def _fetch_dia(fecha: str) -> tuple[str, list[dict]]:
        path = (
            f"/api/cobranzas/search"
            f"?fechaDesde={fecha}&fechaHasta={fecha}&count=50"
        )
        _, payload = api_loader.api_get(session_snapshot, path)
        if not isinstance(payload, dict):
            raise api_loader.ApiError(
                f"Respuesta inesperada en cobranzas {fecha}: "
                f"{type(payload).__name__}"
            )
        dia_items = payload.get("Items", []) or []
        # Cap del servidor a 50 — si llegamos al máximo, hay riesgo
        # de cobranzas perdidas silenciosamente. Fail explícito.
        if len(dia_items) >= 50:
            raise api_loader.ApiError(
                f"El día {fecha} tiene {len(dia_items)} cobranzas "
                f"(cap del servidor). Hay riesgo de cobranzas perdidas; "
                f"subdividir por horas o filtrar por cliente."
            )
        return fecha, dia_items

    # ThreadPool de 10 workers (mismo que load_fc_api).
    with ThreadPoolExecutor(max_workers=10) as exe:
        futures = {exe.submit(_fetch_dia, f): f for f in fechas}
        for future in as_completed(futures):
            _fecha, dia_items = future.result()
            items.extend(dia_items)

    por_vend: dict = defaultdict(float)
    detalle: dict = defaultdict(list)
    huerfanas_a_mario: list = []
    descartadas_sin_vendedor: list = []
    monedas_no_uyu: list = []

    for it in items:
        rut = str(it.get("NroDocumento") or "").strip()
        imp = api_loader.parse_monto_uy(it.get("ImporteTotal"))
        razon = it.get("RazonSocial")
        nro = it.get("Numero")
        fecha = _parse_fecha_uy_a_iso(it.get("Fecha"))
        moneda = it.get("Moneda", "")

        if moneda and str(moneda).upper() != "UYU":
            monedas_no_uyu.append((nro, moneda))
            continue

        if rut not in mapa_clientes:
            # Cobranza huérfana → MARIO (decisión 2026-04-09)
            por_vend[VENDEDOR_HUERFANAS] += imp
            detalle[VENDEDOR_HUERFANAS].append(
                {
                    "codigo": rut,
                    "razon": razon,
                    "numero": nro,
                    "fecha": fecha,
                    "importe": imp,
                    "asignacion": "huerfana_a_mario",
                }
            )
            huerfanas_a_mario.append((rut, razon, nro, imp))
        elif mapa_clientes[rut] is None:
            descartadas_sin_vendedor.append((rut, razon, nro, imp))
        else:
            v = mapa_clientes[rut]
            por_vend[v] += imp
            detalle[v].append(
                {
                    "codigo": rut,
                    "razon": razon,
                    "numero": nro,
                    "fecha": fecha,
                    "importe": imp,
                    "asignacion": "directa",
                }
            )

    return session, {
        "por_vend": dict(por_vend),
        "detalle": dict(detalle),
        "huerfanas_a_mario": huerfanas_a_mario,
        "descartadas_sin_vendedor": descartadas_sin_vendedor,
        "monedas_no_uyu": monedas_no_uyu,
    }
