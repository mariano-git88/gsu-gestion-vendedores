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
import rendicion_web  # recibo con NC vía endpoints internos del web (cobranzase.aspx)

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
    body_cobro: dict           # solo para el caso SIN NC (cobrar público)
    id_cliente: int | None = None   # IdCliente = idPersona del web (recibo con NC)
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
        body_cobro=body_cobro, id_cliente=factura.get("IdCliente"),
        advertencias=advertencias,
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
    id_recibo: int | None = None       # id del recibo (caso con NC, vía web)
    numero_recibo: str | None = None   # nº del recibo
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
    cookie: str | None = None,
    fecha_ddmmyyyy: str | None = None,
) -> tuple[api_loader.ApiSession, ResultadoEjecucion]:
    """Ejecuta el plan. `dry_run=True` (default) NO escribe: describe lo que haría.

    Dos caminos según el descuento:
      - SIN NC (pago total): `POST /api/comprobantes/cobrar` (API pública).
      - CON NC (descuento 10%): HÍBRIDO → crea la NC por API pública
        (`anularComprobante`) y el recibo que la imputa por los endpoints
        INTERNOS del web (`rendicion_web`, requiere `cookie` de sesión), porque
        la API pública no puede imputar una NC.

    Siempre auto-verifica después de escribir (factura→0, y NC→0 si aplica). Si la
    NC se crea pero el recibo falla, se reporta su Id para revertir a mano.

    `fecha_ddmmyyyy`: fecha del recibo (hoy) en formato DD/MM/YYYY; la pasa el
    caller porque los scripts del entorno no usan datetime.now().
    """
    res = ResultadoEjecucion(ok=False, dry_run=dry_run)

    if dry_run:
        res.pasos.append("DRY-RUN — no se escribió nada en Contabilium.")
        if plan.aplica_nc:
            res.pasos.append(
                f"1) Crearía NC 10% por {plan.nc_con_iva:,.2f} (c/IVA) vía "
                f"POST /api/comprobantes/anularComprobante."
            )
            res.pasos.append(
                f"2) Crearía el recibo imputando factura + NC, cobrando "
                f"{plan.cobro_efectivo + plan.cobro_cheque:,.2f}, vía los endpoints "
                f"internos del web → factura y NC a saldo 0."
            )
        else:
            res.pasos.append(
                f"Imputaría el cobro ({plan.cobro_efectivo + plan.cobro_cheque:,.2f}) "
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

    # --- ESCRITURA REAL ---
    id_nc = None
    try:
        if plan.aplica_nc:
            # ===== HÍBRIDO: NC por API pública + recibo por web interno =====
            if not cookie:
                res.error = ("Conectate a Contabilium en la app (barra lateral 🔐, "
                             "usuario y contraseña) para el recibo con NC.")
                res.pasos.append(res.error)
                return session, res

            # CHEQUE + NC: el cheque precargado se referencia por su id interno
            # (idcheque), no por su número. Lo resolvemos ANTES de crear la NC; si
            # el cheque no está precargado en Contabilium, cortamos sin crear nada
            # (evita NC huérfana).
            idcheque = ""
            if plan.cobro_cheque > 0:
                try:
                    idcheque = rendicion_web.buscar_idcheque(cookie, plan.nro_cheque)
                except rendicion_web.WebError as e:
                    res.error = f"No se pudieron consultar los cheques de Contabilium: {e}"
                    res.pasos.append(res.error)
                    return session, res
                if not idcheque:
                    res.error = (
                        f"El cheque nº {plan.nro_cheque} no está precargado en "
                        "Contabilium (o el número no coincide). Cargalo primero y "
                        "reintentá. No se creó ninguna NC."
                    )
                    res.pasos.append(res.error)
                    return session, res
                res.pasos.append(
                    f"Cheque nº {plan.nro_cheque} encontrado (idcheque={idcheque}).")

            # 1) Crear la NC (API pública).
            session, resp_nc = _crear_nc(session, plan.body_nc)
            res.resp_nc = resp_nc
            errs = _valor(resp_nc, "errores", "Errores")
            if errs:
                raise EjecutorError(f"anularComprobante devolvió errores: {errs}")
            id_nc = _extraer_id(resp_nc)
            res.id_nc = id_nc
            if not id_nc:
                raise EjecutorError(f"NC creada pero sin Id reconocible: {resp_nc}")
            # Leer la NC recién creada: nº y saldo real (montos exactos).
            session, nc_det = api_loader.api_get(session, f"/api/comprobantes/?id={id_nc}")
            res.numero_nc = nc_det.get("Numero") if isinstance(nc_det, dict) else None
            total_nc = (api_loader.parse_monto_uy(nc_det.get("Saldo"))
                        if isinstance(nc_det, dict) else plan.nc_con_iva)
            res.pasos.append(f"NC creada: id={id_nc} nº={res.numero_nc} (${total_nc:,.2f})")

            # 2) Repartir la plata para que las formas sumen el neto (factura − NC).
            #    Política (con Mariano 2026-07-09): cierre exacto en $0; el cheque
            #    va como se rindió y el efectivo absorbe el redondeo (centavos).
            saldo_fac = plan.saldo_actual
            neto = round(saldo_fac - total_nc, 2)
            imp_cheque = round(plan.cobro_cheque, 2)
            # El efectivo absorbe el redondeo (cheque va como se rindió). Nunca
            # negativo: si el cheque cubre de más por centavos, no se manda
            # efectivo (queda un residuo mínimo que la auto-verificación tolera).
            imp_efectivo = max(0.0, round(neto - imp_cheque, 2))

            # 3) Crear el recibo por los endpoints internos del web.
            r = rendicion_web.crear_recibo_con_nc(
                cookie,
                id_persona=plan.id_cliente,
                id_factura=plan.id_factura,
                nombre_factura=f"FAC {plan.numero_factura} (Saldo: UYU {saldo_fac:,.2f})",
                saldo_factura=saldo_fac,
                id_nc=id_nc, total_nc=total_nc,
                importe_efectivo=imp_efectivo, importe_cheque=imp_cheque,
                nro_cheque=plan.nro_cheque, idcheque=idcheque,
                fecha_ddmmyyyy=fecha_ddmmyyyy,
            )
            res.pasos.extend(r["pasos"])
            res.id_recibo = r["id_recibo"]
            res.numero_recibo = r["nro_recibo"]

            # 4) Auto-verificación: factura y NC deben quedar en saldo ~0.
            session, sf = _saldo(session, plan.id_factura)
            session, sn = _saldo(session, id_nc)
            res.pasos.append(
                f"Verificación: saldo factura = {sf:,.2f} · saldo NC = {sn:,.2f} "
                "(ambos esperado 0)."
            )
            if abs(sf) > 1.0 or abs(sn) > 1.0:
                res.error = (
                    f"El recibo {res.numero_recibo} se creó PERO la imputación "
                    f"quedó MAL: saldo factura {sf:,.2f}, saldo NC {sn:,.2f} "
                    f"(ambos deberían ser 0). REVERTIR a mano el recibo y la NC "
                    f"(id={id_nc}) en Contabilium."
                )
                res.pasos.append(f"⚠️ {res.error}")
                return session, res
            res.ok = True
            return session, res

        # ===== SIN NC: cobro total por la API pública (funciona) =====
        session, resp_cobro = _cobrar(session, plan.body_cobro)
        res.resp_cobro = resp_cobro
        errs_c = _valor(resp_cobro, "errores", "Errores")
        if errs_c:
            raise EjecutorError(f"cobrar devolvió errores: {errs_c}")
        res.pasos.append("Cobro/imputación OK.")
        session, sf = _saldo(session, plan.id_factura)
        res.pasos.append(f"Verificación: saldo factura = {sf:,.2f} (esperado 0).")
        if abs(sf) > 1.0:
            res.error = (f"El recibo se creó pero la factura quedó con saldo "
                         f"{sf:,.2f} (esperado 0). Revisar en Contabilium.")
            res.pasos.append(f"⚠️ {res.error}")
            return session, res
        res.ok = True

    except rendicion_web.CookieExpirada as e:
        res.error = str(e)
        res.pasos.append(f"COOKIE VENCIDA: {e}")
        if id_nc and not res.id_recibo:
            res.pasos.append(
                f"⚠️ Quedó una NC creada (id={id_nc}) SIN imputar (el recibo no se "
                "llegó a crear). Revertir la NC a mano y reintentar con cookie nueva."
            )
    except (EjecutorError, rendicion_web.WebError) as e:
        res.error = str(e)
        res.pasos.append(f"ERROR: {e}")
        if id_nc and not res.id_recibo:
            res.pasos.append(
                f"⚠️ Quedó una NC creada (id={id_nc}) SIN imputar. "
                "Revertir manualmente en Contabilium."
            )
    return session, res
