"""
televentas_cliente.py — Alta de clientes nuevos en Contabilium desde el CRM.

Envuelve `POST /api/clientes` (CreateCliente). Un cliente nuevo cargado
por la Vendedora Televentas impacta directamente en el ERP y después
aparece en la lista de leads (tras resincronizar).

⚠️ ESCRIBE EN PRODUCCIÓN. `crear_cliente()` es la única función que
escribe; `armar_body_cliente()` es pura (arma el payload, para preview).
Reutiliza los helpers write-safe de `facturador` (retry solo en 401).
"""

from __future__ import annotations

import api_loader
import facturador

# Defaults UY. La mayoría de los clientes GSU son ferreterías (jurídicas).
PAIS_DEFAULT = "Uruguay"
CONDICION_IVA_DEFAULT = "EMPUY"   # empresa UY (visto en el maestro real)


class ClienteError(Exception):
    """Falla al crear un cliente en Contabilium."""


def armar_body_cliente(
    *,
    razon_social: str,
    nombre_fantasia: str = "",
    tipo_doc: str = "RUT",
    nro_doc: str = "",
    telefono: str = "",
    email: str = "",
    departamento: str = "",
    ciudad: str = "",
    domicilio: str = "",
    cp: str = "",
    observaciones: str = "",
    id_vendedor: int | None = None,
    condicion_iva: str = CONDICION_IVA_DEFAULT,
) -> dict:
    """Construye el body de POST /api/clientes. Puro (no red).

    `departamento` va al campo `Provincia` (en UY provincia = departamento).
    `id_vendedor` (IdUsuarioAdicional) asigna el cliente a un vendedor —
    importante para comisiones y para que el lead herede su vendedor.
    `Personeria`: "J" si es RUT (jurídica), "F" si es CI (persona física).
    """
    razon = (razon_social or "").strip()
    if not razon:
        raise ValueError("La razón social es obligatoria.")
    personeria = "J" if tipo_doc.upper() == "RUT" else "F"
    body = {
        "Id": 0,
        "RazonSocial": razon,
        "NombreFantasia": (nombre_fantasia or "").strip(),
        "CondicionIva": condicion_iva,
        "TipoDoc": tipo_doc,
        "NroDoc": str(nro_doc or "").strip(),
        "Pais": PAIS_DEFAULT,
        "Provincia": (departamento or "").strip(),
        "Ciudad": (ciudad or "").strip(),
        "Domicilio": (domicilio or "").strip(),
        "Telefono": str(telefono or "").strip(),
        "Email": (email or "").strip(),
        "Codigo": None,   # Contabilium asigna el código '0XXXX-C'
        "PisoDepto": "",
        "Cp": str(cp or "").strip(),
        "Observaciones": (observaciones or "").strip(),
        "Personeria": personeria,
    }
    if id_vendedor:
        body["IdUsuarioAdicional"] = int(id_vendedor)
    return body


def crear_cliente(
    session: api_loader.ApiSession, body: dict
) -> tuple[api_loader.ApiSession, dict]:
    """POST /api/clientes. ⚠️ CREA el cliente en producción.

    Devuelve (session, respuesta). Levanta ClienteError si el HTTP no es
    2xx. El caller debería resincronizar el maestro después para ver el
    nuevo lead.
    """
    session, r = facturador._post(session, "/api/clientes", body)
    if r.status_code not in (200, 201):
        raise ClienteError(
            f"crear_cliente devolvió HTTP {r.status_code}: {r.text[:300]}"
        )
    return session, (r.json() if r.text else {})
