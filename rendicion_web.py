"""
rendicion_web.py — Alta de recibos vía los endpoints INTERNOS del Contabilium web.

La API pública (`POST /api/comprobantes/cobrar`) NO puede imputar una Nota de
Crédito en un recibo: solo aplica plata a una factura (probado en vivo, la NC
queda flotando y el asiento no balancea). El Contabilium web SÍ lo hace, con
endpoints internos con sesión (`cobranzase.aspx`). Este módulo los replica para
el único caso que la API pública no cubre: **el recibo que imputa factura + NC**
(descuento comercial 10%). Validado en vivo 2026-07-09 (recibo 13345: factura y
NC quedaron en saldo 0).

⚠️ Endpoints NO documentados ni soportados por Contabilium: pueden cambiar sin
aviso y romper esto. Por eso el caller (`rendicion_ejecutor`) SIEMPRE
auto-verifica los saldos después de escribir (factura→0, NC→0) y, si algo quedó
mal, avisa para revertir a mano.

Auth: la app se loguea con usuario+contraseña de Contabilium (`login()`, POST a
internalapi.contabilium.com/api/login) y obtiene la sesión sola — Valeria escribe
sus credenciales una vez, sin pegar cookies. El token dura ~20h; si vence, se
reconecta. Ver `reference_contabilium_cobranza_web_interna` para la receta.

El `sessionId` que hilvana el borrador server-side lo genera el cliente (un GUID).
"""

from __future__ import annotations

import re
import uuid

import requests

BASE = "https://app.contabilium.com.uy"
INTERNALAPI = "https://internalapi.contabilium.com"
TIMEOUT = 40

# Constantes de Suprabond (extraídas de recibos/cobranzas reales).
IDPUNTOVENTA = "874"
IDCAJA_EFECTIVO = "824"
IDMONEDA_UYU = "794"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class WebError(Exception):
    """Falla al hablar con los endpoints internos del Contabilium web."""


class CookieExpirada(WebError):
    """La sesión venció o es inválida (hay que reconectar)."""


class LoginError(WebError):
    """Falló el login (usuario/contraseña incorrectos o servicio caído)."""


def login(email: str, password: str, country: str = "uy") -> str:
    """Se loguea en Contabilium con usuario+contraseña y devuelve el string de
    cookie de sesión listo para las llamadas del web (`Secure-1CBL` + `ASP.NET_SessionId`).

    Reemplaza el pegado manual de cookies: la app pide las credenciales una vez y
    llama a esto. Flujo (reverse-engineered de login.har 2026-07-09):
      1. POST internalapi.contabilium.com/api/login {email, password, country} →
         devuelve {"jwt": <token>} (el frontend lo usa como cookie Secure-1CBL).
      2. Se golpea una página .aspx con ese JWT para que el server cree el
         ASP.NET_SessionId (estado de sesión server-side).
    Lanza LoginError si las credenciales son inválidas.
    """
    if not (email or "").strip() or not (password or ""):
        raise LoginError("Falta usuario o contraseña.")
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    try:
        r = s.post(f"{INTERNALAPI}/api/login",
                   json={"email": email.strip(), "password": password, "country": country},
                   headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise WebError(f"login: error de red: {e}") from e
    if r.status_code != 200:
        raise LoginError(
            f"No se pudo iniciar sesión (HTTP {r.status_code}). "
            "Revisá el usuario y la contraseña de Contabilium."
        )
    try:
        jwt = (r.json() or {}).get("jwt")
    except ValueError:
        raise LoginError("Respuesta inesperada del login de Contabilium.")
    if not jwt:
        raise LoginError("Usuario o contraseña incorrectos.")
    # El JWT es la cookie Secure-1CBL en el dominio del app; con eso, pegarle a
    # una página .aspx hace que el server devuelva el ASP.NET_SessionId.
    s.cookies.set("Secure-1CBL", jwt, domain="app.contabilium.com.uy")
    try:
        s.get(f"{BASE}/cobranzase.aspx", timeout=TIMEOUT)
    except requests.RequestException as e:
        raise WebError(f"login: no se pudo abrir la sesión: {e}") from e
    sid = s.cookies.get("ASP.NET_SessionId")
    cookie = f"Secure-1CBL={jwt}"
    if sid:
        cookie += f"; ASP.NET_SessionId={sid}"
    return cookie


def _headers(cookie: str) -> dict:
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": _UA,
        "Origin": BASE,
        "Referer": f"{BASE}/cobranzase.aspx",
        "Cookie": cookie.strip(),
    }


def _post(cookie: str, path: str, body: dict) -> dict:
    """POST a un page-method y devuelve el JSON. Detecta cookie vencida.

    Los page-methods de ASP.NET devuelven `{"d": ...}`. Si la sesión venció, el
    server responde con un redirect/HTML de login (no JSON) o un 401/403.
    """
    if not cookie or not cookie.strip():
        raise CookieExpirada("No hay cookie de Contabilium configurada.")
    try:
        r = requests.post(BASE + path, headers=_headers(cookie), json=body,
                          timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        raise WebError(f"{path}: error de red: {e}") from e
    if r.status_code in (401, 403) or 300 <= r.status_code < 400:
        raise CookieExpirada(
            f"{path}: la cookie venció o es inválida (HTTP {r.status_code}). "
            "Re-pegá la cookie de Contabilium."
        )
    if r.status_code != 200:
        raise WebError(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
    txt = (r.text or "").lstrip()
    if txt.startswith("<"):  # HTML = redirect a login ⇒ sesión caída
        raise CookieExpirada(
            f"{path}: respondió HTML (login), la cookie venció. Re-pegala."
        )
    try:
        return r.json()
    except ValueError as e:
        raise WebError(f"{path}: respuesta no-JSON: {r.text[:200]}") from e


# =====================================================================
# Lecturas (sin escribir) — sirven para validar la cookie y prellenar
# =====================================================================

def obtener_ultimo_nro_recibo(cookie: str) -> str:
    """Próximo nº de recibo (RC) del punto de venta. Read-only."""
    j = _post(cookie, "/common.aspx/obtenerUltimoNroRecibo",
              {"tipoComprobante": "RC", "idPunto": int(IDPUNTOVENTA), "modo": ""})
    return j.get("d")


def obtener_comprobantes_pendientes(cookie: str, id_persona) -> list[dict]:
    """Comprobantes pendientes (facturas + NC) de un cliente. Read-only."""
    j = _post(cookie, "/cobranzase.aspx/obtenerComprobantesPendientes",
              {"id": int(id_persona), "idCobranza": 0})
    return j.get("d") or []


def obtener_cheques_disponibles(cookie: str, id_moneda: int = 794) -> list[dict]:
    """Cheques precargados disponibles para imputar (valores en cartera).
    Cada uno: {ID (id interno), Codigo (nº de cheque), Nombre, Importe, ...}."""
    j = _post(cookie, "/common.aspx/obtenerChequesCobranzas",
              {"EsPropio": False, "idMoneda": int(id_moneda)})
    return j.get("d") or []


def buscar_idcheque(cookie: str, nro_cheque: str, id_moneda: int = 794) -> str | None:
    """Busca el id interno (`idcheque`) de un cheque precargado por su NÚMERO.

    Contabilium referencia el cheque en el recibo por su id interno, no por el
    número; hay que resolverlo con `obtenerChequesCobranzas` matcheando `Codigo`.
    Devuelve el ID (str) o None si el cheque no está precargado.
    """
    objetivo = re.sub(r"\D", "", str(nro_cheque or ""))
    if not objetivo:
        return None
    for ch in obtener_cheques_disponibles(cookie, id_moneda):
        cod = re.sub(r"\D", "", str(ch.get("Codigo") or ""))
        if cod and cod == objetivo:
            return str(ch.get("ID"))
    return None


def verificar_cookie(cookie: str) -> tuple[bool, str]:
    """Chequea que la cookie autentique, con una llamada read-only barata.
    Devuelve (ok, mensaje)."""
    try:
        obtener_ultimo_nro_recibo(cookie)
        return True, "Cookie válida."
    except CookieExpirada as e:
        return False, str(e)
    except WebError as e:
        return False, f"No se pudo validar la cookie: {e}"


# =====================================================================
# Escritura del recibo (factura + NC)
# =====================================================================

def crear_recibo_con_nc(
    cookie: str,
    *,
    id_persona,
    id_factura,
    nombre_factura: str,
    saldo_factura: float,
    id_nc,
    total_nc: float,
    importe_efectivo: float,
    importe_cheque: float = 0.0,
    nro_cheque: str = "",
    idcheque: str = "",
    fecha_ddmmyyyy: str,
) -> dict:
    """Crea un recibo que imputa la factura (+) y la NC (−), cobrando en efectivo
    y/o cheque. Replica el flujo del web: clearInFo → agregarItem×2 → agregarForma
    → guardar. Devuelve {ok, id_recibo, nro_recibo, pasos}.

    Las formas de pago deben sumar el neto (factura − NC); eso lo calcula el
    caller (`rendicion_ejecutor`), acá solo se imputa lo recibido. Lanza
    WebError/CookieExpirada si algo falla (el caller revierte / avisa).
    """
    sid = str(uuid.uuid4())
    pasos: list[str] = []

    _post(cookie, "/cobranzase.aspx/clearInFo", {"sessionId": sid})
    pasos.append("clearInFo OK")

    _post(cookie, "/cobranzase.aspx/agregarItem", {
        "sessionId": sid, "id": 0, "idComprobante": str(id_factura),
        "comprobante": nombre_factura,
        "importe": str(round(saldo_factura, 2)), "saldo": str(round(saldo_factura, 2)),
        "idMonedaCobranza": IDMONEDA_UYU, "idMonedaDefecto": IDMONEDA_UYU,
        "tipoDeCambio": "1",
    })
    pasos.append(f"agregarItem factura +{saldo_factura:,.2f} OK")

    _post(cookie, "/cobranzase.aspx/agregarItem", {
        "sessionId": sid, "id": 0, "idComprobante": str(id_nc),
        "comprobante": f"NCF (Saldo: UYU -{total_nc:,.2f})",
        "importe": str(round(-total_nc, 2)), "saldo": str(round(-total_nc, 2)),
        "idMonedaCobranza": IDMONEDA_UYU, "idMonedaDefecto": IDMONEDA_UYU,
        "tipoDeCambio": "1",
    })
    pasos.append(f"agregarItem NC -{total_nc:,.2f} OK")

    if importe_cheque and importe_cheque > 0:
        # El cheque precargado se referencia por su id interno (idcheque), que el
        # caller resolvió con buscar_idcheque; el número va en nroRef.
        _post(cookie, "/cobranzase.aspx/agregarForma", {
            "sessionId": sid, "id": 0, "forma": "Cheque", "nroRef": nro_cheque or "",
            "importe": str(round(importe_cheque, 2)), "idcheque": str(idcheque or ""),
            "idBanco": "", "idNotaCredito": "", "idCaja": "", "fecha": fecha_ddmmyyyy,
            "importeBrutoNC": "0", "idComprobanteAsociado": "0", "ComprobanteAsociado": "",
        })
        pasos.append(f"agregarForma cheque {importe_cheque:,.2f} (nº {nro_cheque}, idcheque {idcheque}) OK")

    if importe_efectivo and importe_efectivo > 0:
        _post(cookie, "/cobranzase.aspx/agregarForma", {
            "sessionId": sid, "id": 0, "forma": "Efectivo", "nroRef": "",
            "importe": str(round(importe_efectivo, 2)), "idcheque": "", "idBanco": "",
            "idNotaCredito": "", "idCaja": IDCAJA_EFECTIVO, "fecha": fecha_ddmmyyyy,
            "importeBrutoNC": "0", "idComprobanteAsociado": "0", "ComprobanteAsociado": "",
        })
        pasos.append(f"agregarForma efectivo {importe_efectivo:,.2f} OK")

    nro = obtener_ultimo_nro_recibo(cookie)
    g = _post(cookie, "/cobranzase.aspx/guardar", {
        "sessionId": sid, "id": 0, "idPersona": int(id_persona), "tipo": "RC",
        "fecha": fecha_ddmmyyyy, "idPuntoVenta": IDPUNTOVENTA, "modo": "T",
        "nroComprobante": nro, "obs": "", "idMoneda": IDMONEDA_UYU,
        "tipoDeCambio": "1", "sobranteACuenta": False,
    })
    id_recibo = g.get("d")
    pasos.append(f"guardar OK: recibo {nro} (id {id_recibo})")
    if not id_recibo:
        raise WebError(f"guardar no devolvió id de recibo: {g}")
    return {"ok": True, "id_recibo": id_recibo, "nro_recibo": nro, "pasos": pasos}
