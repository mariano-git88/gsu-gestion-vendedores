"""
rendicion_ejecutor.py — Fase 2 (ESCRITURA) de la Rendición de Cobranzas.

Ejecuta en Contabilium, para UNA cobranza aprobada, la secuencia:
  1. Crear la Nota de Crédito del 10% (descuento comercial) asociada a la
     factura → POST /api/comprobantes/anularComprobante.
  2. Crear el recibo e imputar la NC + el cobro (efectivo/cheque) contra la
     factura en una sola llamada → POST /api/comprobantes/cobrar, con un
     `Pagos[]` que mezcla el pago-NC (IDNotaCredito) y el pago real.

⚠️ ESTO ESCRIBE EN EL CONTABILIUM DE PRODUCCIÓN. Por eso:
  - `ejecutar()` corre en **dry_run=True por defecto**: arma y devuelve los
    payloads EXACTOS que se mandarían, sin tocar la API. Solo con
    dry_run=False escribe, y aun así el caller debe pasar un gate de
    confirmación explícito (ver rendicion_app.py).
  - Reutiliza los helpers HTTP write-safe de `facturador` (refresco de
    token + reintento SOLO en 401; nunca reintento ciego en 5xx/red, que
    podría duplicar una NC o un recibo).

La receta de la NC del 10% se obtuvo por reverse-engineering de las NCF de
"descuento comercial" reales (2026-07-02): TipoFc NCF, un único ítem de
texto libre `Concepto="10% DTO. COMERCIAL"`, `PrecioUnitario` = 10% del
NETO sin IVA de la factura, `Iva=22`. Total con IVA de la NC = 10% del
total con IVA de la factura.

CAMPOS AÚN INCIERTOS (a confirmar en la primera prueba real):
  - Si `anularComprobante` acepta el ítem de texto libre (IdConcepto null):
    el ejemplo del Postman manda un producto, pero las NC reales son texto
    libre. El facturador NO podía crear líneas libres para facturas.
  - `TipoFc` correcto en el body (la factura es "FAC"; el ejemplo usaba "FCA").
  - `IdUsuarioAdicional` (la factura de prueba trae 0).
  - Para el cobro: `IDCaja`/`IDBanco` de efectivo/cheque y cómo se referencia
    el cheque precargado (¿FormaDePago "Cheque" + NroReferencia?).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import api_loader
import facturador  # _get / _post write-safe (401 retry, sin retry ciego)

TASA_DESCUENTO = 0.10
IVA_BASICO = 22.0
CONCEPTO_NC = "10% DTO. COMERCIAL"

# IdCaja del efectivo en Suprabond — extraído de un recibo REAL de Valeria
# (recibo 0002-00013287, cobranza en efectivo). El cheque, en cambio, va con
# IdCaja=null / IdBanco=null y el nº de cheque en NroReferencia (validado en
# el recibo 0002-00013288, cheque BROU nº 332100). Ver memoria del proyecto.
IDCAJA_EFECTIVO = 824


class EjecutorError(Exception):
    """Falla al ejecutar una cobranza contra Contabilium."""


# =====================================================================
# Lectura de la factura (necesaria para armar el body de la NC)
# =====================================================================

def obtener_factura(
    session: api_loader.ApiSession, id_factura: int
) -> tuple[api_loader.ApiSession, dict]:
    """GET del detalle completo de la factura. Trae los campos de header
    (IdCliente, PuntoVenta, IDMoneda, TipoDeCambio, TipoFc, importes,
    Saldo) que se copian al body de la NC."""
    session, r = facturador._get(session, f"/api/comprobantes/?id={id_factura}")
    if r.status_code != 200:
        raise EjecutorError(
            f"No se pudo leer la factura id={id_factura}: HTTP {r.status_code} "
            f"| {r.text[:200]}"
        )
    return session, r.json()


# =====================================================================
# Plan de ejecución (puro, sin red) — arma los payloads
# =====================================================================

@dataclass
class PlanEjecucion:
    """Todo lo que se va a hacer para una cobranza, calculado antes de
    escribir. Sirve para previsualizar (dry-run) y para ejecutar."""
    id_factura: int
    numero_factura: str
    neto_factura: float        # ImporteTotalBruto (SIN IVA)
    total_con_iva: float       # ImporteTotalNeto (CON IVA)
    saldo_actual: float
    aplica_nc: bool
    nc_neto: float             # PrecioUnitario de la NC = 10% del neto
    nc_con_iva: float          # 10% del total con IVA
    cobro_efectivo: float
    cobro_cheque: float
    nro_cheque: str
    body_nc: dict | None       # None si no aplica NC (pago total)
    body_cobro: dict           # con placeholder de IDNotaCredito en dry-run
    advertencias: list[str] = field(default_factory=list)


def planificar(
    factura: dict,
    *,
    aplica_nc: bool,
    cobro_efectivo: float,
    cobro_cheque: float,
    nro_cheque: str,
    fecha_emision_iso: str,
) -> PlanEjecucion:
    """Construye el plan (payloads) para una cobranza. Función pura: no
    escribe ni lee de la red. `factura` es el detalle de `obtener_factura`.

    `fecha_emision_iso`: fecha de emisión de la NC (hoy), formato ISO
    "YYYY-MM-DDTHH:MM:SS". Se pasa desde afuera porque los scripts del
    entorno no pueden usar `datetime.now()`.
    """
    id_fac = factura.get("Id")
    numero = str(factura.get("Numero") or "").strip()
    neto = api_loader.parse_monto_uy(factura.get("ImporteTotalBruto"))   # sin IVA
    total_civa = api_loader.parse_monto_uy(factura.get("ImporteTotalNeto"))  # con IVA
    saldo = api_loader.parse_monto_uy(factura.get("Saldo"))

    nc_neto = round(neto * TASA_DESCUENTO, 2) if aplica_nc else 0.0
    nc_con_iva = round(total_civa * TASA_DESCUENTO, 2) if aplica_nc else 0.0

    advertencias: list[str] = []

    # --- Body de la NC (anularComprobante) ---
    body_nc: dict | None = None
    if aplica_nc:
        body_nc = {
            "Id": id_fac,
            "TipoFc": factura.get("TipoFc"),
            "Modo": factura.get("Modo") or "E",
            "IdUsuarioAdicional": factura.get("IdUsuarioAdicional") or 0,
            "IdCliente": factura.get("IdCliente"),
            "PuntoVenta": factura.get("PuntoVenta"),
            "CondicionVenta": factura.get("CondicionVenta") or "Contado",
            "FechaEmision": fecha_emision_iso,
            "IDMoneda": factura.get("IDMoneda"),
            "TipoDeCambio": factura.get("TipoDeCambio") or 1.0,
            "IDTurno": 0,
            "Items": [
                {
                    # Ítem de texto libre (sin IdConcepto), como las NC reales.
                    "Concepto": CONCEPTO_NC,
                    "Cantidad": 1,
                    "PrecioUnitario": nc_neto,
                    "Iva": IVA_BASICO,
                }
            ],
            "Observaciones": f"Descuento comercial 10% s/ {numero} (automatizado GSU)",
        }

    # --- Body del cobro/imputación (POST /api/comprobantes/cobrar) ---
    # `Pagos[]` = SOLO plata real. La NC NO va como forma de pago: el 1er test
    # real (2026-07-08) probó que Contabilium NO consume una NCF de descuento por
    # esa vía (el `IDNotaCredito` quedó None aunque el `IDCaja` sí se guardó, o
    # sea no es casing: rechaza la NC como pago) → queda como saldo a favor y el
    # asiento no balancea. La NC va IMPUTADA en el `Detalle` como línea NEGATIVA,
    # junto a la factura positiva — estructura confirmada leyendo recibos reales
    # (RC-00013332/13333: NC negativa + factura positiva, formas de pago = plata,
    # y el neto del Detalle == la plata; los centavos se cierran con una línea
    # {IDComprobante:0, Importe:<ajuste>}).
    # Valores de caja: EFECTIVO → IDCaja=824; CHEQUE → NroReferencia=<nº cheque>.
    pagos: list[dict] = []
    if cobro_efectivo > 0:
        pagos.append({
            "FormaDePago": "Efectivo", "IDBanco": None, "IDCaja": IDCAJA_EFECTIVO,
            "IDNotaCredito": None, "Importe": round(cobro_efectivo, 2),
            "NroReferencia": "", "IDComprobanteAsociado": "",
        })
    if cobro_cheque > 0:
        pagos.append({
            "FormaDePago": "Cheque", "IDBanco": None, "IDCaja": None,
            "IDNotaCredito": None, "Importe": round(cobro_cheque, 2),
            "NroReferencia": nro_cheque, "IDComprobanteAsociado": "",
        })

    cobrado = round(cobro_efectivo + cobro_cheque, 2)

    if aplica_nc:
        # Detalle explícito: factura (+) y NC (−). El neto debe dar igual a la
        # plata; el resto se reconcilia con la línea de ajuste {IDComprobante:0}.
        neto_detalle = round(total_civa - nc_con_iva, 2)
        ajuste = round(cobrado - neto_detalle, 2)
        detalle = [
            {"IDComprobante": id_fac, "Importe": round(total_civa, 2)},
            {"IDComprobante": "<ID_NC_A_CREAR>", "Importe": round(-nc_con_iva, 2)},
        ]
        if abs(ajuste) >= 0.005:
            detalle.append({"IDComprobante": 0, "Importe": ajuste})
        body_cobro = {
            "Id": id_fac,
            "ImporteTotalNeto": cobrado,   # el recibo vale la plata real
            "Saldo": 0,
            "Detalle": detalle,
            "Pagos": pagos,
        }
    else:
        # Pago total sin NC: el body simple ya funciona (Prueba A ✅).
        body_cobro = {
            "Id": id_fac,
            "ImporteTotalNeto": total_civa,
            "Saldo": 0,
            "Pagos": pagos,
        }

    # --- Chequeos de consistencia ---
    cobrado = round(cobro_efectivo + cobro_cheque, 2)
    suma_imputada = round(nc_con_iva + cobrado, 2)
    if abs(suma_imputada - total_civa) > 1.0:
        advertencias.append(
            f"NC ({nc_con_iva:,.2f}) + cobrado ({cobrado:,.2f}) = "
            f"{suma_imputada:,.2f} ≠ total con IVA ({total_civa:,.2f}). "
            "El saldo NO quedaría en 0."
        )
    if abs(saldo - total_civa) > 1.0:
        advertencias.append(
            f"La factura ya tiene saldo {saldo:,.2f} distinto del total "
            f"{total_civa:,.2f} (pago parcial previo). Revisar antes de imputar."
        )
    if cobro_cheque > 0 and not nro_cheque:
        advertencias.append("Cobro con cheque sin Nº de cheque.")

    return PlanEjecucion(
        id_factura=id_fac, numero_factura=numero, neto_factura=neto,
        total_con_iva=total_civa, saldo_actual=saldo, aplica_nc=aplica_nc,
        nc_neto=nc_neto, nc_con_iva=nc_con_iva, cobro_efectivo=cobro_efectivo,
        cobro_cheque=cobro_cheque, nro_cheque=nro_cheque, body_nc=body_nc,
        body_cobro=body_cobro, advertencias=advertencias,
    )


# =====================================================================
# Ejecución (ESCRITURA) — solo con dry_run=False
# =====================================================================

@dataclass
class ResultadoEjecucion:
    ok: bool
    dry_run: bool
    id_nc: int | None = None
    numero_nc: str | None = None
    pasos: list[str] = field(default_factory=list)   # log legible
    error: str | None = None
    resp_nc: dict | None = None
    resp_cobro: dict | None = None


def _crear_nc(
    session: api_loader.ApiSession, body: dict
) -> tuple[api_loader.ApiSession, dict]:
    session, r = facturador._post(session, "/api/comprobantes/anularComprobante", body)
    if r.status_code not in (200, 201):
        raise EjecutorError(
            f"anularComprobante devolvió HTTP {r.status_code}: {r.text[:300]}"
        )
    return session, (r.json() if r.text else {})


def _cobrar(
    session: api_loader.ApiSession, body: dict
) -> tuple[api_loader.ApiSession, dict]:
    session, r = facturador._post(session, "/api/comprobantes/cobrar", body)
    if r.status_code not in (200, 201):
        raise EjecutorError(
            f"cobrar devolvió HTTP {r.status_code}: {r.text[:300]}"
        )
    return session, (r.json() if r.text else {})


def _extraer_id(resp) -> int | None:
    """Id de un comprobante recién creado, tolerante al casing de Contabilium.

    Contabilium devuelve la clave del Id con casing inconsistente según el
    endpoint: `anularComprobante` devuelve **`idComprobante`** (minúsculas),
    otros usan `Id` / `ID` / `IdComprobante`. Confirmado en el 1er test real
    (2026-07-08): la NC se creó y devolvió `{'idComprobante': 2496173, ...}`,
    pero el parseo buscaba `IdComprobante` y quedaba None → cortaba antes del
    recibo. Ver [[feedback_contabilium_id_inconsistente]].
    """
    if not isinstance(resp, dict):
        return None
    for k, v in resp.items():
        if k.lower() in ("id", "idcomprobante") and v:
            return v
    return None


def _valor(resp, *claves):
    """Primer valor no vacío entre varias claves (case-insensitive extra)."""
    if not isinstance(resp, dict):
        return None
    for c in claves:
        if resp.get(c):
            return resp[c]
    low = {k.lower(): v for k, v in resp.items()}
    for c in claves:
        if low.get(c.lower()):
            return low[c.lower()]
    return None


def _saldo(session, id_comprobante) -> tuple[api_loader.ApiSession, float]:
    """Saldo pendiente de un comprobante (factura o NC). NaN si no se pudo leer.

    Se usa para verificar, después de escribir, que la factura quedó saldada y
    que la NC quedó consumida (ambas en ~0).
    """
    session, r = facturador._get(session, f"/api/comprobantes/?id={id_comprobante}")
    if r.status_code != 200:
        return session, float("nan")
    try:
        return session, api_loader.parse_monto_uy(r.json().get("Saldo"))
    except Exception:  # noqa: BLE001
        return session, float("nan")


def ejecutar(
    session: api_loader.ApiSession,
    plan: PlanEjecucion,
    *,
    dry_run: bool = True,
) -> tuple[api_loader.ApiSession, ResultadoEjecucion]:
    """Ejecuta el plan. Con `dry_run=True` (default) NO escribe: devuelve un
    resultado describiendo lo que haría. Con `dry_run=False` crea la NC y el
    recibo en Contabilium.

    Orden: primero la NC (para obtener su Id), luego el cobro imputando NC +
    efectivo/cheque. Si la NC se crea pero el cobro falla, queda una NC
    huérfana → se reporta su Id para poder revertirla manualmente.
    """
    res = ResultadoEjecucion(ok=False, dry_run=dry_run)

    if dry_run:
        res.pasos.append("DRY-RUN — no se escribió nada en Contabilium.")
        if plan.aplica_nc:
            res.pasos.append(
                f"1) Crearía NC 10% por {plan.nc_con_iva:,.2f} (con IVA) "
                f"vía POST /api/comprobantes/anularComprobante."
            )
        res.pasos.append(
            f"2) Imputaría {'NC + ' if plan.aplica_nc else ''}"
            f"cobro ({plan.cobro_efectivo + plan.cobro_cheque:,.2f}) "
            f"vía POST /api/comprobantes/cobrar → saldo 0."
        )
        res.ok = True
        return session, res

    # --- Salvaguarda: nunca imputar un cheque sin su número ---
    # El cheque se referencia en Contabilium por su NroReferencia (= nº de cheque).
    # Sin él quedaría un valor no referenciable. En opción 2 el número lo confirma
    # la UI antes de llegar acá; este guard es la red de seguridad del write path.
    if plan.cobro_cheque > 0 and not str(plan.nro_cheque or "").strip():
        res.error = "No se puede ejecutar: cobro con cheque sin Nº de cheque."
        res.pasos.append(f"ERROR: {res.error}")
        return session, res

    # --- LÍMITE DE LA API (confirmado 2026-07-08): NC no automatizable ---
    # La API pública NO puede imputar una Nota de Crédito en un recibo. Probado
    # en vivo de dos formas y ambas fallan:
    #   (1) NC como forma de pago en Pagos[] → Contabilium la deja como saldo a
    #       favor flotante y el asiento no balancea ("Debe y Haber no coinciden").
    #   (2) NC como línea negativa en un `Detalle` explícito del body de `cobrar`
    #       → `cobrar` IGNORA el Detalle (lo reconstruye desde `Id`+`Pagos`) y la
    #       NC queda flotando (factura pagada solo por el efectivo).
    # No hay endpoint público de imputación (revisado todo el Postman). El
    # Contabilium web lo hace con endpoints internos con sesión, no expuestos.
    # ⇒ Las cobranzas CON descuento (NC 10%) se cargan A MANO. La herramienta ya
    # deja todo calculado en el reporte. El caso SIN NC (pago total) sí se ejecuta.
    if plan.aplica_nc:
        res.error = (
            f"Cobranza con descuento 10%: cargala A MANO en Contabilium. La API "
            f"pública no permite imputar una Nota de Crédito en el recibo (el "
            f"`cobrar` solo aplica plata a una factura). Datos ya calculados: "
            f"NC ${plan.nc_con_iva:,.2f} (c/IVA) + cobro "
            f"${plan.cobro_efectivo + plan.cobro_cheque:,.2f}. No se creó ni la "
            f"NC ni el recibo. (El pago total sin NC sí se ejecuta solo.)"
        )
        res.pasos.append(f"NO EJECUTADO (NC no automatizable por la API): {res.error}")
        return session, res

    # --- ESCRITURA REAL ---
    id_nc = None
    try:
        if plan.aplica_nc:
            session, resp_nc = _crear_nc(session, plan.body_nc)
            res.resp_nc = resp_nc
            # Contabilium puede devolver los errores del comprobante en el body
            # aun con HTTP 200 → tratarlos como fallo.
            errs = _valor(resp_nc, "errores", "Errores")
            if errs:
                raise EjecutorError(f"anularComprobante devolvió errores: {errs}")
            id_nc = _extraer_id(resp_nc)
            res.id_nc = id_nc
            res.numero_nc = _valor(resp_nc, "Numero", "numero")
            res.pasos.append(f"NC creada: id={id_nc} nº={res.numero_nc}")
            if not id_nc:
                raise EjecutorError(
                    "NC creada pero no se pudo extraer su Id "
                    "[build: fix-idComprobante]. Claves devueltas: "
                    f"{list(resp_nc) if isinstance(resp_nc, dict) else resp_nc}"
                )
            # Rellenar el id real de la NC en su línea del Detalle.
            for d in plan.body_cobro.get("Detalle", []):
                if d.get("IDComprobante") == "<ID_NC_A_CREAR>":
                    d["IDComprobante"] = id_nc

        session, resp_cobro = _cobrar(session, plan.body_cobro)
        res.resp_cobro = resp_cobro
        errs_c = _valor(resp_cobro, "errores", "Errores")
        if errs_c:
            raise EjecutorError(f"cobrar devolvió errores: {errs_c}")
        res.pasos.append("Cobro/imputación OK.")

        # --- AUTO-VERIFICACIÓN post-escritura (red de seguridad) ---
        # No confiamos en el HTTP 200: verificamos por API que la factura quedó
        # saldada Y que la NC quedó CONSUMIDA (saldo ~0). Si la NC sigue con
        # saldo, quedó flotando (imputación fallida) → marcar para revertir a
        # mano, en el acto (no esperar a descubrirlo en el estado de cuenta).
        if plan.aplica_nc and id_nc:
            session, saldo_fac = _saldo(session, plan.id_factura)
            session, saldo_nc = _saldo(session, id_nc)
            res.pasos.append(
                f"Verificación: saldo factura = {saldo_fac:,.2f} (esperado 0) · "
                f"saldo NC = {saldo_nc:,.2f} (esperado 0)."
            )
            if abs(saldo_fac) > 1.0 or abs(saldo_nc) > 1.0:
                res.ok = False
                res.error = (
                    f"El recibo se creó PERO la imputación quedó MAL: saldo "
                    f"factura {saldo_fac:,.2f}, saldo NC {saldo_nc:,.2f} "
                    f"(ambos deberían ser 0). REVERTIR a mano el recibo y la NC "
                    f"(id={id_nc}) en Contabilium."
                )
                res.pasos.append(f"⚠️ {res.error}")
                return session, res

        res.ok = True
    except EjecutorError as e:
        res.error = str(e)
        res.pasos.append(f"ERROR: {e}")
        if id_nc:
            res.pasos.append(
                f"⚠️ Quedó una NC creada (id={id_nc}) SIN imputar. "
                "Revertir manualmente en Contabilium."
            )
    return session, res
