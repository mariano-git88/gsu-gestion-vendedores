"""
diagnostico_login.py — ¿el login de Contabilium falla por el PAÍS desde donde sale el pedido?

Contexto (2026-10-13): la app de Cobranzas no puede iniciar sesión en
Contabilium. Devuelve `HTTP 400 {"code": "CONNECTION_EXCEPTION"}` con dos
cuentas distintas, así que no son las credenciales. En paralelo, Mariano no
podía ni entrar al web de Contabilium desde Argentina (lo frenaba Cloudflare)
y SÍ pudo con una VPN puesta en Uruguay.

Hipótesis: Contabilium restringió el acceso por geografía, y Streamlit Cloud
(que corre en Estados Unidos) queda afuera.

ESTE SCRIPT LA PRUEBA, y se corre DOS veces desde la misma máquina:

    1. con la VPN de Uruguay APAGADA
    2. con la VPN de Uruguay PRENDIDA

Si falla sin VPN y anda con VPN, la hipótesis queda confirmada y el problema
no está en el código: está en desde dónde sale el pedido.

Uso:   python3 diagnostico_login.py

La contraseña se pide escondida (no queda en el historial de la consola) y no
se guarda en ningún lado. Lo único que imprime es la RESPUESTA del servidor.
"""

from __future__ import annotations

import getpass
import json

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 40


def _resumen(r: requests.Response) -> str:
    txt = (r.text or "").strip()
    if not txt:
        return "(sin cuerpo)"
    if txt.lstrip().startswith("<"):
        return "HTML (pagina de bloqueo de Cloudflare o error)"
    return txt[:300]


def main() -> None:
    print(__doc__.split("Uso:")[0].strip())
    print("=" * 70)

    try:
        ip = requests.get("https://api.ipify.org?format=json",
                          timeout=20).json().get("ip")
    except Exception:
        ip = "?"
    try:
        pais = requests.get(f"https://ipapi.co/{ip}/country_name/",
                            timeout=20).text.strip()
    except Exception:
        pais = "?"
    print(f"\nEste pedido sale desde la IP {ip} ({pais}).")
    print("Si no dice Uruguay y la VPN esta prendida, revisa la VPN.\n")

    email = input("Usuario (email) de Contabilium: ").strip()
    password = getpass.getpass("Contrasena (no se ve ni se guarda): ")

    # 1) ¿Se llega al web de Contabilium? (esto es lo que Cloudflare frena)
    for url in ("https://app.contabilium.com.uy/",
                "https://rest.contabilium.com.uy/"):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                             allow_redirects=False)
            marca = "BLOQUEADO por Cloudflare" if r.status_code == 403 else "llega"
            print(f"\nGET {url}\n  HTTP {r.status_code} -> {marca}")
        except Exception as e:
            print(f"\nGET {url}\n  error de red: {type(e).__name__}: {e}")

    # 2) El login propiamente dicho — lo mismo que hace la app.
    url = "https://internalapi.contabilium.com/api/login"
    try:
        r = requests.post(url,
                          json={"email": email, "password": password,
                                "country": "uy"},
                          headers={"User-Agent": UA,
                                   "Content-Type": "application/json"},
                          timeout=TIMEOUT)
    except Exception as e:
        print(f"\nPOST {url}\n  error de red: {type(e).__name__}: {e}")
        return

    try:
        tiene_jwt = bool((r.json() or {}).get("jwt"))
    except (ValueError, AttributeError):
        tiene_jwt = False

    print(f"\nPOST {url}")
    print(f"  HTTP {r.status_code}")
    print(f"  respuesta: {_resumen(r)}")
    print(f"\n  >>> {'SESION OK (devolvio jwt)' if tiene_jwt else 'NO devolvio sesion'}")
    print("\nCorre esto una vez con la VPN apagada y otra con la VPN de Uruguay")
    print("prendida, y manda las dos salidas.")


if __name__ == "__main__":
    main()
