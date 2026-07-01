"""
api_loader.py — Cliente de API de Contabilium para GSU Uruguay.

Este módulo es el puente entre la API REST de Contabilium
(`https://rest.contabilium.com.uy`) y los DataFrames que consume el
resto del dashboard. Es el equivalente dinámico de `data_loader.py`
(que hoy lee .xlsx subidos por el usuario).

Funciones públicas expuestas a este nivel (Tanda A):
  - obtener_token(client_id, client_secret) -> ApiSession
  - api_get(session, path) -> (session_refrescada, payload)
  - api_paginate(session, path_sin_page) -> (session_refrescada, lista_de_items)
  - parse_monto_uy(s) -> float
  - parse_fecha_iso(s) -> datetime | None

En las tandas siguientes se agregan:
  - Tanda B: load_clientes_api, load_productos_api, load_combos_api
  - Tanda C: load_fc_api (con concurrencia para el N+1 de GetById)
  - Tanda D: utilidades de mapping (IDVendedor, IdSubrubro)

Principios:
  - No importa streamlit. El cache se aplica desde `app.py` envolviendo
    estas funciones con `@st.cache_data`.
  - Funciones puras: reciben credenciales o sesión como parámetros, no
    leen de st.secrets ni de variables globales.
  - Mismo estilo de excepciones que `data_loader.py` (jerarquía propia,
    mensajes amigables al usuario).

Referencias:
  - claude.md.txt (sección "Reglas de carga y tipos de dato")
  - _learning/decisions.md (entrada 2026-04-17 sobre integración API)
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import requests

# =====================================================================
# Constantes
# =====================================================================

BASE_URL = "https://rest.contabilium.com.uy"
USER_AGENT = "GSU-Dashboard/1.0"
DEFAULT_TIMEOUT = 30  # segundos por request
MAX_RETRIES = 5       # para errores transitorios (429, 5xx, red)
EXPIRY_MARGIN = 60    # regenerar token 60s antes de que venza

# --- Control de rate limit (Contabilium devuelve 429 si vamos rápido) ---
# El sync de maestros dispara cientos de requests paginadas seguidas
# (clientes + conceptos + stock por depósito). Sin espaciarlas, Contabilium
# corta con HTTP 429. Dos defensas:
#   1. MIN_REQUEST_INTERVAL: pausa mínima proactiva entre requests para no
#      superar el límite de ráfaga.
#   2. RETRY_BASE_429: al recibir 429, esperar bastante (respetando el
#      header Retry-After si viene) porque el límite es por ventana de
#      tiempo y un backoff corto no alcanza a que se reponga.
MIN_REQUEST_INTERVAL = 0.35  # segundos mínimos entre dos GET consecutivos
RETRY_BASE_429 = 6           # backoff base (seg) por intento ante un 429
_last_request_ts = 0.0       # timestamp del último GET (throttle global)

# IVA básico UY 22%. `PrecioFinal` de Contabilium viene bruto con IVA;
# para que el "valor de stock" sea comparable con el `monto` neto del
# resto del dashboard, dividimos por este factor al cargar el maestro.
IVA_BASICO_UY = 1.22

# Depósitos que cuentan como "stock disponible" para la tab Inventario.
# El stock de Inventario NO es el consolidado de todos los depósitos de
# Contabilium: solo se cuentan las unidades DISPONIBLES (físico menos
# reservado = StockConReservas) en estos dos (decisión 2026-06-30).
# Match por nombre normalizado (.strip().upper()) porque el ERP devuelve
# " MFLEX" con un espacio adelante.
DEPOSITOS_INVENTARIO = ("VENTAS", "MFLEX")


# =====================================================================
# Excepciones
# =====================================================================

class ApiError(Exception):
    """Error genérico al comunicarse con la API de Contabilium."""


class AuthError(ApiError):
    """Credenciales inválidas, plan insuficiente, o API no habilitada."""


# =====================================================================
# Sesión autenticada
# =====================================================================

@dataclass
class ApiSession:
    """Sesión autenticada contra la API de Contabilium.

    Encapsula el access_token, las credenciales (necesarias para
    regenerar el token si expira a mitad de un sync) y el timestamp
    de expiración absoluto (epoch seconds).

    Se crea con `obtener_token(client_id, client_secret)`. El resto
    del módulo espera recibir una `ApiSession` como primer parámetro.
    """
    client_id: str
    client_secret: str
    access_token: str
    expires_at: float

    def is_expired(self) -> bool:
        """True si el token venció o está a menos de EXPIRY_MARGIN de vencer."""
        return time.time() > (self.expires_at - EXPIRY_MARGIN)


# =====================================================================
# Parseos de formatos UY / Contabilium
# =====================================================================

def parse_monto_uy(valor) -> float:
    """Convierte un monto con formato locale UY/AR a float.

    Contabilium devuelve los montos como string con punto como separador
    de miles y coma como decimal: "1.260,00", "-826,45", "35.000,00".

    Casos manejados:
      - None o "" → 0.0
      - int/float → convertido directamente
      - string con formato locale → parseado
      - string inválido → 0.0 (no levanta excepción)
    """
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip()
    if not s:
        return 0.0
    # Remover separador de miles (punto), cambiar decimal (coma → punto).
    cleaned = s.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_fecha_iso(valor) -> datetime | None:
    """Parsea una fecha ISO de Contabilium a datetime naive.

    Contabilium emite dos variantes:
      - "2026-03-02T00:00:00"           (sin microsegundos)
      - "2026-03-02T08:35:51.193"       (con microsegundos, precisión ms)

    Devuelve None si el valor es vacío, None o no parseable.
    """
    if not valor:
        return None
    s = str(valor).strip()
    if not s:
        return None
    try:
        if "." in s:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


# =====================================================================
# Autenticación
# =====================================================================

def obtener_token(client_id: str, client_secret: str) -> ApiSession:
    """Obtiene un access_token vía OAuth2 client_credentials.

    El token dura 24h (expires_in=86399). Levanta AuthError si las
    credenciales son rechazadas; ApiError si hay un error de red o una
    respuesta inesperada del servidor.

    Ver docs Contabilium: POST https://rest.contabilium.com.uy/token,
    form-urlencoded con grant_type, client_id, client_secret.
    """
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        r = requests.post(
            f"{BASE_URL}/token",
            data=data,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        raise ApiError(f"Error de red al autenticar contra Contabilium: {e}") from e

    if r.status_code in (400, 401, 403):
        raise AuthError(
            f"Contabilium rechazó las credenciales (HTTP {r.status_code}). "
            "Verificar client_id (email admin) y client_secret (API Key de "
            "Mi Cuenta → Datos de mi empresa → API). Requiere plan Full o superior."
        )
    if r.status_code != 200:
        raise ApiError(
            f"Respuesta inesperada al autenticar: HTTP {r.status_code}. "
            f"Cuerpo: {r.text[:300]}"
        )
    payload = r.json()
    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 86400))
    if not token:
        raise ApiError(f"Respuesta de /token sin access_token: {payload}")
    return ApiSession(
        client_id=client_id,
        client_secret=client_secret,
        access_token=token,
        expires_at=time.time() + expires_in,
    )


def _refrescar_si_expirado(session: ApiSession) -> ApiSession:
    if session.is_expired():
        return obtener_token(session.client_id, session.client_secret)
    return session


# =====================================================================
# Cliente HTTP con retry
# =====================================================================

def _throttle() -> None:
    """Espacia los GET para no gatillar el rate limit (429) de Contabilium.

    Mantiene al menos MIN_REQUEST_INTERVAL segundos entre requests
    consecutivos usando un timestamp global. Es una defensa proactiva:
    evita el 429 en vez de recuperarse de él.
    """
    global _last_request_ts
    ahora = time.time()
    espera = MIN_REQUEST_INTERVAL - (ahora - _last_request_ts)
    if espera > 0:
        time.sleep(espera)
    _last_request_ts = time.time()


def _retry_after_segundos(response: requests.Response, fallback: float) -> float:
    """Devuelve cuántos segundos esperar tras un 429.

    Usa el header `Retry-After` (en segundos) si el servidor lo manda; si
    no viene o no es parseable, cae en `fallback`.
    """
    valor = response.headers.get("Retry-After")
    if valor:
        try:
            return max(float(valor), fallback)
        except (TypeError, ValueError):
            pass
    return fallback


def api_get(session: ApiSession, path: str) -> tuple[ApiSession, dict | list]:
    """GET autenticado con retry en errores transitorios.

    Parámetros:
      session: ApiSession activa.
      path: ruta relativa a BASE_URL, ej. "/api/clientes/search?page=1".

    Devuelve una tupla (sesión_posiblemente_refrescada, payload). La
    sesión puede venir refrescada si el token había expirado o si el
    servidor devolvió 401 (el token puede invalidarse antes de tiempo).

    Levanta ApiError si el servidor responde 4xx no recuperable, o si
    después de MAX_RETRIES sigue fallando por errores transitorios.
    """
    session = _refrescar_si_expirado(session)
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {session.access_token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    last_error = "desconocido"
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            last_error = f"error de red ({e})"
            if attempt == MAX_RETRIES:
                raise ApiError(f"GET {path}: {last_error} tras {MAX_RETRIES} intentos") from e
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 200:
            return session, r.json()

        if r.status_code == 401:
            # Token rechazado. Regenerar y reintentar UNA vez sin contar
            # contra el presupuesto de retries (se cuenta implícitamente
            # por el loop).
            session = obtener_token(session.client_id, session.client_secret)
            headers["Authorization"] = f"Bearer {session.access_token}"
            last_error = "401 (token regenerado)"
            continue

        if r.status_code == 429:
            last_error = "HTTP 429"
            if attempt == MAX_RETRIES:
                raise ApiError(f"GET {path}: {last_error} tras {MAX_RETRIES} intentos")
            # Backoff más largo: el 429 es por ventana de tiempo. Respetar
            # Retry-After si el servidor lo manda; si no, backoff creciente.
            time.sleep(_retry_after_segundos(r, RETRY_BASE_429 * attempt))
            continue

        if r.status_code >= 500:
            last_error = f"HTTP {r.status_code}"
            if attempt == MAX_RETRIES:
                raise ApiError(f"GET {path}: {last_error} tras {MAX_RETRIES} intentos")
            time.sleep(2 ** attempt)
            continue

        # 4xx definitivo (404, 400, etc.)
        raise ApiError(
            f"GET {path} devolvió HTTP {r.status_code}: {r.text[:300]}"
        )
    raise ApiError(f"GET {path}: {last_error} tras {MAX_RETRIES} intentos")


def api_paginate(
    session: ApiSession,
    path_sin_page: str,
    *,
    max_pages: int = 200,
) -> tuple[ApiSession, list[dict]]:
    """Recorre todas las páginas de un endpoint de búsqueda.

    Asume que el endpoint devuelve un objeto con forma:
        {"Items": [...], "TotalItems": N, "TotalPage": M}

    En Contabilium UY, `TotalPage` representa el tamaño de página
    (items por página), NO el número total de páginas — por eso el loop
    se corta cuando `len(items_acumulados) >= TotalItems` o cuando una
    página viene vacía.

    Parámetros:
      session: ApiSession activa.
      path_sin_page: path con query params excepto `page`. Ejemplos:
          "/api/clientes/search"
          "/api/comprobantes/search?fechaDesde=2026-03-01&fechaHasta=2026-03-31"
      max_pages: tope de seguridad anti-loop infinito.

    Devuelve (sesión_actualizada, lista_completa_de_items).
    """
    all_items: list[dict] = []
    sep = "&" if "?" in path_sin_page else "?"
    for page in range(1, max_pages + 1):
        path = f"{path_sin_page}{sep}page={page}"
        session, payload = api_get(session, path)
        if not isinstance(payload, dict):
            raise ApiError(
                f"Respuesta inesperada en api_paginate: {path} "
                f"devolvió {type(payload).__name__}, se esperaba dict con 'Items'"
            )
        items = payload.get("Items", [])
        if not items:
            break
        all_items.extend(items)
        total_items = payload.get("TotalItems")
        if total_items is not None and len(all_items) >= total_items:
            break
    return session, all_items


# =====================================================================
# Loaders de maestros (Tanda B)
# =====================================================================
#
# Cada loader API produce un DataFrame con el MISMO schema que el loader
# xlsx correspondiente en data_loader.py. Así, el resto del pipeline
# (transforms, metrics, views, exports) sigue funcionando sin cambios
# independientemente de la fuente de datos.
#
# Mapping de IDs a valores humanos:
#   - Contabilium expone identificadores numéricos (IdUsuarioAdicional,
#     IdSubrubro, IdRubro) en lugar de emails/códigos cortos. Los
#     diccionarios de traducción se pasan como parámetros opcionales
#     (`vendedores_map`, `subrubros_map`, `rubros_map`).
#   - Si el mapping no se pasa, o si un ID no está en el dict, cae en
#     fallback `"ID_<n>"` como string — así el pipeline no rompe por
#     NaN y es trivial ver qué falta mapear.
#   - Los mappings definitivos se arman en Tanda D (vendedores.py,
#     subrubros.py) y se inyectan desde app.py en Tanda E.


def _fetch_all_conceptos(session: ApiSession) -> tuple[ApiSession, list[dict]]:
    """Pullea TODOS los conceptos (productos + combos + servicios) paginado.

    Helper compartido por load_productos_api y load_combos_api para no
    duplicar la llamada a /api/conceptos/search cuando se usan juntos.
    """
    return api_paginate(session, "/api/conceptos/search")


def load_stock_depositos(
    session: ApiSession,
    nombres: tuple[str, ...] = DEPOSITOS_INVENTARIO,
) -> tuple[ApiSession, dict[int, float]]:
    """Suma el stock disponible (StockConReservas) de un conjunto de depósitos.

    Para la tab Inventario, "stock disponible" = unidades disponibles
    (físico menos reservado) en los depósitos VENTAS y MFLEX, NO el
    consolidado de todos los depósitos que trae el campo `Stock` de
    /api/conceptos/search.

    Flujo:
        1. GET /api/inventarios/getDepositos → lista de depósitos.
           Se resuelven los IDs cuyos nombres (normalizados con
           .strip().upper()) están en `nombres`. El ERP devuelve
           " MFLEX" con espacio adelante, por eso el .strip().
        2. Para cada depósito resuelto, paginar
           GET /api/inventarios/getStockByDeposito?id=<dep> que devuelve
           {Items:[{Id(concepto), Codigo(SKU), StockActual,
           StockReservado, StockConReservas}], TotalItems}.
        3. Acumular la suma de StockConReservas (= StockActual −
           StockReservado) por `Id` de concepto a lo largo de los depósitos.

    Devuelve (sesión, dict[concepto_id → stock_disponible_sumado]). Los
    conceptos sin stock en estos depósitos simplemente no aparecen en el
    dict (el caller resuelve con .get(id, 0.0)).

    Si alguno de los nombres pedidos no existe en el ERP, lo avisa por
    stderr pero no rompe (procesa los que sí encontró). Si NINGUNO
    matchea, levanta ApiError (configuración inválida).
    """
    session, depositos = api_get(session, "/api/inventarios/getDepositos")
    if not isinstance(depositos, list):
        raise ApiError(
            "getDepositos devolvió una respuesta inesperada: "
            f"{type(depositos).__name__}, se esperaba lista"
        )
    objetivo = {n.strip().upper() for n in nombres}
    resueltos: dict[str, int] = {}
    for d in depositos:
        nombre_norm = str(d.get("Nombre") or "").strip().upper()
        if nombre_norm in objetivo:
            resueltos[nombre_norm] = d.get("Id")

    faltantes = objetivo - set(resueltos)
    if faltantes:
        print(
            f"ADVERTENCIA load_stock_depositos: no se encontraron los "
            f"depósitos {sorted(faltantes)} en el ERP "
            f"(disponibles: {sorted(str(d.get('Nombre') or '').strip() for d in depositos)})",
            file=sys.stderr,
        )
    if not resueltos:
        raise ApiError(
            f"load_stock_depositos: ninguno de los depósitos {sorted(objetivo)} "
            f"existe en el ERP"
        )

    stock_por_concepto: dict[int, float] = {}
    for dep_id in resueltos.values():
        session, items = api_paginate(
            session, f"/api/inventarios/getStockByDeposito?id={dep_id}"
        )
        for it in items:
            cid = it.get("Id")
            if cid is None:
                continue
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                continue
            stock_por_concepto[cid_int] = (
                stock_por_concepto.get(cid_int, 0.0)
                + _safe_float(it.get("StockConReservas"))
            )
    return session, stock_por_concepto


def load_clientes_api(
    session: ApiSession,
    vendedores_map: dict[int, str] | None = None,
    clientes_items: list[dict] | None = None,
) -> tuple[ApiSession, pd.DataFrame]:
    """Carga el maestro de clientes desde la API.

    Produce un DataFrame con el mismo schema que `data_loader.load_clientes`:
        columnas: [documento, razon_social, vendedor]

    Mapping de campos API → schema interno:
        NroDoc              → documento        (string, se preserva formato)
        RazonSocial         → razon_social
        IdUsuarioAdicional  → vendedor         (via vendedores_map, fallback "ID_<n>")

    Parámetros:
        clientes_items: si se pasa una lista pre-pulleada (ej. cuando
            el caller ya usó `_fetch_all_clientes` para obtener el
            mapping IdCliente→NroDoc), se evita un segundo round-trip.

    Devuelve (sesión_actualizada, DataFrame). La sesión se retorna porque
    puede haberse refrescado durante la paginación.
    """
    if clientes_items is None:
        session, items = _fetch_all_clientes(session)
    else:
        items = clientes_items
    vmap = vendedores_map or {}
    rows = []
    for c in items:
        vid = c.get("IdUsuarioAdicional")
        if vid is None or vid == 0:
            vendedor = ""
        else:
            vendedor = vmap.get(int(vid), f"ID_{vid}")
        rows.append(
            {
                "documento": str(c.get("NroDoc") or "").strip(),
                "razon_social": str(c.get("RazonSocial") or "").strip(),
                "vendedor": vendedor,
            }
        )
    df = pd.DataFrame(rows, columns=["documento", "razon_social", "vendedor"])
    return session, df


def _stock_de(
    concepto: dict, stock_por_concepto: dict[int, float] | None
) -> float:
    """Stock de un concepto, según la fuente de stock activa.

    Si `stock_por_concepto` está presente (mapa concepto_id → stock
    disponible sumado de los depósitos de inventario), devuelve ese
    valor — 0.0 si el concepto no tiene stock en esos depósitos. Si es
    None, cae al campo `Stock` consolidado del propio concepto (legacy).
    """
    if stock_por_concepto is not None:
        cid = concepto.get("Id")
        try:
            return stock_por_concepto.get(int(cid), 0.0)
        except (TypeError, ValueError):
            return 0.0
    return _safe_float(concepto.get("Stock"))


def load_productos_api(
    session: ApiSession,
    subrubros_map: dict[int, str] | None = None,
    rubros_map: dict[int, str] | None = None,
    conceptos_items: list[dict] | None = None,
    stock_por_concepto: dict[int, float] | None = None,
) -> tuple[ApiSession, pd.DataFrame]:
    """Carga el maestro de productos desde la API.

    Filtra conceptos con `Tipo == "Producto"`. Los combos se obtienen
    con `load_combos_api`, los servicios (si existieran) se ignoran.

    Produce un DataFrame con las columnas:
        sku, nombre, sub_rubro, rubro, stock, stock_minimo, precio

    Las cuatro últimas vienen del listado de conceptos de Contabilium
    (discovery 2026-04-18) y se usan para la tab Inventario y el
    cálculo de stock valorizado.

    Mapping de campos:
        Codigo      → sku
        Nombre      → nombre
        IdSubrubro  → sub_rubro   (via subrubros_map, fallback "ID_<n>")
        IdRubro     → rubro       (via rubros_map, fallback "ID_<n>")
        Stock       → stock       (float, unidades en stock)
        StockMinimo → stock_minimo (float, umbral "mínimo" del ERP)
        PrecioFinal → precio      (float, neto sin IVA — bruto / 1.22)

    Stock por depósito (decisión 2026-06-30):
        Si se pasa `stock_por_concepto` (mapa concepto_id → stock
        disponible sumado de VENTAS+MFLEX, ver `load_stock_depositos`),
        el `stock` sale de ahí en lugar del campo `Stock` consolidado.
        Un producto sin stock en esos depósitos queda en 0.0. Si es None
        (modo legacy / self-test sin credenciales), se usa el `Stock`
        consolidado de todos los depósitos como antes.

    Optimización:
        Si ya se pullearon los conceptos (ej. llamando load_combos_api
        primero), pasarlos en `conceptos_items` para evitar una segunda
        llamada a /api/conceptos/search.
    """
    if conceptos_items is None:
        session, conceptos_items = _fetch_all_conceptos(session)
    smap = subrubros_map or {}
    rmap = rubros_map or {}
    rows = []
    for c in conceptos_items:
        if c.get("Tipo") != "Producto":
            continue
        id_sub_raw = c.get("IdSubrubro")
        id_rub_raw = c.get("IdRubro")
        try:
            id_sub = int(id_sub_raw) if id_sub_raw not in (None, "") else 0
        except (TypeError, ValueError):
            id_sub = 0
        try:
            id_rub = int(id_rub_raw) if id_rub_raw not in (None, "") else 0
        except (TypeError, ValueError):
            id_rub = 0
        rows.append(
            {
                "sku": str(c.get("Codigo") or "").strip(),
                "nombre": str(c.get("Nombre") or "").strip(),
                "sub_rubro": smap.get(id_sub, f"ID_{id_sub}"),
                "rubro": rmap.get(id_rub, f"ID_{id_rub}"),
                "stock": _stock_de(c, stock_por_concepto),
                "stock_minimo": _safe_float(c.get("StockMinimo")),
                "precio": _precio_neto(c.get("PrecioFinal")),
            }
        )
    df = pd.DataFrame(
        rows,
        columns=[
            "sku", "nombre", "sub_rubro", "rubro",
            "stock", "stock_minimo", "precio",
        ],
    )
    return session, df


def load_combos_api(
    session: ApiSession,
    conceptos_items: list[dict] | None = None,
    stock_por_concepto: dict[int, float] | None = None,
) -> tuple[ApiSession, pd.DataFrame]:
    """Carga el maestro de combos desde la API, incluyendo stock derivado.

    Filtra conceptos con `Tipo == "Combo"`. Para calcular el stock del
    combo, hace un N+1 sobre cada combo para obtener su composición
    (`Items: [{Id, Codigo, Cantidad}]`) y aplica la fórmula:

        stock_combo = floor(min(stock_componente / cantidad_requerida))

    Si un componente no está en `conceptos_items` (caso raro), se lo
    ignora para el cálculo — no debería pasar porque todos los
    componentes son conceptos del mismo catálogo. Si la lista de
    componentes está vacía, el stock queda en 0.

    Produce un DataFrame con columnas:
        sku, nombre, stock, precio

    `stock` es un entero (floor) — no tiene sentido decir "0.5 combos
    armables". Discovery 2026-04-18 confirmó que combos tienen su
    propio campo `Stock` en Contabilium, pero decidimos calcular
    derivado desde componentes para ser conservadores (ver decisión).

    `precio` viene de `PrecioFinal` del listado de conceptos, neto
    sin IVA (bruto / 1.22). Se usa para valorizar el stock del combo
    en la tab Inventario.

    N+1: 9 combos en GSU → ~1 s adicional al sync. No se paraleliza
    (no amerita).

    Optimización: ver `load_productos_api`.
    """
    if conceptos_items is None:
        session, conceptos_items = _fetch_all_conceptos(session)

    # Mapa concepto_id → stock disponible (para resolver componentes).
    # Si se pasó `stock_por_concepto` (stock disponible de VENTAS+MFLEX), el
    # stock derivado del combo se calcula con el stock de sus componentes
    # en esos depósitos. Si es None, cae al `Stock` consolidado (legacy).
    if stock_por_concepto is not None:
        stock_by_id: dict[int, float] = dict(stock_por_concepto)
    else:
        stock_by_id = {}
        for c in conceptos_items:
            cid = c.get("Id")
            if cid is None:
                continue
            try:
                stock_by_id[int(cid)] = _safe_float(c.get("Stock"))
            except (TypeError, ValueError):
                continue

    rows = []
    for c in conceptos_items:
        if c.get("Tipo") != "Combo":
            continue
        combo_id = c.get("Id")
        if combo_id is None:
            continue

        precio_combo = _precio_neto(c.get("PrecioFinal"))

        # Detalle del combo para obtener sus Items.
        try:
            session, detail = api_get(session, f"/api/conceptos/?id={combo_id}")
        except ApiError:
            # Si falla el detalle, caemos de vuelta al stock del propio
            # combo (filtrado por depósito si hay mapa, consolidado si no).
            rows.append(
                {
                    "sku": str(c.get("Codigo") or "").strip(),
                    "nombre": str(c.get("Nombre") or "").strip(),
                    "stock": int(_stock_de(c, stock_por_concepto)),
                    "precio": precio_combo,
                }
            )
            continue

        items_comp = detail.get("Items") or []
        if not items_comp:
            stock_derivado = 0
        else:
            ratios = []
            for it in items_comp:
                comp_id = it.get("Id")
                cantidad_req = _safe_float(it.get("Cantidad"))
                if comp_id is None or cantidad_req <= 0:
                    continue
                stock_comp = stock_by_id.get(int(comp_id), 0.0)
                ratios.append(stock_comp / cantidad_req)
            # floor(min(ratios)) — si no hay ratios válidos, 0.
            stock_derivado = int(min(ratios)) if ratios else 0

        rows.append(
            {
                "sku": str(c.get("Codigo") or "").strip(),
                "nombre": str(c.get("Nombre") or "").strip(),
                "stock": stock_derivado,
                "precio": precio_combo,
            }
        )
    df = pd.DataFrame(rows, columns=["sku", "nombre", "stock", "precio"])
    return session, df


# =====================================================================
# Helpers compartidos: mappings desde clientes + moneda
# =====================================================================

# Mapping de IDMoneda de Contabilium UY al código ISO que usamos
# internamente. 794 es UYU según IATA/ISO. Si aparecen otras monedas
# (ej. USD=2, EUR=47) caen en fallback "MONEDA_<id>" para que
# `validate_moneda` las flaguee como warning y las excluya (ver
# transforms.py).
MONEDA_MAP: dict[int, str] = {
    794: "UYU",
}

# Tipos de comprobante que representan notas de crédito (restan del
# total de venta). En el header del comprobante, `ImporteTotalBruto`
# viene con signo negativo; pero en los `Items` del detalle,
# `Cantidad` y `PrecioUnitario` vienen SIEMPRE positivos. Validado
# empíricamente con 5 NCF reales de marzo 2026 (ratio calc/header =
# -1.0000 en todos). Por eso `load_fc_api` aplica el signo manualmente
# cuando el `TipoFc` cae en este set.
#
# Tipos UY (según tabla de referencias de Contabilium):
#   NCF = Nota de crédito eFactura
#   NCT = Nota de crédito eTicket
#   NCE = Nota de crédito eFactura exportación
#
# Las notas de DÉBITO (NDF, NDT, NDE) NO entran acá — esas suman como
# facturas, con signo positivo.
TIPOS_NEGATIVOS: frozenset[str] = frozenset({"NCF", "NCT", "NCE"})


def _fetch_all_clientes(session: ApiSession) -> tuple[ApiSession, list[dict]]:
    """Pullea TODOS los clientes paginado.

    Helper compartido por load_clientes_api y load_fc_api (este último
    lo necesita para construir el mapping IdCliente → NroDoc).
    """
    return api_paginate(session, "/api/clientes/search")


def _build_cliente_documento_map(items: list[dict]) -> dict[int, str]:
    """Construye mapping IdCliente (int) → NroDoc (string preservando formato).

    Se usa en load_fc_api para enriquecer comprobantes con el documento
    del cliente, ya que la API del comprobante solo expone IdCliente.
    """
    out: dict[int, str] = {}
    for c in items:
        cid = c.get("Id")
        doc = str(c.get("NroDoc") or "").strip()
        if cid is not None:
            out[int(cid)] = doc
    return out


# =====================================================================
# Loader de facturación (Tanda C) — el más pesado
# =====================================================================


def load_fc_api(
    session: ApiSession,
    fecha_desde: str,
    fecha_hasta: str,
    *,
    vendedores_map: dict[int, str] | None = None,
    clientes_items: list[dict] | None = None,
    max_workers: int = 10,
) -> tuple[ApiSession, pd.DataFrame, list[tuple[int, str]]]:
    """Carga la facturación de un rango de fechas desde la API.

    Es el equivalente dinámico de `data_loader.load_fc` (que lee
    `fc_semanal.xlsx` o `fc_mensual.xlsx`).

    Algoritmo:
        1. Pull paginado de /api/comprobantes/search — listado "header"
           con Id, IdCliente, RazonSocial, FechaEmision, TipoFc, IDMoneda
           e ImporteTotalBruto, pero SIN items.
        2. Por cada comprobante, GET /api/comprobantes/?id={Id} en
           paralelo (ThreadPool de `max_workers` workers) para obtener
           el detalle con `Items` y `IDVendedor` (que no viene en el
           listado).
        3. Explotar los items en filas: un comprobante con N items se
           convierte en N filas del DataFrame final.
        4. Aplicar mapeos (IdCliente → NroDoc, IDVendedor → email,
           IDMoneda → código).

    Parámetros:
        session: ApiSession activa.
        fecha_desde, fecha_hasta: strings en formato "YYYY-MM-DD".
        vendedores_map: dict {IDVendedor (int): email/nombre}. Si None
            o vacío, los vendedores aparecen como "ID_<n>".
        clientes_items: lista pre-pulleada de clientes. Si None, la
            función pullea el maestro por dentro. Pasarla optimiza
            cuando el caller ya la tiene (ej. Tanda E cachea).
        max_workers: concurrencia del N+1. Default 10 — empíricamente
            reduce un sync de 1000 comprobantes de ~3 min (serial) a
            ~30-60 seg.

    Retorna tripleta:
        (sesión_actualizada,
         DataFrame con columnas:
             documento, razon_social, vendedor, fecha, tipo, moneda,
             sku, producto, unidades, monto,
         lista de errores: [(IdComprobante, mensaje_error), ...])

    Schema del DataFrame idéntico a `data_loader.load_fc`.

    Manejo de errores:
        Si un GetById individual falla tras los retries de `api_get`,
        el comprobante se omite del DataFrame y el par `(Id, mensaje)`
        queda en la lista de errores retornada. Esto prioriza
        "dashboard con 99% de los datos" sobre "sin dashboard". La
        lista vacía `[]` significa "sync 100% exitoso". El caller
        (`app.py`) eleva estos errores al panel de salud para
        visibilidad del usuario.
    """
    # --- Pre-refrescar token antes del batch paralelo ---
    # Minimiza el riesgo de que varios threads intenten regenerarlo
    # simultáneamente si venciera a mitad del sync.
    session = _refrescar_si_expirado(session)

    # --- Mapping IdCliente → NroDoc (pullea maestro si no vino) ---
    if clientes_items is None:
        session, clientes_items = _fetch_all_clientes(session)
    doc_by_id_cli = _build_cliente_documento_map(clientes_items)

    # --- Pull paginado del search ---
    path = (
        f"/api/comprobantes/search?"
        f"fechaDesde={fecha_desde}&fechaHasta={fecha_hasta}"
    )
    session, headers = api_paginate(session, path)

    if not headers:
        # Rango sin comprobantes. Devolvemos DF vacío con el schema correcto.
        return session, _empty_fc_df(), []

    # --- N+1 de GetById en paralelo ---
    details: dict[int, dict] = {}
    errors: list[tuple[int, str]] = []

    session_snapshot = session  # sesión pre-refrescada, usada por todos los threads

    def _fetch_detail(cid: int) -> tuple[int, dict]:
        _, payload = api_get(session_snapshot, f"/api/comprobantes/?id={cid}")
        if not isinstance(payload, dict):
            raise ApiError(f"GetById de comprobante {cid} no devolvió un dict")
        return cid, payload

    ids = [h["Id"] for h in headers if h.get("Id") is not None]
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {exe.submit(_fetch_detail, cid): cid for cid in ids}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                _, detail = future.result()
                details[cid] = detail
            except Exception as e:  # noqa: BLE001 — queremos ver todos los tipos
                errors.append((cid, str(e)))

    if errors:
        # Reporte simple por stdout. En Tanda E se eleva al panel de salud.
        print(
            f"[api_loader] WARNING: {len(errors)} comprobante(s) no se "
            f"pudieron traer en detalle y se omiten:"
        )
        for cid, err in errors[:5]:
            print(f"  - {cid}: {err}")
        if len(errors) > 5:
            print(f"  ... y {len(errors) - 5} más")

    # --- Explotar items en filas ---
    vmap = vendedores_map or {}
    rows: list[dict] = []
    for h in headers:
        cid = h.get("Id")
        if cid is None:
            continue
        detail = details.get(cid)
        if detail is None:
            continue  # fetch falló, ya logueado arriba

        items = detail.get("Items") or []
        # `RazonSocial` viene en el header pero null en el detalle.
        razon_social = str(h.get("RazonSocial") or "").strip()
        id_cliente = h.get("IdCliente")
        documento = doc_by_id_cli.get(int(id_cliente), "") if id_cliente else ""
        fecha = parse_fecha_iso(h.get("FechaEmision"))
        tipo = (h.get("TipoFc") or "").strip()
        moneda_id = h.get("IDMoneda")
        moneda = MONEDA_MAP.get(
            int(moneda_id) if moneda_id is not None else 0,
            f"MONEDA_{moneda_id}",
        )
        # `IDVendedor` viene en el detalle, null en el header.
        vid = detail.get("IDVendedor")
        if vid is None or vid == 0:
            vendedor = ""
        else:
            vendedor = vmap.get(int(vid), f"ID_{vid}")

        # Signo del comprobante: las NCF tienen Items con Cantidad positiva
        # y el -1 lo tenemos que aplicar nosotros. Ver constante
        # TIPOS_NEGATIVOS arriba para la justificación.
        signo = -1.0 if tipo in TIPOS_NEGATIVOS else 1.0

        # Campos de cobranzas (discovery 2026-04-18). El detalle del
        # comprobante trae:
        #   Saldo: string UY con monto pendiente bruto (con IVA).
        #          0 → cobrado. >0 → adeudado. Replicado en todas las
        #          filas del comprobante para simplificar métricas.
        #   FechaVencimiento: ISO string. Puede ser null.
        #   CondicionVenta: ej "30 Cuenta Corriente", "Contado".
        #   Pagos: array de pagos parciales. Puede ser null si el
        #          comprobante se cobró de una vez (o sigue adeudado).
        saldo = parse_monto_uy(detail.get("Saldo"))
        fecha_venc = parse_fecha_iso(detail.get("FechaVencimiento"))
        cond_venta = str(detail.get("CondicionVenta") or "").strip()
        pagos_raw = detail.get("Pagos") or []
        pagos_count = len(pagos_raw) if isinstance(pagos_raw, list) else 0

        if not items:
            # Comprobante sin items (ej. NCF de descuento comercial).
            # Mantenemos la fila con sku/producto vacíos para que
            # `filter_notas_credito` en transforms.py lo descarte.
            rows.append(
                {
                    "id_comprobante": str(cid),
                    "documento": documento,
                    "razon_social": razon_social,
                    "vendedor": vendedor,
                    "fecha": fecha,
                    "tipo": tipo,
                    "moneda": moneda,
                    "sku": "",
                    "producto": "",
                    "unidades": 0.0,
                    "monto": 0.0,
                    "saldo": saldo,
                    "fecha_vencimiento": fecha_venc,
                    "condicion_venta": cond_venta,
                    "pagos_count": pagos_count,
                }
            )
            continue

        for it in items:
            cantidad = _safe_float(it.get("Cantidad"))
            precio = _safe_float(it.get("PrecioUnitario"))
            bonif = _safe_float(it.get("Bonificacion"))
            # Fórmula de "Subo Total Bonif": monto neto del ítem SIN IVA,
            # con descuento de bonificación aplicado. El signo viene del
            # tipo de comprobante, no de los items.
            monto = signo * cantidad * precio * (1.0 - bonif / 100.0)
            unidades = signo * cantidad
            rows.append(
                {
                    "id_comprobante": str(cid),
                    "documento": documento,
                    "razon_social": razon_social,
                    "vendedor": vendedor,
                    "fecha": fecha,
                    "tipo": tipo,
                    "moneda": moneda,
                    "sku": str(it.get("Codigo") or "").strip(),
                    "producto": str(it.get("Concepto") or "").strip(),
                    "unidades": unidades,
                    "monto": monto,
                    "saldo": saldo,
                    "fecha_vencimiento": fecha_venc,
                    "condicion_venta": cond_venta,
                    "pagos_count": pagos_count,
                }
            )

    df = pd.DataFrame(
        rows,
        columns=[
            "id_comprobante",
            "documento",
            "razon_social",
            "vendedor",
            "fecha",
            "tipo",
            "moneda",
            "sku",
            "producto",
            "unidades",
            "monto",
            "saldo",
            "fecha_vencimiento",
            "condicion_venta",
            "pagos_count",
        ],
    )
    return session, df, errors


def _empty_fc_df() -> pd.DataFrame:
    """DataFrame vacío con el schema canónico de facturación."""
    return pd.DataFrame(
        columns=[
            "id_comprobante",
            "documento",
            "razon_social",
            "vendedor",
            "fecha",
            "tipo",
            "moneda",
            "sku",
            "producto",
            "unidades",
            "monto",
            "saldo",
            "fecha_vencimiento",
            "condicion_venta",
            "pagos_count",
        ]
    )


def _safe_float(v) -> float:
    """Convierte a float tolerante: None/None/basura → 0.0."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _precio_neto(v) -> float:
    """Devuelve `PrecioFinal` neto sin IVA básico UY (22%).

    Tolerante: el campo puede venir como número directo o como string
    locale UY ("1.260,00"). Devuelve 0.0 si no se puede parsear.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) / IVA_BASICO_UY
    bruto = parse_monto_uy(v)
    return bruto / IVA_BASICO_UY


# =====================================================================
# Self-test — correr con `python api_loader.py`
# =====================================================================

def _self_test() -> None:
    """Valida parseos sin red y (si hay secrets locales) auth + paginación.

    Imprime un checklist a stdout. No falla silenciosamente: si algo no
    cuadra, levanta AssertionError o ApiError con contexto.
    """
    # --- Parseos (offline, siempre corren) ---
    assert parse_monto_uy("1.260,00") == 1260.0, "parse básico"
    assert parse_monto_uy("2.500,50") == 2500.5, "decimales"
    assert parse_monto_uy("0,00") == 0.0, "cero"
    assert parse_monto_uy("-826,45") == -826.45, "negativo (NCF)"
    assert parse_monto_uy("35.000,00") == 35000.0, "miles"
    assert parse_monto_uy("1.234.567,89") == 1234567.89, "varios separadores de miles"
    assert parse_monto_uy(None) == 0.0, "None"
    assert parse_monto_uy("") == 0.0, "vacío"
    assert parse_monto_uy(1260.0) == 1260.0, "float passthrough"
    assert parse_monto_uy(1260) == 1260.0, "int passthrough"
    assert parse_monto_uy("basura") == 0.0, "no parseable no rompe"
    print("OK  parse_monto_uy")

    d1 = parse_fecha_iso("2026-03-02T00:00:00")
    assert d1 is not None and d1.year == 2026 and d1.month == 3 and d1.day == 2
    d2 = parse_fecha_iso("2026-03-02T08:35:51.193")
    assert d2 is not None and d2.hour == 8 and d2.minute == 35 and d2.second == 51
    assert parse_fecha_iso(None) is None
    assert parse_fecha_iso("") is None
    assert parse_fecha_iso("basura") is None
    print("OK  parse_fecha_iso")

    # --- Auth + paginación (online, requiere secrets locales) ---
    try:
        import tomllib
    except ImportError:
        print("SKIP tomllib requiere Python 3.11+. Saltando tests online.")
        return

    from pathlib import Path
    secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        print(f"SKIP no existe {secrets_path}. Saltando tests online.")
        return
    with open(secrets_path, "rb") as f:
        secrets = tomllib.load(f)
    client_id = secrets.get("contabilium_client_id")
    client_secret = secrets.get("contabilium_client_secret")
    if not client_id or not client_secret:
        print("SKIP faltan contabilium_client_id / _secret en secrets.toml.")
        return

    session = obtener_token(client_id, client_secret)
    remaining = int(session.expires_at - time.time())
    print(f"OK  obtener_token (token vence en {remaining}s)")
    assert not session.is_expired()
    assert len(session.access_token) > 100, "token sospechosamente corto"

    session, payload = api_get(session, "/api/clientes/search?page=1")
    assert isinstance(payload, dict)
    assert "Items" in payload
    total = payload.get("TotalItems", 0)
    print(f"OK  api_get /api/clientes/search?page=1 (TotalItems={total})")

    # Paginación completa sobre un rango pequeño (2 días de marzo).
    session, items = api_paginate(
        session,
        "/api/comprobantes/search?fechaDesde=2026-03-01&fechaHasta=2026-03-02",
    )
    print(f"OK  api_paginate 2 días de marzo ({len(items)} comprobantes)")

    # --- Tanda B: loaders de maestros ---
    session, df_cli = load_clientes_api(session)
    assert list(df_cli.columns) == ["documento", "razon_social", "vendedor"], (
        f"clientes columns: {list(df_cli.columns)}"
    )
    assert len(df_cli) > 900, f"clientes: esperaba >900, obtuve {len(df_cli)}"
    # Spot check: documento preservado como string, razón social no vacía.
    first = df_cli.iloc[0]
    assert isinstance(first["documento"], str) and len(first["documento"]) > 0
    assert isinstance(first["razon_social"], str) and len(first["razon_social"]) > 0
    print(f"OK  load_clientes_api ({len(df_cli)} filas, schema correcto)")

    # Optimización: pulleamos conceptos una vez y se los pasamos a productos + combos.
    session, conceptos = _fetch_all_conceptos(session)
    print(f"    ({len(conceptos)} conceptos totales, se reutilizan abajo)")

    session, df_prod = load_productos_api(session, conceptos_items=conceptos)
    assert list(df_prod.columns) == [
        "sku", "nombre", "sub_rubro", "rubro",
        "stock", "stock_minimo", "precio",
    ], (
        f"productos columns: {list(df_prod.columns)}"
    )
    assert len(df_prod) > 400, f"productos: esperaba >400, obtuve {len(df_prod)}"
    # Todos los sub_rubro y rubro deberían tener fallback "ID_<n>" (sin mapping aún).
    assert df_prod["sub_rubro"].str.startswith("ID_").all(), (
        "sin mapping, todos los sub_rubro deberían ser 'ID_<n>'"
    )
    print(f"OK  load_productos_api ({len(df_prod)} filas, schema correcto)")

    session, df_combos = load_combos_api(session, conceptos_items=conceptos)
    assert list(df_combos.columns) == ["sku", "nombre", "stock", "precio"], (
        f"combos columns: {list(df_combos.columns)}"
    )
    # En UY sabemos que hay ~9 combos en marzo 2026. Toleramos rango amplio.
    assert 0 <= len(df_combos) < 100, f"combos: cantidad sospechosa {len(df_combos)}"
    print(f"OK  load_combos_api ({len(df_combos)} filas, schema correcto)")

    # Validación cruzada: productos + combos + otros tipos ≈ total conceptos.
    # Si hay conceptos con Tipo != Producto/Combo (ej. Servicio), informamos.
    tipos_extras = {c.get("Tipo") for c in conceptos} - {"Producto", "Combo"}
    if tipos_extras:
        print(f"    (ojo: conceptos con Tipo inesperado: {tipos_extras})")

    # --- Stock por depósito (VENTAS + MFLEX) ---
    session, stock_dep = load_stock_depositos(session)
    assert isinstance(stock_dep, dict) and len(stock_dep) > 0, (
        "load_stock_depositos devolvió un mapa vacío"
    )
    assert all(isinstance(k, int) for k in stock_dep), "keys deben ser concepto_id int"
    suma_dep = sum(stock_dep.values())
    print(
        f"OK  load_stock_depositos ({len(stock_dep)} conceptos con stock en "
        f"{DEPOSITOS_INVENTARIO}, {suma_dep:,.0f} unidades sumadas)"
    )
    # El stock filtrado por depósito debe ser ≤ al consolidado de todos
    # los depósitos (VENTAS+MFLEX es un subconjunto). Lo verificamos a
    # nivel total contra el `Stock` consolidado del listado de conceptos.
    suma_consolidada = sum(
        _safe_float(c.get("Stock"))
        for c in conceptos
        if c.get("Tipo") == "Producto" and _safe_float(c.get("Stock")) > 0
    )
    assert suma_dep <= suma_consolidada + 1e-6, (
        f"stock VENTAS+MFLEX ({suma_dep}) > consolidado ({suma_consolidada}); "
        "imposible si son subconjunto"
    )
    print(
        f"    (sanity: {suma_dep:,.0f} en 2 depósitos ≤ {suma_consolidada:,.0f} "
        f"consolidado, {100*suma_dep/suma_consolidada:.0f}% del total)"
    )

    # Los loaders con stock filtrado producen el mismo schema y un stock
    # total que coincide con el mapa de depósitos (productos).
    session, df_prod_dep = load_productos_api(
        session, conceptos_items=conceptos, stock_por_concepto=stock_dep,
    )
    assert list(df_prod_dep.columns) == list(df_prod.columns), "schema cambió"
    print(
        f"OK  load_productos_api con stock por depósito "
        f"(stock total {df_prod_dep['stock'].sum():,.0f} vs "
        f"{df_prod['stock'].sum():,.0f} consolidado)"
    )

    # Smoke extra: vendedor como ID_<n> cuando no hay mapping.
    vendedores_unicos = set(df_cli["vendedor"].unique())
    con_vendedor = [v for v in vendedores_unicos if v and v != ""]
    print(f"    ({len(con_vendedor)} vendedores únicos en clientes, formato ID_<n>)")

    # --- Tanda C: load_fc_api contra marzo 2026 completo ---
    # Reutilizamos los clientes ya pulleados para evitar otro round-trip.
    # Pre-pulleamos con _fetch_all_clientes y se lo pasamos a load_fc_api.
    session, clientes_items = _fetch_all_clientes(session)
    t0 = time.time()
    session, df_fc, fc_errors = load_fc_api(
        session,
        fecha_desde="2026-03-01",
        fecha_hasta="2026-03-31",
        clientes_items=clientes_items,
    )
    elapsed = time.time() - t0
    if fc_errors:
        print(f"    ({len(fc_errors)} comprobantes con error en GetById)")

    expected_cols = [
        "documento", "razon_social", "vendedor", "fecha", "tipo",
        "moneda", "sku", "producto", "unidades", "monto",
    ]
    assert list(df_fc.columns) == expected_cols, (
        f"fc columns: {list(df_fc.columns)}"
    )
    # Sabemos que marzo 2026 tiene 908 comprobantes. Al explotar items, el
    # DF final tiene >= 908 filas (muchos comprobantes tienen múltiples items).
    assert len(df_fc) >= 900, f"fc: filas sospechosamente bajas ({len(df_fc)})"
    # Distribución de tipos debe incluir al menos FAC y NCF.
    tipos_presentes = set(df_fc["tipo"].unique())
    assert "FAC" in tipos_presentes, f"no hay FAC en marzo 2026: {tipos_presentes}"
    assert "NCF" in tipos_presentes, f"no hay NCF en marzo 2026: {tipos_presentes}"
    # Moneda: todo UYU.
    monedas_presentes = set(df_fc["moneda"].unique())
    assert monedas_presentes <= {"UYU", ""}, f"monedas raras: {monedas_presentes}"

    print(
        f"OK  load_fc_api marzo 2026 ({len(df_fc)} filas de items, "
        f"{elapsed:.1f}s)"
    )
    print(f"    tipos: {sorted(tipos_presentes)}")

    # Resumen rápido: monto total por tipo (útil para comparar contra xlsx).
    resumen = df_fc.groupby("tipo", as_index=False)["monto"].sum()
    print("    totales por tipo:")
    for _, r in resumen.iterrows():
        print(f"      {r['tipo']}: {r['monto']:>16,.2f}")

    print("\n=== Self-test OK: Tandas A + B + C validadas ===")


if __name__ == "__main__":
    _self_test()
