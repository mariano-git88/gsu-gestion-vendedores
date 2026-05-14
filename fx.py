"""
fx.py — Cotizaciones online para conversión de monedas en `listas_app.py`.

Fuentes públicas, sin API key:
  - ARS/USD Blue: dolarapi.com (https://dolarapi.com/v1/dolares/blue).
    Devuelve compra/venta del Blue agregado de múltiples casas de cambio.
  - UYU/USD interbancario: open.er-api.com
    (https://open.er-api.com/v6/latest/USD). Servicio comunitario que
    expone JSON con todas las cotizaciones contra USD. Para precisión
    fiscal el referente sería BCU, pero su API es SOAP/XML viejo y
    poco amigable — esto es suficiente para análisis de listas.

Funciones puras: no importan streamlit. El caché TTL se aplica desde
la app vía @st.cache_data sobre wrappers, no acá.

Diseño:
  - Cada función levanta `FxError` con mensaje útil si falla (timeout,
    JSON inesperado, status != 200). El caller decide si tratar como
    warning o como bloqueante.
  - Timeout corto (10s) para no colgar la UI si la fuente está caída.
"""

from __future__ import annotations

import requests


class FxError(Exception):
    """Error al obtener cotizaciones de una fuente externa."""


# User-Agent genérico. Algunas APIs públicas bloquean requests sin UA.
_UA = "Mozilla/5.0 (GSU Listas; +https://suprabond.com.uy)"
_TIMEOUT = 10  # segundos


def obtener_blue_ars_usd() -> dict:
    """Devuelve la cotización del dólar Blue desde dolarapi.com.

    Returns:
        {
          "compra": float,   # ARS que paga el broker por 1 USD
          "venta": float,    # ARS que cobra el broker por 1 USD
          "fecha": str,      # ISO timestamp de la última actualización
          "fuente": str,
        }

    Para llevar un precio en ARS a USD, dividir por `venta` da el USD
    más bajo (escenario "AR más barato en USD"). Dividir por `compra`
    da el USD más alto.
    """
    try:
        r = requests.get(
            "https://dolarapi.com/v1/dolares/blue",
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise FxError(f"No pude conectar a dolarapi.com: {e}") from e

    if r.status_code != 200:
        raise FxError(
            f"dolarapi.com devolvió HTTP {r.status_code}: {r.text[:200]}"
        )

    try:
        d = r.json()
    except ValueError as e:
        raise FxError(f"dolarapi.com devolvió JSON inválido: {e}") from e

    try:
        compra = float(d["compra"])
        venta = float(d["venta"])
    except (KeyError, TypeError, ValueError) as e:
        raise FxError(
            f"Respuesta de dolarapi.com sin compra/venta válidas: {d}"
        ) from e

    return {
        "compra": compra,
        "venta": venta,
        "fecha": str(d.get("fechaActualizacion") or ""),
        "fuente": "dolarapi.com (Blue)",
    }


def obtener_uyu_usd() -> dict:
    """Devuelve la cotización UYU/USD desde open.er-api.com.

    Returns:
        {
          "valor": float,   # UYU por 1 USD
          "fecha": str,     # último update declarado por la fuente
          "fuente": str,
        }

    La fuente agrega tasas de mercado spot — no es exactamente el
    interbancario uruguayo oficial (BCU), pero la diferencia para
    análisis de precios es típicamente < 1%.
    """
    try:
        r = requests.get(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise FxError(f"No pude conectar a open.er-api.com: {e}") from e

    if r.status_code != 200:
        raise FxError(
            f"open.er-api.com devolvió HTTP {r.status_code}: {r.text[:200]}"
        )

    try:
        d = r.json()
    except ValueError as e:
        raise FxError(f"open.er-api.com devolvió JSON inválido: {e}") from e

    if d.get("result") != "success":
        raise FxError(
            f"open.er-api.com result={d.get('result')!r}: "
            f"{d.get('error-type', 'sin detalle')}"
        )

    try:
        uyu = float(d["rates"]["UYU"])
    except (KeyError, TypeError, ValueError) as e:
        raise FxError(
            "Respuesta de open.er-api.com sin tasa UYU válida."
        ) from e

    return {
        "valor": uyu,
        "fecha": str(d.get("time_last_update_utc") or ""),
        "fuente": "open.er-api.com",
    }
