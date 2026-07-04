"""
televentas_data.py — Capa de datos del CRM de Televentas.

Construye la lista de LEADS que trabaja la Vendedora Televentas, cruzando
dos fuentes de solo lectura de Contabilium:

  1. Maestro de clientes enriquecido (`/api/clientes/search`) — trae
     teléfono, email, ciudad, departamento (campo `Provincia` en UY),
     vendedor asignado, nombre de fantasía, observaciones de entrega.
  2. Historial de compras (facturación, `api_loader.load_fc_api`) — para
     derivar última compra, días sin comprar, ticket promedio, qué SKUs y
     subgrupos compró, etc.

El resultado es un DataFrame de leads con todo lo que la agente necesita
para decidir a quién llamar y qué ofrecer. La capa CRM (llamadas,
resultados, seguimientos) vive aparte en `televentas_crm.py` (Google
Sheet) y se mergea en la app por `documento`.

Funciones puras donde se puede: las agregaciones de compra reciben un
DataFrame y no tocan la red, para poder testearlas sin API.
"""

from __future__ import annotations

import pandas as pd

import api_loader

# Umbrales de segmentación (días sin comprar). Ajustables desde la app.
DIAS_ACTIVO = 90        # < 90 días → activo
DIAS_DORMIDO = 180      # 90–180 → dormido reciente; > 180 → dormido profundo


# =====================================================================
# 1. Maestro de clientes enriquecido (red)
# =====================================================================

def cargar_clientes_enriquecidos(
    session: api_loader.ApiSession,
    vendedores_map: dict[int, str] | None = None,
) -> tuple[api_loader.ApiSession, pd.DataFrame]:
    """Pullea TODOS los clientes con los campos ricos para el CRM.

    Devuelve (sesión, DataFrame) con una fila por cliente y columnas:
      documento, id_cliente, codigo, razon_social, nombre_fantasia,
      telefono, email, ciudad, departamento, domicilio, cp,
      id_lista_precio, vendedor, observaciones, condicion_iva.

    `departamento` = campo `Provincia` de Contabilium (en UY la provincia
    es el departamento). `vendedor` se resuelve del `IdUsuarioAdicional`
    vía `vendedores_map` (fallback "ID_<n>").
    """
    session, items = api_loader.api_paginate(session, "/api/clientes/search")
    vmap = vendedores_map or {}
    rows = []
    for c in items:
        vid = c.get("IdUsuarioAdicional")
        if vid is None or vid == 0:
            vendedor = ""
        else:
            try:
                vendedor = vmap.get(int(vid), f"ID_{vid}")
            except (TypeError, ValueError):
                vendedor = f"ID_{vid}"
        rows.append({
            "documento": str(c.get("NroDoc") or "").strip(),
            "id_cliente": c.get("Id"),
            "id_vendedor": vid,   # IdUsuarioAdicional crudo (para crear órdenes)
            "codigo": str(c.get("Codigo") or "").strip(),
            "razon_social": str(c.get("RazonSocial") or "").strip(),
            "nombre_fantasia": str(c.get("NombreFantasia") or "").strip(),
            "telefono": str(c.get("Telefono") or "").strip(),
            "email": str(c.get("Email") or "").strip(),
            "ciudad": str(c.get("Ciudad") or "").strip(),
            "departamento": str(c.get("Provincia") or "").strip(),
            "domicilio": str(c.get("Domicilio") or "").strip(),
            "cp": str(c.get("Cp") or "").strip(),
            "id_lista_precio": c.get("IdListaPrecio"),
            "vendedor": vendedor,
            "observaciones": str(c.get("Observaciones") or "").strip(),
            "condicion_iva": str(c.get("CondicionIva") or "").strip(),
        })
    df = pd.DataFrame(rows)
    return session, df


# =====================================================================
# 2. Resumen de compras por cliente (puro)
# =====================================================================

def resumen_compras(
    df_fc: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
    sku_subrubro_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Agrega la facturación en un resumen por cliente (por `documento`).

    Solo considera ventas reales (tipo == "FAC"): las notas de crédito no
    cuentan como "compra" para la actividad comercial.

    Devuelve DataFrame indexado por `documento` con columnas:
      ultima_compra (Timestamp), primera_compra, dias_sin_compra (int),
      num_facturas (int), monto_total (float), ticket_prom (float),
      skus_comprados (set[str]), subrubros_comprados (set[str]),
      top_skus (list[(sku, monto)] top 5).

    `df_fc` es la salida de `api_loader.load_fc_api` (columnas: documento,
    fecha, tipo, sku, monto, id_comprobante, ...). Trabaja sobre la ventana
    que se haya cargado (típicamente 12 meses).

    `sku_subrubro_map`: dict SKU → sub_rubro (del maestro de productos).
    Necesario para poblar `subrubros_comprados` porque `load_fc_api` NO trae
    el sub_rubro en la línea (la clasificación vive en el maestro). Si no se
    pasa, `subrubros_comprados` queda vacío pero el filtro por SKU igual anda.
    """
    smap = sku_subrubro_map or {}
    cols = [
        "ultima_compra", "primera_compra", "dias_sin_compra", "num_facturas",
        "monto_total", "ticket_prom", "skus_comprados", "subrubros_comprados",
        "top_skus",
    ]
    if df_fc is None or df_fc.empty:
        return pd.DataFrame(columns=cols)
    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    fac = df_fc[df_fc["tipo"] == "FAC"].copy()
    if fac.empty:
        return pd.DataFrame(columns=cols)
    fac["fecha"] = pd.to_datetime(fac["fecha"], errors="coerce")
    fac = fac[fac["fecha"].notna() & (fac["documento"].astype(str).str.len() > 0)]

    filas = []
    for doc, g in fac.groupby(fac["documento"].astype(str)):
        ultima = g["fecha"].max()
        primera = g["fecha"].min()
        n_fac = g["id_comprobante"].nunique() if "id_comprobante" in g.columns else len(g)
        monto = float(g["monto"].sum())
        skus = {s for s in g["sku"].astype(str) if s.strip()}
        # subrubros derivados del mapa SKU→subrubro (load_fc_api no los trae).
        subs = {smap[s] for s in skus if s in smap and smap[s]}
        top = (
            g[g["sku"].astype(str).str.len() > 0]
            .groupby("sku")["monto"].sum().sort_values(ascending=False)
            .head(5)
        )
        filas.append({
            "documento": doc,
            "ultima_compra": ultima,
            "primera_compra": primera,
            "dias_sin_compra": int((hoy - ultima).days),
            "num_facturas": int(n_fac),
            "monto_total": round(monto, 2),
            "ticket_prom": round(monto / n_fac, 2) if n_fac else 0.0,
            "skus_comprados": skus,
            "subrubros_comprados": subs,
            "top_skus": list(top.items()),
        })
    return pd.DataFrame(filas).set_index("documento")


# =====================================================================
# 2b. Resumen RÁPIDO por encabezados (sin el N+1 de detalle) — red
# =====================================================================

def cargar_headers_facturacion(
    session: api_loader.ApiSession, desde: str, hasta: str,
) -> tuple[api_loader.ApiSession, list[dict]]:
    """Pagina SOLO los encabezados de comprobantes (sin traer el detalle
    línea por línea). Es MUCHO más rápido que `load_fc_api` (que hace un
    GET por comprobante) y alcanza para los datos que el CRM necesita de
    entrada: última compra, ticket, antigüedad. El detalle de SKUs se
    carga aparte, opcional (`resumen_compras` + load_fc_api)."""
    path = f"/api/comprobantes/search?fechaDesde={desde}&fechaHasta={hasta}"
    return api_loader.api_paginate(session, path)


def resumen_compras_rapido(
    headers: list[dict],
    doc_by_id: dict[int, str],
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Resumen de compras por cliente a partir de los ENCABEZADOS.

    `headers`: items de /api/comprobantes/search (traen IdCliente,
    FechaEmision, ImporteTotalNeto (con IVA), TipoFc).
    `doc_by_id`: mapa IdCliente → documento (del maestro de clientes).

    Devuelve el MISMO schema que `resumen_compras` (indexado por
    documento), pero con `skus_comprados`/`subrubros_comprados`/`top_skus`
    VACÍOS — el detalle de productos se enriquece aparte, opcional.
    """
    cols = [
        "ultima_compra", "primera_compra", "dias_sin_compra", "num_facturas",
        "monto_total", "ticket_prom", "skus_comprados", "subrubros_comprados",
        "top_skus",
    ]
    if not headers:
        return pd.DataFrame(columns=cols)
    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    reg: dict[str, dict] = {}
    for h in headers:
        if str(h.get("TipoFc") or "").upper() != "FAC":
            continue
        idc = h.get("IdCliente")
        doc = doc_by_id.get(int(idc)) if idc is not None else None
        if not doc:
            continue
        fecha = api_loader.parse_fecha_iso(h.get("FechaEmision"))
        if fecha is None:
            continue
        monto = api_loader.parse_monto_uy(h.get("ImporteTotalNeto"))
        r = reg.setdefault(doc, {"ultima": fecha, "primera": fecha, "n": 0, "monto": 0.0})
        r["ultima"] = max(r["ultima"], fecha)
        r["primera"] = min(r["primera"], fecha)
        r["n"] += 1
        r["monto"] += monto

    filas = []
    for doc, r in reg.items():
        ultima = pd.Timestamp(r["ultima"])
        filas.append({
            "documento": doc,
            "ultima_compra": ultima,
            "primera_compra": pd.Timestamp(r["primera"]),
            "dias_sin_compra": int((hoy - ultima).days),
            "num_facturas": r["n"],
            "monto_total": round(r["monto"], 2),
            "ticket_prom": round(r["monto"] / r["n"], 2) if r["n"] else 0.0,
            "skus_comprados": set(),
            "subrubros_comprados": set(),
            "top_skus": [],
        })
    if not filas:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(filas).set_index("documento")


# =====================================================================
# 3. Construcción de la lista de leads (puro)
# =====================================================================

def _segmento(dias_sin_compra) -> str:
    """Etiqueta de segmento según días sin comprar."""
    if dias_sin_compra is None or pd.isna(dias_sin_compra):
        return "sin_compras"        # sin actividad en la ventana cargada
    d = int(dias_sin_compra)
    if d < DIAS_ACTIVO:
        return "activo"
    if d < DIAS_DORMIDO:
        return "dormido"            # 90–180: recuperable
    return "dormido_profundo"       # > 180


def construir_leads(
    df_clientes: pd.DataFrame,
    df_resumen: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Une el maestro de clientes con el resumen de compras → tabla de leads.

    Left join por `documento` (todos los clientes quedan; los que no
    compraron en la ventana tienen los campos de compra en NaN/sin_compras).
    Agrega la columna `segmento`.

    Devuelve el DataFrame de leads listo para filtrar/mostrar en la app.
    """
    if df_clientes is None or df_clientes.empty:
        return pd.DataFrame()

    df = df_clientes.copy()
    df["documento"] = df["documento"].astype(str)

    if df_resumen is not None and not df_resumen.empty:
        df = df.merge(df_resumen, how="left", left_on="documento", right_index=True)
    else:
        for c in ("ultima_compra", "primera_compra", "dias_sin_compra",
                  "num_facturas", "monto_total", "ticket_prom",
                  "skus_comprados", "subrubros_comprados", "top_skus"):
            df[c] = pd.NA

    df["segmento"] = df["dias_sin_compra"].apply(_segmento)
    # Columnas de conjunto: rellenar NaN con set/list vacíos para no romper filtros.
    for c in ("skus_comprados", "subrubros_comprados"):
        df[c] = df[c].apply(lambda v: v if isinstance(v, set) else set())
    df["top_skus"] = df["top_skus"].apply(lambda v: v if isinstance(v, list) else [])
    return df


# =====================================================================
# 4. Helpers de filtrado (puros)
# =====================================================================

def filtrar_leads(
    leads: pd.DataFrame,
    *,
    segmentos: list[str] | None = None,
    departamentos: list[str] | None = None,
    ciudades: list[str] | None = None,
    vendedores: list[str] | None = None,
    con_telefono: bool = False,
    compro_subrubro: str | None = None,
    no_compro_subrubro: str | None = None,
    compro_sku: str | None = None,
    dias_sin_compra_min: int | None = None,
    busqueda: str | None = None,
) -> pd.DataFrame:
    """Aplica los filtros del CRM sobre la tabla de leads. Todos opcionales
    y combinables (AND). Devuelve el subconjunto."""
    df = leads
    if segmentos:
        df = df[df["segmento"].isin(segmentos)]
    if departamentos:
        df = df[df["departamento"].isin(departamentos)]
    if ciudades:
        df = df[df["ciudad"].isin(ciudades)]
    if vendedores:
        df = df[df["vendedor"].isin(vendedores)]
    if con_telefono:
        df = df[df["telefono"].str.len() > 0]
    if compro_subrubro:
        df = df[df["subrubros_comprados"].apply(lambda s: compro_subrubro in s)]
    if no_compro_subrubro:
        df = df[df["subrubros_comprados"].apply(lambda s: no_compro_subrubro not in s)]
    if compro_sku:
        df = df[df["skus_comprados"].apply(lambda s: compro_sku in s)]
    if dias_sin_compra_min is not None:
        df = df[df["dias_sin_compra"].fillna(10**9) >= dias_sin_compra_min]
    if busqueda:
        q = busqueda.strip().lower()
        mask = (
            df["razon_social"].str.lower().str.contains(q, na=False)
            | df["nombre_fantasia"].str.lower().str.contains(q, na=False)
            | df["documento"].str.contains(q, na=False)
            | df["codigo"].str.lower().str.contains(q, na=False)
        )
        df = df[mask]
    return df.reset_index(drop=True)
