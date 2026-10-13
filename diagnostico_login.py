"""
diagnostico_login.py — ¿el login de Contabilium falla por el PAÍS desde donde sale el pedido?

Contexto (2026-10-13): la app de Cobranzas no puede iniciar sesión en
Contabilium. Devuelve `HTTP 400 {"code": "CONNECTION_EXCEPTION"}` con dos
cuentas distintas, así que no son las credenciales. En paralelo, Mariano no
podía ni entrar al web de Contabilium desde Argentina (lo frenaba Cloudflare)
y SÍ pudo con una VPN puesta en Uruguay.

RESUELTO 2026-10-13: no era la geografía. Contabilium **separó el login por
país** y el host genérico `internalapi.contabilium.com` pasó a ser el de
Argentina; para `country=uy` devuelve `CONNECTION_EXCEPTION`. El de Uruguay es
`internalapi.contabilium.com.uy`. Este script queda como verificación: dice
desde dónde sale el pedido, si se llega a Contabilium y si el login da sesión.

ESTE SCRIPT LA PRUEBA, y se corre DOS veces desde la misma máquina:
    1. con la VPN de Uruguay APAGADA
    2. con la VPN de Uruguay PRENDIDA
Si falla sin VPN y anda con VPN, la hipótesis queda confirmada y el problema
no está en el código: está en desde dónde sale el pedido.

Se corre con doble clic en `Diagnostico Contabilium.bat`, o a mano:
    py diagnostico_login.py

Usa SOLO la biblioteca estándar de Python: no hay que instalar nada.
La contraseña se pide escondida (no queda en el historial de la consola) y no
se guarda en ningún lado. Lo único que se imprime es la RESPUESTA del servidor.
"""

from __future__ import annotations

import getpass
import json
import ssl
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 40
_CTX = ssl.create_default_context()


def _pedir(url: str, datos: dict | None = None) -> tuple[int, str]:
    """Devuelve (status, cuerpo). Un 403 de Cloudflare llega como HTTPError:
    nos interesa igual, así que se atrapa y se devuelve como respuesta."""
    cuerpo = None
    headers = {"User-Agent": UA}
    if datos is not None:
        cuerpo = json.dumps(datos).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=cuerpo, headers=headers,
                                 method="POST" if datos is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # red, DNS, timeout
        return 0, f"error de red: {type(e).__name__}: {e}"


def _resumen(txt: str) -> str:
    t = (txt or "").strip()
    if not t:
        return "(sin cuerpo)"
    if t.lstrip().startswith("<"):
        return "HTML — es la pagina de bloqueo de Cloudflare (o un error del server)"
    return t[:300]


def main() -> None:
    print()
    print("=" * 72)
    print(" Diagnostico: el login de Contabilium y el pais desde donde se pide")
    print("=" * 72)

    # 1) Desde donde sale el pedido.
    _, ip = _pedir("https://api.ipify.org")
    ip = ip.strip()
    pais = "?"
    if ip and not ip.startswith("error"):
        _, pais = _pedir(f"https://ipapi.co/{ip}/country_name/")
        pais = pais.strip() or "?"
    print(f"\nEste pedido sale desde la IP {ip} ({pais}).")
    print("Si NO dice Uruguay y la VPN esta prendida, revisa la VPN.")

    # 2) ¿Se llega a los hosts de Contabilium? Esto es lo que frena Cloudflare.
    print("\n--- Se llega a Contabilium? ---")
    for url in ("https://app.contabilium.com.uy/",
                "https://rest.contabilium.com.uy/"):
        status, txt = _pedir(url)
        if status == 403:
            marca = "BLOQUEADO por Cloudflare"
        elif status == 0:
            marca = txt
        else:
            marca = "llega bien"
        print(f"  {url}  ->  HTTP {status}  {marca}")
    print("\n  (`rest.` es la API que usan TODAS las apps de GSU:")
    print("   si esa esta bloqueada, no es solo cobranzas.)")

    # 3) El login — lo mismo que hace la app. Opcional.
    print("\n--- El login ---")
    print("Enter sin escribir nada = saltear esta parte.")
    email = input("Usuario (email) de Contabilium: ").strip()
    if not email:
        print("  (salteado)")
        print("\nListo. Manda esta salida.")
        return
    password = getpass.getpass("Contrasena (no se ve ni se guarda): ")

    url = "https://internalapi.contabilium.com.uy/api/login"
    status, txt = _pedir(url, {"email": email, "password": password,
                               "country": "uy"})
    try:
        tiene_jwt = bool((json.loads(txt) or {}).get("jwt"))
    except Exception:
        tiene_jwt = False

    print(f"\n  POST {url}")
    print(f"  HTTP {status}")
    print(f"  respuesta: {_resumen(txt)}")
    print(f"\n  >>> {'SESION OK (devolvio jwt)' if tiene_jwt else 'NO devolvio sesion'}")

    print("\nCorre esto una vez con la VPN APAGADA y otra con la VPN de Uruguay")
    print("PRENDIDA, y manda las dos salidas.")


if __name__ == "__main__":
    main()
