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

# Si el cliente compró hace menos de esto, probablemente su vendedor de
# calle lo atendió recién → televentas NO debería pisarlo (guardrail).
DIAS_ATENDIDO_RECIENTE = 15


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
        "monto_total", "ticket_prom", "deuda_total", "deuda_vencida",
        "skus_comprados", "subrubros_comprados", "top_skus",
    ]
    if not headers:
        return pd.DataFrame(columns=cols)
    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    reg: dict[str, dict] = {}
    for h in headers:
        idc = h.get("IdCliente")
        doc = doc_by_id.get(int(idc)) if idc is not None else None
        if not doc:
            continue
        r = reg.setdefault(doc, {"ultima": None, "primera": None, "n": 0,
                                 "monto": 0.0, "deuda": 0.0, "deuda_venc": 0.0})
        # Saldo (deuda) — sobre TODOS los comprobantes (FAC suma, NCF resta).
        saldo = api_loader.parse_monto_uy(h.get("Saldo"))
        r["deuda"] += saldo
        if saldo > 0.5:
            venc = api_loader.parse_fecha_iso(h.get("FechaVencimiento"))
            if venc is not None and pd.Timestamp(venc) < hoy:
                r["deuda_venc"] += saldo
        # Actividad de compra — solo FAC.
        if str(h.get("TipoFc") or "").upper() != "FAC":
            continue
        fecha = api_loader.parse_fecha_iso(h.get("FechaEmision"))
        if fecha is None:
            continue
        monto = api_loader.parse_monto_uy(h.get("ImporteTotalNeto"))
        r["ultima"] = fecha if r["ultima"] is None else max(r["ultima"], fecha)
        r["primera"] = fecha if r["primera"] is None else min(r["primera"], fecha)
        r["n"] += 1
        r["monto"] += monto

    filas = []
    for doc, r in reg.items():
        ultima = pd.Timestamp(r["ultima"]) if r["ultima"] else pd.NaT
        filas.append({
            "documento": doc,
            "ultima_compra": ultima,
            "primera_compra": pd.Timestamp(r["primera"]) if r["primera"] else pd.NaT,
            "dias_sin_compra": int((hoy - ultima).days) if r["ultima"] else None,
            "num_facturas": r["n"],
            "monto_total": round(r["monto"], 2),
            "ticket_prom": round(r["monto"] / r["n"], 2) if r["n"] else 0.0,
            "deuda_total": round(r["deuda"], 2),
            "deuda_vencida": round(r["deuda_venc"], 2),
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
                  "num_facturas", "monto_total", "ticket_prom", "deuda_total",
                  "deuda_vencida", "skus_comprados", "subrubros_comprados", "top_skus"):
            df[c] = pd.NA
    # Deuda puede faltar si el resumen vino de un camino que no la calcula.
    for c in ("deuda_total", "deuda_vencida"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    df["segmento"] = df["dias_sin_compra"].apply(_segmento)
    # Guardrail: comprado hace muy poco → atendido por su vendedor de calle.
    df["atendido_reciente"] = df["dias_sin_compra"].apply(
        lambda d: (d is not None and not pd.isna(d) and d < DIAS_ATENDIDO_RECIENTE))
    # Columnas de conjunto: rellenar NaN con set/list vacíos para no romper filtros.
    for c in ("skus_comprados", "subrubros_comprados"):
        df[c] = df[c].apply(lambda v: v if isinstance(v, set) else set())
    df["top_skus"] = df["top_skus"].apply(lambda v: v if isinstance(v, list) else [])
    return df


# =====================================================================
# 4. Helpers de filtrado (puros)
# =====================================================================

def sugerencias_producto(
    documento: str,
    df_fc: pd.DataFrame,
    sku_subrubro_map: dict[str, str],
    hoy: pd.Timestamp | None = None,
    n: int = 5,
) -> dict[str, list[tuple[str, str]]]:
    """Sugerencias de "próximo mejor producto" para un cliente. PURA.

    Requiere el detalle de facturación (df_fc, camino pesado/enriquecido).
    Devuelve dos listas de (sku, motivo):
      - "recompra": SKUs que el cliente compraba pero NO en los últimos 90
        días (dejó de comprar → reofrecer).
      - "cross": SKUs populares (por cantidad de clientes que los compran)
        DENTRO de los subgrupos que el cliente ya compra, que él todavía no
        lleva (venta cruzada relevante).
    """
    empty = {"recompra": [], "cross": []}
    if df_fc is None or df_fc.empty:
        return empty
    if hoy is None:
        hoy = pd.Timestamp.today().normalize()

    fac = df_fc[(df_fc["tipo"] == "FAC") & (df_fc["sku"].astype(str).str.len() > 0)].copy()
    if fac.empty:
        return empty
    fac["fecha"] = pd.to_datetime(fac["fecha"], errors="coerce")
    fac["documento"] = fac["documento"].astype(str)

    mine = fac[fac["documento"] == str(documento)]
    sus_skus = set(mine["sku"].astype(str))
    if not sus_skus:
        return empty
    sus_subs = {sku_subrubro_map.get(s) for s in sus_skus if sku_subrubro_map.get(s)}

    # Recompra: comprados alguna vez pero no en los últimos 90 días.
    recientes = set(mine[mine["fecha"] >= hoy - pd.Timedelta(days=90)]["sku"].astype(str))
    recompra = [s for s in sus_skus if s not in recientes][:n]

    # Cross-sell: populares en los subgrupos del cliente que él no compra.
    fac["sub"] = fac["sku"].astype(str).map(sku_subrubro_map)
    rel = fac[fac["sub"].isin(sus_subs)]
    pop = rel.groupby("sku")["documento"].nunique().sort_values(ascending=False)
    cross = [s for s in pop.index if s not in sus_skus][:n]

    return {
        "recompra": [(s, "dejó de comprarlo") for s in recompra],
        "cross": [(s, "popular en lo que ya compra") for s in cross],
    }


def matchear_seleccion(
    df_subido: pd.DataFrame, leads: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    """Cruza una planilla subida (de leads seleccionados) con los leads.

    Busca en el archivo una columna identificadora (documento/RUT o
    código/nro cliente) y matchea contra los leads por documento, por
    código exacto, o por el número del código (ej "4001" ↔ "04001-C").

    Devuelve (df_matched, no_encontrados):
      - df_matched: subset de `leads` (con las columnas del lead) para los
        clientes hallados, con columnas extra codigo/razon_social listas
        para persistir la importación.
      - no_encontrados: lista de identificadores del archivo sin match.
    """
    import re as _re
    if df_subido is None or df_subido.empty or leads is None or leads.empty:
        return leads.iloc[0:0], []

    # Detectar la columna identificadora.
    cols_norm = {str(c).strip().lower(): c for c in df_subido.columns}
    def _pick(*claves):
        for k in claves:
            for cn, orig in cols_norm.items():
                if k in cn:
                    return orig
        return None
    col_doc = _pick("documento", "rut", "cedula", "cédula", "ci")
    col_cod = _pick("codigo", "código", "nro cliente", "nro. cliente", "cod cliente", "cliente")
    col_id = col_doc or col_cod
    if col_id is None:
        col_id = df_subido.columns[0]  # fallback: primera columna

    # Índices de match sobre los leads.
    by_doc = {str(d).strip(): str(d).strip() for d in leads["documento"]}
    by_cod = {str(c).strip().upper(): str(d).strip()
              for c, d in zip(leads["codigo"], leads["documento"]) if str(c).strip()}
    by_num = {}
    for c, d in zip(leads["codigo"], leads["documento"]):
        digs = _re.sub(r"\D", "", str(c or ""))
        if digs:
            by_num[digs.lstrip("0") or "0"] = str(d).strip()

    docs, faltan = [], []
    for val in df_subido[col_id].astype(str):
        v = val.strip()
        if not v or v.lower() == "nan":
            continue
        vu = v.upper()
        vnum = _re.sub(r"\D", "", v).lstrip("0") or "0"
        doc = by_doc.get(v) or by_cod.get(vu) or by_num.get(vnum)
        if doc:
            docs.append(doc)
        else:
            faltan.append(v)

    docs_set = set(docs)
    matched = leads[leads["documento"].astype(str).isin(docs_set)].copy()
    return matched, faltan


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
    documentos: set[str] | None = None,
    ocultar_atendido_reciente: bool = False,
    solo_con_deuda: bool = False,
) -> pd.DataFrame:
    """Aplica los filtros del CRM sobre la tabla de leads. Todos opcionales
    y combinables (AND). Devuelve el subconjunto.

    `documentos`: si se pasa, restringe a esos documentos (usado para
    trabajar solo sobre una lista importada).
    `ocultar_atendido_reciente`: saca los que compraron hace muy poco
    (guardrail de no pisar al vendedor de calle).
    `solo_con_deuda`: deja solo los que tienen deuda pendiente."""
    df = leads
    if documentos is not None:
        df = df[df["documento"].astype(str).isin(documentos)]
    if ocultar_atendido_reciente and "atendido_reciente" in df.columns:
        df = df[~df["atendido_reciente"].fillna(False)]
    if solo_con_deuda and "deuda_total" in df.columns:
        df = df[df["deuda_total"].fillna(0) > 0.5]
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
