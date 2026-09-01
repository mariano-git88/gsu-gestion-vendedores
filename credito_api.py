"""
credito_api.py — Bajada de los datos que necesita el scoring crediticio.

Dos fuentes de Contabilium UY:
  - `/api/comprobantes/search` → facturas (headers; NO hace falta el detalle).
  - `/api/cobranzas/search` + `/api/cobranzas?id=` → recibos, que son los que
    traen la **fecha real de pago**, contra qué factura se imputa y con qué
    forma de pago se cobró.

Sin streamlit. Reusa `api_loader` para token, throttle y retries.

────────────────────────────────────────────────────────────────────────────
TRES TRAMPAS MEDIDAS EL 2026-08-13 (no asumidas)
────────────────────────────────────────────────────────────────────────────
1. **`/api/cobranzas/search` IGNORA la paginación.** `page`, `pageSize` y
   `skip` devuelven todos los MISMOS 50 primeros registros (orden descendente
   por fecha); `page=1` en particular tira un 404 con un error interno de
   .NET. La única salida es ventanear por DÍA. Si un día tiene más de 50
   recibos igual se trunca: se detecta comparando contra `TotalItems`, que sí
   viene bien, y se reporta — nunca se toma la página corta como el total.
   Medido: 10 días truncados en 24 meses, ~99 recibos de 12.409 (0,8%).

2. **`Pagos[]` del comprobante viene siempre vacío.** La fecha de pago NO
   está en la factura. Solo sale del recibo.

3. **`TotalPage` de `/api/comprobantes/search` no es la cantidad de páginas,
   es el tamaño de página** (50). Paginar hasta `TotalPage` deja la mitad de
   los meses afuera o hace el doble de llamadas. Se pagina hasta juntar
   `TotalItems` o hasta que una página no aporte nada nuevo.

Además, de `feedback_contabilium_trampas`: `ImporteTotalNeto` es el total
**CON IVA** (alineado con `Saldo` y con los importes de los recibos) e
`ImporteTotalBruto` es sin IVA. Están al revés del nombre. Acá se usa el
CON IVA, porque el crédito se otorga sobre lo que el cliente efectivamente
tiene que pagar.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from api_loader import ApiSession, api_get, api_paginate

# Máximo que devuelve `/api/cobranzas/search` por consulta, sin forma de
# paginar. Un resultado de este tamaño exacto es sospechoso por definición.
TOPE_COBRANZAS = 50
WORKERS_DETALLE = 10


@dataclass
class ReporteCarga:
    """Qué se pudo bajar y qué no. Se muestra en la app, no se esconde."""

    recibos: int = 0
    recibos_declarados: int = 0
    dias_truncados: list[str] = field(default_factory=list)
    detalles_fallados: int = 0
    comprobantes: int = 0
    meses_incompletos: list[str] = field(default_factory=list)

    @property
    def completo(self) -> bool:
        return (
            not self.dias_truncados
            and not self.meses_incompletos
            and self.detalles_fallados == 0
        )

    def resumen(self) -> str:
        if self.completo:
            return f"{self.comprobantes} comprobantes y {self.recibos} recibos — completo."
        faltan = max(self.recibos_declarados - self.recibos, 0)
        partes = [f"{self.comprobantes} comprobantes y {self.recibos} recibos"]
        if faltan:
            partes.append(f"faltan {faltan} recibos de días con más de 50")
        if self.detalles_fallados:
            partes.append(f"{self.detalles_fallados} detalles no bajaron")
        if self.meses_incompletos:
            partes.append(f"meses incompletos: {', '.join(self.meses_incompletos)}")
        return " · ".join(partes)


# ────────────────────────────────────────────────────────────────────────────
# Parsers (puros — se testean sin red)
# ────────────────────────────────────────────────────────────────────────────


def monto_uy(v) -> float:
    """'20.893,62' → 20893.62. Tolerante: basura o None → 0.0."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


COLS_COMPROBANTES = [
    "id", "id_cliente", "razon_social", "tipo", "numero", "emision",
    "vencimiento_erp", "cond_venta", "total", "saldo", "moneda", "tc",
    "id_vendedor",
]


def parse_comprobantes(headers: list[dict]) -> pd.DataFrame:
    """Headers crudos de `/api/comprobantes/search` → DataFrame canónico.

    `vencimiento_erp` se guarda solo para poder auditarlo: NO se usa para
    calcular mora, porque el ERP lo llena siempre con emisión + 30 días
    aunque la condición sea 60 o 90 (ver `credito.armar_historial`).
    """
    if not headers:
        return pd.DataFrame(columns=COLS_COMPROBANTES)
    return pd.DataFrame(
        [
            {
                "id": h.get("Id"),
                "id_cliente": h.get("IdCliente"),
                "razon_social": (h.get("RazonSocial") or "").strip(),
                "tipo": (h.get("TipoFc") or "").strip(),
                "numero": h.get("Numero"),
                "emision": pd.to_datetime(h.get("FechaEmision"), errors="coerce"),
                "vencimiento_erp": pd.to_datetime(
                    h.get("FechaVencimiento"), errors="coerce"
                ),
                "cond_venta": (h.get("CondicionVenta") or "").strip(),
                "total": monto_uy(h.get("ImporteTotalNeto")),  # CON IVA
                "saldo": monto_uy(h.get("Saldo")),             # CON IVA
                "moneda": h.get("IDMoneda"),
                "tc": h.get("TipoDeCambio"),
                "id_vendedor": h.get("IDVendedor"),
            }
            for h in headers
            if h.get("Id") is not None
        ],
        columns=COLS_COMPROBANTES,
    )


def parse_cobranzas(detalles: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Detalles de `/api/cobranzas?id=` → (imputaciones, formas de pago).

    imputaciones: id_recibo, fecha_pago, id_cliente, id_comprobante, importe.
      `id_comprobante == 0` es un pago a cuenta, sin factura asociada: se
      conserva (sirve para cuadrar) pero el historial lo ignora.

    formas: id_recibo, id_cliente, forma, importe.
      "Nota de credito" aparece como forma de pago y es la más frecuente en
      GSU. NO es plata que entró: es la NC del 10% de la rendición
      compensando la factura. Se marca aparte con `es_nota_credito` para que
      no infle el "mix de cobro" del cliente.
    """
    imp, formas = [], []
    for d in detalles:
        if not d:
            continue
        rid = d.get("ID")
        fecha = pd.to_datetime(d.get("Fecha"), errors="coerce")
        cli = d.get("IDPersona")
        for x in d.get("Detalle") or []:
            imp.append(
                {
                    "id_recibo": rid,
                    "fecha_pago": fecha,
                    "id_cliente": cli,
                    "id_comprobante": x.get("IDComprobante"),
                    "importe": float(x.get("Importe") or 0.0),
                }
            )
        for f in d.get("FormasDePago") or []:
            nombre = (f.get("FormaDePago") or "").strip()
            formas.append(
                {
                    "id_recibo": rid,
                    "id_cliente": cli,
                    "fecha_pago": fecha,
                    "forma": nombre,
                    "importe": float(f.get("Importe") or 0.0),
                    "es_nota_credito": "nota" in nombre.lower()
                    and "credito" in nombre.lower(),
                }
            )

    df_imp = pd.DataFrame(
        imp,
        columns=["id_recibo", "fecha_pago", "id_cliente", "id_comprobante", "importe"],
    )
    df_formas = pd.DataFrame(
        formas,
        columns=[
            "id_recibo", "id_cliente", "fecha_pago", "forma", "importe",
            "es_nota_credito",
        ],
    )
    return df_imp, df_formas


def parse_clientes(items: list[dict]) -> pd.DataFrame:
    """Maestro de clientes: documento, vendedor asignado, ciudad."""
    if not items:
        return pd.DataFrame(
            columns=["id_cliente", "razon_social", "documento", "tipo_doc",
                     "id_vendedor", "ciudad", "email", "telefono"]
        )
    return pd.DataFrame(
        [
            {
                "id_cliente": c.get("Id"),
                "razon_social": (c.get("RazonSocial") or "").strip(),
                "documento": str(c.get("NroDoc") or "").strip(),
                "tipo_doc": (c.get("TipoDoc") or "").strip(),
                "id_vendedor": c.get("IdUsuarioAdicional"),
                "ciudad": (c.get("Ciudad") or "").strip(),
                "email": (c.get("Email") or "").strip(),
                "telefono": (c.get("Telefono") or "").strip(),
            }
            for c in items
            if c.get("Id") is not None
        ]
    )


# ────────────────────────────────────────────────────────────────────────────
# Bajada
# ────────────────────────────────────────────────────────────────────────────


def _meses(d0: date, d1: date):
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        ini = date(y, m, 1)
        sig = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        yield max(ini, d0), min(sig - timedelta(days=1), d1)
        y, m = y + (m == 12), 1 if m == 12 else m + 1


def bajar_comprobantes(
    session: ApiSession,
    desde: date,
    hasta: date,
    reporte: ReporteCarga | None = None,
    progreso=None,
) -> tuple[ApiSession, pd.DataFrame, ReporteCarga]:
    """Headers de comprobantes del rango, mes por mes.

    `progreso` es un callable opcional `(hecho, total, texto)` para pintar una
    barra desde la app.
    """
    rep = reporte or ReporteCarga()
    meses = list(_meses(desde, hasta))
    todos: dict[int, dict] = {}

    for i, (ini, fin) in enumerate(meses):
        if progreso:
            progreso(i, len(meses), f"Facturas {ini:%Y-%m}")
        vistos: dict[int, dict] = {}
        total, page = None, 1
        while True:
            try:
                session, r = api_get(
                    session,
                    f"/api/comprobantes/search?page={page}"
                    f"&fechaDesde={ini}&fechaHasta={fin}",
                )
            except Exception:  # noqa: BLE001
                rep.meses_incompletos.append(f"{ini:%Y-%m}")
                break
            items = r.get("Items") or []
            if total is None:
                total = r.get("TotalItems") or 0
            nuevos = 0
            for h in items:
                hid = h.get("Id")
                if hid is not None and hid not in vistos:
                    vistos[hid] = h
                    nuevos += 1
            # Se corta cuando ya se juntó lo declarado o cuando una página no
            # aporta nada nuevo. NO se usa `TotalPage`: es el tamaño de página.
            if not items or nuevos == 0 or len(vistos) >= total:
                break
            page += 1
            if page > 400:
                rep.meses_incompletos.append(f"{ini:%Y-%m}")
                break
        if total and len(vistos) < total and f"{ini:%Y-%m}" not in rep.meses_incompletos:
            rep.meses_incompletos.append(f"{ini:%Y-%m}")
        todos.update(vistos)

    df = parse_comprobantes(list(todos.values()))
    rep.comprobantes = len(df)
    return session, df, rep


def bajar_cobranzas(
    session: ApiSession,
    desde: date,
    hasta: date,
    reporte: ReporteCarga | None = None,
    progreso=None,
) -> tuple[ApiSession, pd.DataFrame, pd.DataFrame, ReporteCarga]:
    """Recibos del rango, ventaneando por día (ver trampa 1 del encabezado).

    Devuelve (session, imputaciones, formas de pago, reporte).
    """
    rep = reporte or ReporteCarga()
    dias = (hasta - desde).days + 1
    headers: dict[int, dict] = {}

    for i in range(dias):
        d = desde + timedelta(days=i)
        if progreso and i % 5 == 0:
            progreso(i, dias, f"Recibos {d:%Y-%m-%d}")
        try:
            session, r = api_get(
                session, f"/api/cobranzas/search?fechaDesde={d}&fechaHasta={d}"
            )
        except Exception:  # noqa: BLE001
            rep.dias_truncados.append(f"{d} (falló)")
            continue
        items = r.get("Items") or []
        rep.recibos_declarados += int(r.get("TotalItems") or 0)
        if len(items) >= TOPE_COBRANZAS:
            # El endpoint no pagina: lo que hay arriba de 50 se pierde.
            rep.dias_truncados.append(str(d))
        for h in items:
            if h.get("ID") is not None:
                headers[h["ID"]] = h

    # --- detalles en paralelo (throttle off, como load_fc_api) -------------
    detalles: list[dict] = []
    ids = list(headers)

    def _det(cid):
        _, dd = api_get(session, f"/api/cobranzas?id={cid}", throttle=False)
        return dd

    with ThreadPoolExecutor(max_workers=WORKERS_DETALLE) as exe:
        futs = {exe.submit(_det, c): c for c in ids}
        hechos = 0
        for f in as_completed(futs):
            hechos += 1
            if progreso and hechos % 100 == 0:
                progreso(hechos, len(ids), "Detalle de recibos")
            try:
                detalles.append(f.result())
            except Exception:  # noqa: BLE001
                rep.detalles_fallados += 1

    rep.recibos = len(detalles)
    df_imp, df_formas = parse_cobranzas(detalles)
    return session, df_imp, df_formas, rep


def bajar_clientes(
    session: ApiSession, incluir_proveedores: bool = True
) -> tuple[ApiSession, pd.DataFrame]:
    """Maestro completo de clientes.

    OJO: `/api/clientes/search?pageSize=0` NO trae todo (devuelve 50 de 1.140);
    hay que paginar con `api_paginate`.

    **Y `/api/clientes/search` tampoco trae a todos los que facturan.** En
    Contabilium una persona puede estar clasificada como proveedor y aun así
    tener facturas de venta: esas fichas NO salen en el search de clientes,
    aunque `GET /api/clientes/?id=` sí las devuelve una por una. Medido el
    31-ago-2026: 6 de 942 clientes que facturaron en 12 meses estaban solo en
    proveedores, y entre ellos **SODIMAC, con $1,44M de saldo**. Sin esto sus
    facturas aparecen sin RUT, sin ciudad y sin teléfono.

    Con `incluir_proveedores=True` (default) se completan esas fichas desde
    `/api/proveedores/search`. Se deduplica por id dando prioridad a la ficha
    de cliente: si alguien está en las dos listas, manda la de cliente.
    """
    session, items = api_paginate(session, "/api/clientes/search")
    df = parse_clientes(items)
    if not incluir_proveedores:
        return session, df

    try:
        session, prov = api_paginate(session, "/api/proveedores/search")
    except Exception:  # noqa: BLE001
        # Un fallo acá no puede tumbar la carga entera: sin los proveedores
        # la deudora funciona igual, solo que esas fichas van sin contacto.
        return session, df

    df_prov = parse_clientes(prov)
    if df_prov.empty:
        return session, df
    faltantes = df_prov[~df_prov["id_cliente"].isin(set(df["id_cliente"]))]
    if faltantes.empty:
        return session, df
    return session, pd.concat([df, faltantes], ignore_index=True)
