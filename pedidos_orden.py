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
    fecha: dt.date | None = None,
) -> ResultadoArmado:
    """Construye el body de POST /api/ordenesVenta para un pedido.

    NO hace red. Devuelve ResultadoArmado: si `ok` es False, `problemas`
    explica por qué no se puede cargar (y NO se debe llamar crear_orden).

    `descuentos`: {fila_excel: porcentaje} que el operador ingresó por
    ítem (ej. {53: 32.0} → bonificación 32% en el ítem de la fila 53).
    """
    descuentos = descuentos or {}
    fecha = fecha or dt.date.today()
    problemas: list[str] = []

    cod_cli = codigo_cliente(pedido.nro_cliente)
    cli = mapa_clientes.get(cod_cli) if cod_cli else None
    if cli is None:
        problemas.append(
            f"Cliente no identificado (Nro. {pedido.nro_cliente} → "
            f"{cod_cli}). No se carga."
        )
    if cli and not cli.get("id_vendedor"):
        problemas.append(
            "El cliente no tiene vendedor asignado en Contabilium "
            "(IDVendedor). Revisar antes de cargar (afecta comisiones)."
        )

    items_body = []
    tiene_combo = False
    for it in pedido.items:
        c = mapa_conceptos.get(_norm_sku(it.codigo))
        if c is None:
            problemas.append(f"SKU sin match en Contabilium: {it.codigo!r}")
            continue
        if c.get("tipo") == "Combo":
            tiene_combo = True
        pct = float(descuentos.get(it.fila, 0.0) or 0.0)
        if pct < 0 or pct >= 100:
            problemas.append(
                f"Descuento inválido en {it.codigo!r}: {pct} "
                "(debe ser 0–99)."
            )
        items_body.append(
            {
                "idConcepto": str(c["id"]),
                "cantidad": float(it.cantidad),
                "precioUnitario": round(float(it.precio_sin_iva), 2),
                "bonificacion": round(pct, 2),
            }
        )

    if not items_body:
        problemas.append("El pedido no tiene ítems cargables.")

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
