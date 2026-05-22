"""
pedidos_orden.py — Fase 2: armado y creación de órdenes de venta en
Contabilium a partir de los pedidos leídos.

⚠️ ESCRITURA A PRODUCCIÓN. `crear_orden()` es la ÚNICA función que
escribe (POST /api/ordenesVenta) y **crea una orden que reserva stock al
instante**. El resto del módulo (armado de body, mapeos) es read-only.
El flujo seguro NO llama `crear_orden()` hasta: gates manuales
(APROBADO deuda + APROBADO precio) + pantalla de revisión final +
palabra-gate, y una primera corrida con UN solo pedido de prueba.

Schema confirmado contra el Postman oficial (request "CreateOrden"):
    POST {base}/api/ordenesVenta
    { idCliente, fechaEmision, observaciones, bonificacionGlobal,
      IDInventario, IDVendedor, origen,
      items:[{ idConcepto, cantidad, precioUnitario, bonificacion }] }

Decisiones tomadas (Mariano 2026-05-19):
  - precioUnitario = el precio del Excel TAL CUAL (las planillas de
    interior ya traen el +5% adentro).
  - El descuento que el operador tipea por ítem va a `bonificacion`
    (%), no se pisa el precio. precio 100 + bonif 32 → neto 68.
  - IDVendedor = vendedor asignado al cliente en Contabilium
    (`IdUsuarioAdicional`), que es lo que usa comisiones. (Pendiente
    confirmar si en algún caso debe ser el vendedor del pedido.)
  - IDInventario = 832 (depósito "VENTAS"), resuelto dinámicamente.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import requests

import api_loader
import facturador
import pedidos

ORIGEN = "Pedidos GSU"


def _norm_sku(x: object) -> str:
    return re.sub(r"\s+", " ", str(x or "").strip()).upper()


# =====================================================================
# Mapeos read-only
# =====================================================================

def cargar_mapa_conceptos(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, dict[str, dict]]:
    """{CODIGO_normalizado: {id, nombre, tipo, precio}} desde
    /api/conceptos/search. Validado: 42/42 del sample matchean."""
    session, items = api_loader.api_paginate(session, "/api/conceptos/search")
    mapa: dict[str, dict] = {}
    for c in items:
        cod = _norm_sku(c.get("Codigo"))
        if not cod:
            continue
        mapa[cod] = {
            "id": c.get("Id"),
            "nombre": str(c.get("Nombre") or "").strip(),
            "tipo": str(c.get("Tipo") or "").strip(),  # Producto | Combo
            "precio": c.get("Precio"),
        }
    return session, mapa


def cargar_mapa_clientes_full(
    session: api_loader.ApiSession,
) -> tuple[api_loader.ApiSession, dict[str, dict]]:
    """{Codigo: {id, id_vendedor, rut, razon_social}}. Incluye el
    vendedor asignado (`IdUsuarioAdicional`) que va como IDVendedor."""
    session, items = api_loader.api_paginate(session, "/api/clientes/search")
    mapa: dict[str, dict] = {}
    for c in items:
        cod = str(c.get("Codigo") or "").strip()
        if not cod:
            continue
        mapa[cod] = {
            "id": c.get("Id"),
            "id_vendedor": c.get("IdUsuarioAdicional"),
            "rut": str(c.get("NroDoc") or "").strip(),
            "razon_social": str(c.get("RazonSocial") or "").strip(),
        }
    return session, mapa


def resolver_inventario_ventas(
    session: api_loader.ApiSession, nombre: str = "VENTAS"
) -> tuple[api_loader.ApiSession, int | None]:
    """ID del depósito 'VENTAS' (832 al 2026-05-19, pero se resuelve
    dinámico por las dudas)."""
    session, invs = facturador.cargar_inventarios(session)
    for i in invs:
        if str(i.get("Nombre") or "").strip().upper() == nombre.upper():
            return session, i.get("Id")
    return session, None


def codigo_cliente(nro) -> str | None:
    """Nro. Cliente del Excel → `Codigo` Contabilium '0XXXX-C'."""
    d = re.sub(r"\D", "", str(nro or ""))
    return f"{int(d):05d}-C" if d else None


# =====================================================================
# Armado del body (read-only, sin red)
# =====================================================================

@dataclass
class ResultadoArmado:
    hoja: str
    ok: bool
    body: dict | None = None
    problemas: list[str] = field(default_factory=list)
    tiene_combo: bool = False


def armar_body_orden(
    pedido: pedidos.Pedido,
    mapa_clientes: dict[str, dict],
    mapa_conceptos: dict[str, dict],
    inventario_id: int,
    *,
    descuentos: dict[int, float] | None = None,
    precios: dict[int, float] | None = None,
    cantidades: dict[int, float] | None = None,
    fecha: dt.date | None = None,
    codigo_cliente_override: str | None = None,
) -> ResultadoArmado:
    """Construye el body de POST /api/ordenesVenta para un pedido.

    NO hace red. Devuelve ResultadoArmado: si `ok` es False, `problemas`
    explica por qué no se puede cargar (y NO se debe llamar crear_orden).

    `descuentos`: {fila_excel: %} de bonificación por ítem.
    `precios`: {fila_excel: precio} que pisa el precio del Excel.
    `cantidades`: {fila_excel: cantidad} que pisa la cantidad del Excel
        para ese ítem (ej. el vendedor pidió 50 pero hay stock para 30).
        Si la cantidad final queda en 0 el ítem se EXCLUYE de la orden
        (típicamente porque no hay stock); el resto del pedido se carga
        normal. Si TODOS los ítems quedan en 0, el pedido es no cargable
        por el guard de total $0.
    `codigo_cliente_override`: si el operador asignó el cliente a mano,
        el código Contabilium ('0XXXX-C') elegido. Si es None, se
        resuelve por Nro. Cliente y, como fallback, por el número
        embebido en el nombre (ver pedidos.codigo_cliente_candidatos).
    """
    descuentos = descuentos or {}
    precios = precios or {}
    cantidades = cantidades or {}
    fecha = fecha or dt.date.today()
    problemas: list[str] = []

    if codigo_cliente_override:
        cands = [codigo_cliente_override]
    else:
        cands = pedidos.codigo_cliente_candidatos(
            pedido.nro_cliente, pedido.cliente
        )
    cod_cli = next((c for c in cands if c in mapa_clientes), None)
    cli = mapa_clientes.get(cod_cli) if cod_cli else None
    if cli is None:
        problemas.append(
            f"Cliente no identificado (Nro. {pedido.nro_cliente!r} / "
            f"nombre {pedido.cliente!r}). Asignarlo a mano."
        )
    if cli and not cli.get("id_vendedor"):
        problemas.append(
            "El cliente no tiene vendedor asignado en Contabilium "
            "(IDVendedor). Revisar antes de cargar (afecta comisiones)."
        )

    items_body = []
    tiene_combo = False
    total_neto = 0.0
    for it in pedido.items:
        c = mapa_conceptos.get(_norm_sku(it.codigo))
        if c is None:
            problemas.append(f"SKU sin match en Contabilium: {it.codigo!r}")
            continue
        # Cantidad — el operador puede pisarla (típico: 0 = "sin stock,
        # no enviar este ítem"). cant=0 excluye el ítem; el resto del
        # pedido se carga normal.
        cant_raw = cantidades.get(it.fila, it.cantidad)
        cant_final = float(cant_raw) if cant_raw is not None else float(it.cantidad)
        if cant_final < 0:
            problemas.append(
                f"Cantidad negativa en {it.codigo!r}: {cant_final}"
            )
            continue
        if cant_final == 0:
            continue  # ítem excluido por el operador
        if c.get("tipo") == "Combo":
            tiene_combo = True
        pct = float(descuentos.get(it.fila, 0.0) or 0.0)
        if pct < 0 or pct >= 100:
            problemas.append(
                f"Descuento inválido en {it.codigo!r}: {pct} "
                "(debe ser 0–99)."
            )
        precio = precios.get(it.fila)
        precio = float(precio) if precio is not None else float(it.precio_sin_iva)
        if precio < 0:
            problemas.append(f"Precio negativo en {it.codigo!r}: {precio}")
        precio = round(precio, 2)
        items_body.append(
            {
                "idConcepto": str(c["id"]),
                "cantidad": cant_final,
                "precioUnitario": precio,
                "bonificacion": round(pct, 2),
            }
        )
        total_neto += precio * cant_final * (1 - pct / 100.0)

    if not items_body:
        problemas.append("El pedido no tiene ítems cargables.")
    elif round(total_neto, 2) <= 0:
        problemas.append(
            "El total del pedido es $0,00 — no se carga."
        )

    if problemas:
        return ResultadoArmado(pedido.hoja, False, None, problemas, tiene_combo)

    body = {
        "idCliente": cli["id"],
        "fechaEmision": fecha.isoformat(),
        "observaciones": pedido.cond_pago or "",
        "bonificacionGlobal": 0.0,
        "IDInventario": inventario_id,
        "IDVendedor": cli["id_vendedor"],
        "origen": ORIGEN,
        "items": items_body,
    }
    return ResultadoArmado(pedido.hoja, True, body, [], tiene_combo)


# =====================================================================
# Escritura — ⚠️ ÚNICA función que crea la orden en producción
# =====================================================================

def crear_orden(
    session: api_loader.ApiSession, body: dict
) -> tuple[api_loader.ApiSession, requests.Response]:
    """POST /api/ordenesVenta. ⚠️ CREA la orden y RESERVA stock.

    Devuelve (session, requests.Response) — el caller debe chequear
    `r.status_code` y `r.json()`. Solo debe llamarse tras los gates
    manuales + revisión final + palabra-gate. Respeta el throttling UY
    vía facturador._post.
    """
    return facturador._post(session, "/api/ordenesVenta", body)


# =====================================================================
# Lectura post-creación — obtener el Nº de OV formateado
# =====================================================================

def extraer_id_orden(r: requests.Response) -> str:
    """Saca el ID interno de la orden desde la respuesta del POST.

    Contabilium no documenta el body de POST /api/ordenesVenta (el
    Postman oficial lo trae sin ejemplo de respuesta). En la práctica
    puede venir como dict {"ID": ...}, como número plano, o como
    string numérico entre comillas. Esta función tolera las tres
    formas y devuelve el ID como string, o "" si no se pudo extraer.

    El POST de creación NO trae el `NumeroOrden` formateado; para eso
    está `obtener_numero_orden`, que necesita este ID.
    """
    try:
        j = r.json()
    except Exception:  # noqa: BLE001 — body no-JSON, se cae al texto crudo
        j = None
    if isinstance(j, dict):
        for k in ("ID", "Id", "id", "idOrden", "IdOrden"):
            v = j.get(k)
            if v:
                return str(v)
        return ""
    # bool es subclase de int en Python: descartarlo antes del chequeo numérico.
    if isinstance(j, bool):
        return ""
    if isinstance(j, (int, float)):
        return str(int(j))
    if isinstance(j, str) and j.strip().strip('"').isdigit():
        return j.strip().strip('"')
    texto = (r.text or "").strip().strip('"')
    return texto if texto.isdigit() else ""


def obtener_numero_orden(
    session: api_loader.ApiSession, id_orden: str
) -> tuple[api_loader.ApiSession, str]:
    """GET /api/ordenesVenta/?id={id} → `NumeroOrden` formateado.

    El número visible de la OV ("00010749") no viene en la respuesta
    del POST de creación; hay que pedirlo con este GET. Devuelve ""
    si la orden no trae el campo o la respuesta no es un dict.
    """
    session, payload = api_loader.api_get(
        session, f"/api/ordenesVenta/?id={id_orden}"
    )
    if isinstance(payload, dict):
        return session, str(payload.get("NumeroOrden") or "")
    return session, ""
