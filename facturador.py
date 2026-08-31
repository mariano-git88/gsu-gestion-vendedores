"""
facturador.py — Módulo de facturación masiva contra la API REST de Contabilium UY.

Workflow de 3 endpoints, validado end-to-end en sesión 13 (2026-05-05):
    1. POST /api/comprobantes/crear            → {Id: int} (borrador)
    2. GET  /api/comprobantes/emitirFE?id=     → {CAE, Numero, FiscalUrl, ...}
    3. GET  /api/comprobantes/obtenerPdf/?id=  → bytes PDF binario

Caveats operativos críticos:
  - **`crear` ya genera movimientos de stock + asientos contables** aunque
    sea borrador. Si el flujo se interrumpe entre `crear` y `emitirFE`, el
    borrador queda colgado con `Numero: "FAC A-00000000"`. Se elimina con
    DELETE para revertir movimientos.
  - **No se permiten peticiones concurrentes en facturación electrónica**
    (lo dice el PDF oficial). El run masivo debe ser estrictamente secuencial.
  - **Throttling UY: 15 req / 10s**. Excederlo → bloqueo por IP × 1 minuto.
  - **`CondicionVenta` espera el NOMBRE del combo como string** (ej.
    "30 Cuenta Corriente"), NO el ID numérico. El JSON exportado de la
    colección Postman pública miente; el PDF oficial lo confirma. Sin este
    fix la API tira HTTP 500 NullReferenceException.
  - **`IdConcepto` debe ser un ID válido del catálogo** — `null` o `""`
    causa NRE. Las órdenes con línea libre no son facturables vía API.
  - **La orden de venta NO queda vinculada al comprobante** post-emisión.
    El comprobante guarda `RefExterna` (= idOrden) pero el `IDComprobante`
    de la orden permanece en 0. Ver `cargar_facturas_via_api()` para el
    patrón anti-doble-facturación basado en RefExterna.

Excepción explícita en `claude.md.txt`: este módulo es el ÚNICO lugar
del proyecto autorizado a llamar endpoints de escritura de la API.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import requests

import api_loader
from vendedores import VENDEDORES

# Lookup invertido: {email_uppercase: id_vendedor}. Se usa para resolver
# el `IDVendedor` que va al body de POST /api/comprobantes/crear, dado
# que la orden trae `Vendedor` como email.
_VENDEDOR_EMAIL_A_ID: dict[str, int] = {
    email.upper(): vid for vid, email in VENDEDORES.items()
}

# =====================================================================
# Constantes
# =====================================================================

# Throttling UY: 15 req / 10s = 1 req cada ~0.67s. Margen: 0.7s.
THROTTLE_DELAY = 0.7

# Discriminador de borrador. Comprobantes en estado borrador tienen
# `Numero` con todos ceros después del prefijo de letra.
SUFIJO_BORRADOR = "-00000000"

# Default UYU. Los items de las órdenes traen IDMoneda en la respuesta;
# si por alguna razón no viene, asumimos UYU.
ID_MONEDA_UYU = 794

# Días de vencimiento que se setean en el comprobante. Si dejamos
# FechaVencimiento=None, Contabilium aplica un default de 10 días que
# Suprabond NO usa — el negocio opera a 30 días contra cliente B2B.
# Cambiar acá si se ajusta la política.
DIAS_VENCIMIENTO_DEFAULT = 30

# Tasas de IVA que la API puede recibir sin que nadie las haya revisado.
# En Uruguay son 22 (básico) y 10 (mínimo).
#
# **El 0 NO está acá a propósito.** Un item con IVA 0 puede ser un producto
# exento de verdad o un producto cargado sin IVA en el catálogo — desde acá
# son indistinguibles, y Valeria Falero reportó (2026-08-31) que "productos
# sin el IVA" es uno de los errores que caza a ojo antes de facturar. Emitir
# con la tasa adivinada es peor que frenar: la factura sale mal y se
# corrige con nota de crédito a mano, que no se puede automatizar.
#
# Si algún día GSU vende algo genuinamente exento, agregar 0.0 acá y
# documentar cuál es el producto.
IVA_TASAS_VALIDAS = frozenset({10.0, 22.0})

USER_AGENT = "GSU-Facturador/1.0"
DEFAULT_TIMEOUT = 60


# =====================================================================
# Excepciones
# =====================================================================

class FacturadorError(api_loader.ApiError):
    """Error en operación de facturación masiva."""


class BorradorYaEmitidoError(FacturadorError):
    """Se intentó eliminar un comprobante que ya tiene CAE."""


class OrdenNoFacturableError(FacturadorError):
    """La orden no es facturable (anulada, ya facturada, sin items, etc.)."""


class IvaNoConfiableError(OrdenNoFacturableError):
    """Un item de la orden tiene un IVA que no se puede usar sin revisar.

    Se levanta cuando el IVA no viene, viene vacío, o viene con una tasa
    que no es 22 ni 10. Antes esto se tapaba con un default de 22.
    """


# =====================================================================
# Throttling
# =====================================================================

_last_request_at: float = 0.0


def _throttle() -> None:
    """Bloquea hasta que pasen al menos THROTTLE_DELAY segundos desde el
    último request. Llamado por todos los helpers HTTP de este módulo.
    Implementación naive: process-local, mono-thread (la API no admite
    concurrencia en facturación electrónica de todos modos)."""
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < THROTTLE_DELAY:
        time.sleep(THROTTLE_DELAY - elapsed)
    _last_request_at = time.time()


# =====================================================================
# HTTP helpers (refresh de token + throttling)
# =====================================================================

def _headers(session: api_loader.ApiSession, content_json: bool = True) -> dict:
    h = {
        "Authorization": f"Bearer {session.access_token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if content_json:
        h["Content-Type"] = "application/json"
    return h


def _refresh_si_expirado(session: api_loader.ApiSession) -> api_loader.ApiSession:
    if session.is_expired():
        return api_loader.obtener_token(session.client_id, session.client_secret)
    return session


def _get(session: api_loader.ApiSession, path: str
         ) -> tuple[api_loader.ApiSession, requests.Response]:
    session = _refresh_si_expirado(session)
    _throttle()
    r = requests.get(
        f"{api_loader.BASE_URL}{path}",
        headers=_headers(session),
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code == 401:
        session = api_loader.obtener_token(session.client_id, session.client_secret)
        _throttle()
        r = requests.get(
            f"{api_loader.BASE_URL}{path}",
            headers=_headers(session),
            timeout=DEFAULT_TIMEOUT,
        )
    return session, r


def _post(session: api_loader.ApiSession, path: str, body: dict
          ) -> tuple[api_loader.ApiSession, requests.Response]:
    session = _refresh_si_expirado(session)
    _throttle()
    r = requests.post(
        f"{api_loader.BASE_URL}{path}",
        headers=_headers(session),
        json=body,
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code == 401:
        session = api_loader.obtener_token(session.client_id, session.client_secret)
        _throttle()
        r = requests.post(
            f"{api_loader.BASE_URL}{path}",
            headers=_headers(session),
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
    return session, r


def _delete(session: api_loader.ApiSession, path: str
            ) -> tuple[api_loader.ApiSession, requests.Response]:
    session = _refresh_si_expirado(session)
    _throttle()
    r = requests.delete(
        f"{api_loader.BASE_URL}{path}",
        headers=_headers(session),
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code == 401:
        session = api_loader.obtener_token(session.client_id, session.client_secret)
        _throttle()
        r = requests.delete(
            f"{api_loader.BASE_URL}{path}",
            headers=_headers(session),
            timeout=DEFAULT_TIMEOUT,
        )
    return session, r


# =====================================================================
# Lectura — combos
# =====================================================================

def cargar_condiciones_venta(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, list[dict]]:
    """GET /api/usuarios/condicionesVenta. Devuelve list[{ID, Nombre, Activa, ...}]."""
    session, r = _get(session, "/api/usuarios/condicionesVenta")
    if r.status_code != 200:
        raise FacturadorError(f"condicionesVenta: HTTP {r.status_code} | {r.text[:200]}")
    data = r.json()
    return session, data if isinstance(data, list) else []


def cargar_puntos_venta(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, list[dict]]:
    """GET /api/puntosdeventa/search. Devuelve list[{Id, Nombre, Activo, ...}]."""
    session, r = _get(session, "/api/puntosdeventa/search")
    if r.status_code != 200:
        raise FacturadorError(f"puntosdeventa: HTTP {r.status_code} | {r.text[:200]}")
    data = r.json()
    return session, data if isinstance(data, list) else []


def cargar_inventarios(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, list[dict]]:
    """GET /api/inventarios/getDepositos. Devuelve list[{Id, Nombre, Activo, ...}]."""
    session, r = _get(session, "/api/inventarios/getDepositos")
    if r.status_code != 200:
        raise FacturadorError(f"inventarios: HTTP {r.status_code} | {r.text[:200]}")
    data = r.json()
    return session, data if isinstance(data, list) else []


def resolver_condicion_venta_nombre(
    session: api_loader.ApiSession,
    condicion_venta_id: int,
) -> tuple[api_loader.ApiSession, str]:
    """Mapea ID → Nombre del combo de condiciones de venta. Necesario porque
    el body de POST /comprobantes/crear espera el Nombre como string, no el ID.
    """
    session, items = cargar_condiciones_venta(session)
    for it in items:
        ids = (it.get("ID"), it.get("Id"), it.get("id"))
        if condicion_venta_id in ids:
            nombre = it.get("Nombre") or it.get("nombre")
            if nombre:
                return session, nombre
    raise FacturadorError(
        f"Condición de venta {condicion_venta_id} no encontrada en "
        f"/api/usuarios/condicionesVenta"
    )


# =====================================================================
# Lectura — anti-doble-facturación
# =====================================================================

def cargar_facturas_via_api(
    session: api_loader.ApiSession,
    fecha_desde: str,
    fecha_hasta: str,
) -> tuple[api_loader.ApiSession, dict[str, int]]:
    """Devuelve {RefExterna: IDComprobante} de comprobantes EMITIDOS via API
    en el rango. Excluye borradores (Numero ending en `-00000000`).

    Caveat documentado: el server NO soporta filtro por RefExterna
    (`?refExterna=X` lo ignora silenciosamente). Hay que paginar todo y
    filtrar client-side. ~21 páginas para 1000 items, ~35-42s con throttling.

    Llamar UNA sola vez al inicio del run masivo, comparar contra ids de
    órdenes candidatas en memoria.

    Args:
      fecha_desde, fecha_hasta: "YYYY-MM-DD". Rango cubre TODAS las facturas
        que pudieran tener `RefExterna` apuntando a las órdenes a procesar.
        Conservador: usar mes anterior + mes actual.
    """
    path = f"/api/comprobantes/search?fechaDesde={fecha_desde}&fechaHasta={fecha_hasta}"
    session, items = api_loader.api_paginate(session, path)

    out: dict[str, int] = {}
    for it in items:
        numero = str(it.get("Numero") or "")
        if not numero or numero.endswith(SUFIJO_BORRADOR):
            continue  # excluye borradores
        ref = str(it.get("RefExterna") or "").strip()
        if not ref:
            continue  # comprobantes emitidos via UI no tienen RefExterna
        id_comp = it.get("Id") or it.get("ID") or 0
        out[ref] = int(id_comp)
    return session, out


# =====================================================================
# Lectura — orden de venta
# =====================================================================

def obtener_orden(
    session: api_loader.ApiSession,
    id_orden: int,
) -> tuple[api_loader.ApiSession, dict]:
    """GET /api/ordenesventa/?id= → detalle completo de la orden."""
    session, r = _get(session, f"/api/ordenesventa/?id={id_orden}")
    if r.status_code != 200:
        raise FacturadorError(
            f"ordenesventa/?id={id_orden}: HTTP {r.status_code} | {r.text[:200]}"
        )
    return session, r.json()


# =====================================================================
# Buzón de armados del depósito
# =====================================================================

def derivar_estado_armados(df_eventos):
    """Del log de eventos del buzón, deriva una fila por orden con su
    estado actual.

    El buzón es append-only: la app de Picking apenda un evento `armado`
    (y lo puede reintentar, así que puede haber duplicados), y el
    facturador apenda `facturado` cuando emite. **El estado de una orden
    es su último evento**, ordenando por timestamp.

    Devuelve un DataFrame con las columnas del evento representativo de cada
    orden más `estado` ("armado" | "facturado"), `items` ya deserializado
    (lista de dicts; lista vacía si el JSON viene roto o ausente) y
    `rearmado_post_factura`.

    **`facturado` pisa a `armado`, aunque el `armado` sea posterior.** No
    alcanza con "gana el último evento": la PC del depósito reintenta los
    envíos que le fallaron, así que un armado viejo puede aterrizar en el
    buzón después de que la orden ya se facturó. Con la regla del último
    evento, esa orden volvía a la cola de Valeria y quedaba expuesta a que
    se facturara dos veces. Una factura emitida es un documento fiscal: ante
    la duda, la orden se queda afuera de la cola.

    Cuando eso pasa se marca `rearmado_post_factura` para que la pantalla lo
    pueda avisar, porque también puede ser un armado nuevo de verdad.

    Función pura: no toca red ni Sheets, para poder testearla.
    """
    import json as _json

    import pandas as pd

    cols_salida = ["estado", "items"]
    if df_eventos is None or df_eventos.empty:
        vacio = pd.DataFrame(
            {c: pd.Series(dtype="object") for c in
             list(getattr(df_eventos, "columns", [])) + cols_salida}
        )
        return vacio

    df = df_eventos.copy()
    df["id_orden"] = df["id_orden"].astype(str).str.strip()
    df = df[df["id_orden"] != ""]
    if df.empty:
        df["estado"] = pd.Series(dtype="object")
        df["items"] = pd.Series(dtype="object")
        return df

    # Orden estable: por timestamp, y ante empate por el orden de aparición
    # en la planilla (que es el orden en que se apendaron las filas).
    df["_orden_original"] = range(len(df))
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["_evento"] = df["evento"].astype(str).str.strip().str.lower()
    df["_es_factura"] = df["_evento"] == "facturado"

    # Ordenamos con los `facturado` al final de cada grupo: así el `.last()`
    # se queda con la factura si existe, y con el armado más nuevo si no.
    df = df.sort_values(
        ["_es_factura", "_ts", "_orden_original"], na_position="first"
    )

    ultimo = df.groupby("id_orden", as_index=False).last()
    ultimo["estado"] = ultimo["_evento"]

    # ¿Llegó algún `armado` después de la factura? Puede ser un reintento
    # tardío (inofensivo) o un rearmado real (hay que mirarlo).
    ts_factura = (
        df[df["_es_factura"]].groupby("id_orden")["_ts"].max().rename("_ts_fact")
    )
    ts_armado = (
        df[~df["_es_factura"]].groupby("id_orden")["_ts"].max().rename("_ts_arm")
    )
    marcas = pd.concat([ts_factura, ts_armado], axis=1)
    marcas["rearmado_post_factura"] = (
        marcas["_ts_fact"].notna()
        & marcas["_ts_arm"].notna()
        & (marcas["_ts_arm"] > marcas["_ts_fact"])
    )
    ultimo = ultimo.merge(
        marcas[["rearmado_post_factura"]], left_on="id_orden",
        right_index=True, how="left",
    )
    ultimo["rearmado_post_factura"] = ultimo["rearmado_post_factura"].fillna(False)

    def _parsear_items(raw):
        if not raw:
            return []
        try:
            datos = _json.loads(raw)
            return datos if isinstance(datos, list) else []
        except (ValueError, TypeError):
            # Un JSON cortado (celda truncada) no puede tumbar la pantalla:
            # la orden se muestra igual, sin detalle.
            return []

    ultimo["items"] = ultimo["items_json"].apply(_parsear_items)

    internas = ["_ts", "_orden_original", "_evento", "_es_factura"]
    return ultimo.drop(columns=internas).reset_index(drop=True)


def armados_pendientes_de_facturar(df_eventos):
    """Órdenes que el depósito armó y todavía nadie facturó.

    Es `derivar_estado_armados` filtrado por estado == "armado", ordenado
    por fecha de armado (las más viejas primero, que son las que más
    apuran).
    """
    estado = derivar_estado_armados(df_eventos)
    if estado.empty:
        return estado
    pendientes = estado[estado["estado"] == "armado"]
    if pendientes.empty:
        return pendientes
    return pendientes.sort_values("timestamp").reset_index(drop=True)


# =====================================================================
# Mapeo de orden a body de POST /api/comprobantes/crear
# =====================================================================

OBSERVACIONES_MAX = 500


def _observaciones_con_adenda(observaciones: str, adenda: str | None) -> str:
    """Combina la adenda de administración con las Observaciones de la orden,
    respetando el tope de caracteres.

    **La adenda va primero y nunca se recorta.** Las Observaciones de las
    órdenes de GSU arrancan con el machete de cuentas bancarias (~230
    caracteres), así que si la adenda fuera al final y hubiera que cortar,
    lo que se perdería sería justo el dato que hace falta: la sucursal de
    Sodimac o el número de orden de compra del cliente. Prefiero perder el
    machete, que se repite en todas las facturas.

    OJO: el tope de 500 es una defensa nuestra, no un límite confirmado de
    la API de Contabilium. Habría que medirlo con una factura real.
    """
    adenda = (adenda or "").strip()
    observaciones = (observaciones or "").strip()

    if not adenda:
        return observaciones[:OBSERVACIONES_MAX]
    if not observaciones:
        return adenda[:OBSERVACIONES_MAX]

    separador = " | "
    espacio_restante = OBSERVACIONES_MAX - len(adenda) - len(separador)
    if espacio_restante <= 0:
        return adenda[:OBSERVACIONES_MAX]
    return f"{adenda}{separador}{observaciones[:espacio_restante]}"


def _iva_del_item(item: dict, id_orden: object) -> float:
    """Devuelve la tasa de IVA del item, o levanta si no es confiable.

    Antes acá había `float(it.get("Iva") or 22)`. Ese default hacía dos
    cosas malas en silencio: emitía con 22 un producto que venía sin IVA
    cargado, y convertía un IVA 0 legítimo en 22 (`0 or 22` es 22).
    Ninguna de las dos se veía en ningún lado hasta que llegaba el
    reclamo del cliente.
    """
    crudo = item.get("Iva")
    concepto = str(item.get("Concepto") or item.get("IdConcepto") or "sin nombre")

    if crudo is None or (isinstance(crudo, str) and not crudo.strip()):
        raise IvaNoConfiableError(
            f"Orden {id_orden}: el producto \"{concepto}\" no trae IVA. "
            f"Revisalo en el catálogo de Contabilium: no se puede "
            f"facturar esta orden hasta corregirlo."
        )

    try:
        tasa = float(crudo)
    except (TypeError, ValueError):
        raise IvaNoConfiableError(
            f"Orden {id_orden}: el producto \"{concepto}\" trae un IVA "
            f"ilegible ({crudo!r}). No se puede facturar esta orden."
        ) from None

    if tasa not in IVA_TASAS_VALIDAS:
        esperadas = " o ".join(f"{t:g}" for t in sorted(IVA_TASAS_VALIDAS))
        raise IvaNoConfiableError(
            f"Orden {id_orden}: el producto \"{concepto}\" tiene IVA "
            f"{tasa:g}%, que no es {esperadas}%. Si está bien cargado hay "
            f"que facturarlo desde Contabilium; si está mal, corregí el "
            f"producto. No se puede facturar esta orden desde acá."
        )

    return tasa


def revisar_iva_de_la_orden(orden: dict) -> list[str]:
    """Los problemas de IVA de una orden, sin levantar excepción.

    Sirve para avisar ANTES de que alguien apriete "Emitir": el guard duro
    vive en `_iva_del_item`, pero descubrir el problema recién al apretar
    el botón es una mala pantalla para el depósito.
    """
    id_orden = orden.get("ID") or orden.get("Id") or orden.get("id") or 0
    problemas = []
    for it in orden.get("Items") or []:
        try:
            _iva_del_item(it, id_orden)
        except IvaNoConfiableError as exc:
            problemas.append(str(exc))
    return problemas


def mapear_orden_a_body_crear(
    orden: dict,
    condicion_venta_nombre: str,
    punto_venta_id: int,
    inventario_id: int,
    *,
    tipo_fc: str = "FAC",
    fecha_emision: date | None = None,
    vendedor_email_override: str | None = None,
    adenda: str | None = None,
) -> dict:
    """Construye el body validado para POST /api/comprobantes/crear.

    Schema confirmado contra PDF oficial de Contabilium UY (sesión 13).
    NO incluir `IDCondicionVenta` (integer) — el campo correcto es
    `CondicionVenta` (string nombre).

    Args:
      orden: respuesta de obtener_orden() o de /api/ordenesventa/?id=.
      condicion_venta_nombre: STRING nombre del combo. Resolver con
        resolver_condicion_venta_nombre() si solo se tiene el ID.
      punto_venta_id, inventario_id: IDs de los combos.
      tipo_fc: "FAC" para UY (default). En AR sería "FCE"/"FCB".
      fecha_emision: default = hoy.
      adenda: texto libre que administración agrega al facturar
        (sucursal de Sodimac, número de orden de compra del cliente).
        Se antepone a las Observaciones de la orden — ver
        `_observaciones_con_adenda` para el porqué del orden.

    Levanta OrdenNoFacturableError si la orden tiene problemas estructurales
    (sin items, items con IdConcepto null, etc.).
    """
    id_orden = orden.get("ID") or orden.get("Id") or orden.get("id") or 0

    items_orden = orden.get("Items") or []
    if not items_orden:
        raise OrdenNoFacturableError(f"Orden {id_orden} no tiene items.")

    items_body = []
    id_moneda_item: int | None = None
    for it in items_orden:
        id_concepto = it.get("IdConcepto")
        if id_concepto in (None, "", 0):
            raise OrdenNoFacturableError(
                f"Orden {id_orden} tiene item con IdConcepto inválido "
                f"(línea libre no soportada por API). "
                f"Item: {it.get('Concepto')!r}"
            )
        items_body.append({
            "IdConcepto": id_concepto,
            "Cantidad": float(it.get("Cantidad") or 0),
            "Concepto": it.get("Concepto", ""),
            "PrecioUnitario": float(api_loader.parse_monto_uy(it.get("PrecioUnitario"))),
            "Iva": _iva_del_item(it, id_orden),
            "Bonificacion": float(it.get("Bonificacion") or 0),
        })
        if id_moneda_item is None and it.get("IDMoneda"):
            id_moneda_item = int(it.get("IDMoneda"))

    fecha_emision = fecha_emision or date.today()

    # Resolver IDVendedor desde el email del vendedor.
    # CAVEAT: GET /api/ordenesventa/?id= NO devuelve el campo Vendedor
    # (validado 2026-05-06 con orden 2026154). Solo el listado de search
    # lo trae. Por eso aceptamos `vendedor_email_override` con prioridad
    # sobre lo que devuelva el detalle, para que el caller pueda pasar
    # el email obtenido del listado.
    vendedor_email = (
        vendedor_email_override
        or orden.get("Vendedor")
        or ""
    ).strip().upper()
    id_vendedor = _VENDEDOR_EMAIL_A_ID.get(vendedor_email)

    body = {
        "IdUsuarioAdicional": 0,
        "IdCliente": orden.get("IDPersona") or orden.get("IDCliente"),
        "FechaEmision": fecha_emision.isoformat(),
        "TipoFc": tipo_fc,
        "Modo": "E",
        "PuntoVenta": str(punto_venta_id),
        "Inventario": int(inventario_id),
        "FechaVencimiento": (
            fecha_emision + timedelta(days=DIAS_VENCIMIENTO_DEFAULT)
        ).isoformat(),
        "Items": items_body,
        "Tributos": None,
        "Observaciones": _observaciones_con_adenda(
            orden.get("Observaciones") or "", adenda
        ),
        "fceMiPYME": False,
        "Canal": "",
        "Pagos": None,
        "Descuento": "0",
        "Recargo": "0",
        "IDIntegracion": None,
        "IDVentaIntegracion": None,
        "CondicionVenta": condicion_venta_nombre,
        "IDTurno": None,
        "IDMoneda": id_moneda_item or ID_MONEDA_UYU,
        "TipoDevolucion": None,
        "RefExterna": str(id_orden),  # clave para anti-doble-facturación.
    }
    if id_vendedor is not None:
        body["IDVendedor"] = int(id_vendedor)
    return body


# =====================================================================
# Escritura — flujo de emisión
# =====================================================================

def crear_borrador(
    session: api_loader.ApiSession,
    body: dict,
) -> tuple[api_loader.ApiSession, int]:
    """POST /api/comprobantes/crear. Devuelve IDComprobante del borrador.

    Side effect importante: ya genera movimientos de stock y asientos
    contables. Si el run se interrumpe entre `crear` y `emitir_fe`, hay
    que llamar a `eliminar_borrador()` para revertir.
    """
    session, r = _post(session, "/api/comprobantes/crear", body)
    if r.status_code not in (200, 201):
        raise FacturadorError(
            f"crear: HTTP {r.status_code} | {r.text[:300]}"
        )
    payload = r.json()
    if isinstance(payload, int):
        return session, payload
    if isinstance(payload, dict):
        id_comp = payload.get("Id") or payload.get("id")
        if id_comp:
            return session, int(id_comp)
    raise FacturadorError(f"crear: respuesta inesperada: {payload!r}")


def emitir_fe(
    session: api_loader.ApiSession,
    id_borrador: int,
) -> tuple[api_loader.ApiSession, dict]:
    """GET /api/comprobantes/emitirFE?id=. Solicita CAE/CFE a DGI.

    Devuelve dict con CAE, Numero (FAC A-NNNNNNNN), FechaCAE,
    LinkPublico, FiscalUrl. **Esta operación es FISCAL — la factura
    queda legal y solo se anula con nota de crédito.**
    """
    session, r = _get(session, f"/api/comprobantes/emitirFE?id={id_borrador}")
    if r.status_code != 200:
        raise FacturadorError(
            f"emitirFE id={id_borrador}: HTTP {r.status_code} | {r.text[:300]}"
        )
    return session, r.json()


def obtener_pdf(
    session: api_loader.ApiSession,
    id_comprobante: int,
) -> tuple[api_loader.ApiSession, bytes]:
    """GET /api/comprobantes/obtenerPdf/?id=. Devuelve los bytes del PDF."""
    session, r = _get(session, f"/api/comprobantes/obtenerPdf/?id={id_comprobante}")
    if r.status_code != 200:
        raise FacturadorError(
            f"obtenerPdf id={id_comprobante}: HTTP {r.status_code} | {r.text[:200]}"
        )
    if r.content[:5] != b"%PDF-":
        raise FacturadorError(
            f"obtenerPdf id={id_comprobante}: respuesta no parece PDF binario"
        )
    return session, r.content


def cancelar_orden(
    session: api_loader.ApiSession,
    id_orden: int,
) -> api_loader.ApiSession:
    """POST /api/ordenesventa/Cancel?id=. Cancela una orden de venta.

    Uso clave en el pipeline post-facturación: cuando emitís la factura
    via API, Contabilium descuenta StockActual correctamente PERO no
    libera StockReservado (la reserva colgada de la orden). El "Libres"
    queda doble-descontado. Cancelar la orden libera la reserva sin
    tocar StockActual (que ya bajó solo).

    Como en errors.md (sesión 2026-05-06) está documentado, este endpoint
    es el HANDLER GENÉRICO de /api/ordenesventa/<algo>?id= — los nombres
    son decorativos. El subpath "Cancel" es el canónico de la doc oficial,
    lo usamos por claridad.

    Para que comisiones no pierda esta venta, ver la lógica de RefExterna
    en comisiones_data.py (sesión 2026-05-13).
    """
    session, r = _post(session, f"/api/ordenesventa/Cancel?id={id_orden}", body={})
    if r.status_code != 200:
        raise FacturadorError(
            f"Cancel orden {id_orden}: HTTP {r.status_code} | {r.text[:200]}"
        )
    return session


def eliminar_borrador(
    session: api_loader.ApiSession,
    id_borrador: int,
) -> api_loader.ApiSession:
    """DELETE /api/comprobantes/?id=. Solo válido para borradores no
    emitidos y no cobrados. Revierte movimientos de stock + asientos.

    Levanta BorradorYaEmitidoError si el comprobante tiene CAE asignado
    (chequea con un GET previo).
    """
    # Guard: confirmar que es borrador antes de borrar.
    session, r = _get(session, f"/api/comprobantes/?id={id_borrador}")
    if r.status_code != 200:
        raise FacturadorError(
            f"comprobante {id_borrador} no encontrado para eliminar: HTTP {r.status_code}"
        )
    comp = r.json()
    numero = str(comp.get("Numero") or "")
    cae = str(comp.get("Cae") or "").strip()
    if cae or not numero.endswith(SUFIJO_BORRADOR):
        raise BorradorYaEmitidoError(
            f"Comprobante {id_borrador} ya está emitido (Numero={numero}, "
            f"Cae={cae!r}). No se puede eliminar."
        )

    session, r = _delete(session, f"/api/comprobantes/?id={id_borrador}")
    if r.status_code not in (200, 204):
        raise FacturadorError(
            f"DELETE id={id_borrador}: HTTP {r.status_code} | {r.text[:200]}"
        )
    return session


# =====================================================================
# Pipeline de alto nivel
# =====================================================================

def facturar_orden(
    session: api_loader.ApiSession,
    id_orden: int,
    condicion_venta_nombre: str,
    punto_venta_id: int,
    inventario_id: int,
    *,
    tipo_fc: str = "FAC",
    fecha_emision: date | None = None,
    vendedor_email: str | None = None,
    adenda: str | None = None,
) -> tuple[api_loader.ApiSession, dict]:
    """Pipeline completo: trae orden → mapea body → crea borrador →
    emite CAE. Devuelve dict con id_borrador + datos de emisión.

    Args:
      vendedor_email: email del vendedor que aparece en el listado de
        órdenes (`/search`). El detalle (`/?id=`) NO lo trae, así que
        el caller debe pasarlo desde el listado para que se mapee a
        `IDVendedor` correctamente. Si es None y el detalle tampoco
        lo trae, Contabilium asigna el vendedor del API key (admin).

    Si falla `emitir_fe`, intenta limpiar el borrador automáticamente
    (best effort — si la limpieza también falla, deja el id en el error
    para que el caller lo limpie manualmente).
    """
    session, orden = obtener_orden(session, id_orden)
    if (orden.get("IDComprobante") or 0) > 0:
        raise OrdenNoFacturableError(
            f"Orden {id_orden} ya facturada vía UI web "
            f"(IDComprobante={orden.get('IDComprobante')})."
        )

    body = mapear_orden_a_body_crear(
        orden,
        condicion_venta_nombre=condicion_venta_nombre,
        punto_venta_id=punto_venta_id,
        inventario_id=inventario_id,
        tipo_fc=tipo_fc,
        fecha_emision=fecha_emision,
        vendedor_email_override=vendedor_email,
        adenda=adenda,
    )

    session, id_borrador = crear_borrador(session, body)

    try:
        session, emision = emitir_fe(session, id_borrador)
    except Exception as exc_emitir:
        # Borrador creado pero emisión falló: intentar limpiar.
        try:
            session = eliminar_borrador(session, id_borrador)
            raise FacturadorError(
                f"emitir_fe falló para borrador {id_borrador}, borrador eliminado. "
                f"Error original: {exc_emitir}"
            ) from exc_emitir
        except Exception as exc_cleanup:
            raise FacturadorError(
                f"emitir_fe falló para borrador {id_borrador} Y cleanup también "
                f"falló — borrador queda colgado, eliminar manualmente. "
                f"Original: {exc_emitir}. Cleanup: {exc_cleanup}"
            ) from exc_emitir

    # Cancelar la orden tras emisión exitosa. Esto libera el
    # StockReservado, que el bug del API de Contabilium no toca al
    # facturar (sí descuenta StockActual, pero deja la reserva
    # colgada — Gabi reportó "Libres baja doble"). Ver feedback
    # 2026-05-13 + decisión de pivotear comisiones a RefExterna.
    #
    # Best effort: si Cancel falla, NO romper. La factura ya está
    # emitida con CAE válido. Solo registramos el error en el dict
    # para que el caller pueda mostrarlo y eventualmente cancelar
    # manualmente desde Contabilium.
    orden_cancelada = False
    orden_cancel_error: str | None = None
    try:
        session = cancelar_orden(session, id_orden)
        orden_cancelada = True
    except Exception as exc_cancel:
        orden_cancel_error = str(exc_cancel)

    return session, {
        "id_borrador": id_borrador,
        "id_comprobante": id_borrador,  # mismo id, ya con CAE.
        "id_orden": id_orden,
        "cae": emision.get("CAE"),
        "numero": emision.get("Numero"),
        "fiscal_url": emision.get("FiscalUrl"),
        "link_publico": emision.get("LinkPublico"),
        "fecha_cae": emision.get("FechaCAE"),
        "orden_cancelada": orden_cancelada,
        "orden_cancel_error": orden_cancel_error,
    }
