"""
sembrar_historico_desde_xlsx.py — Utility CLI para sembrar el histórico
del Sheet con datos de un mes ya pagado en el flujo legacy.

Se usa **una sola vez por mes** que ya pagaste con el flujo viejo y
querés que el ajuste retroactivo lo considere como "ya cobrado".

Cómo funciona:
  1. Lee los 3 xlsx legacy del mes (clientes, ventas, cobranzas) desde
     `inputs/AAAA MES/` del proyecto Liquidación de Comisiones.
  2. Calcula el resumen de comisiones con `commissions.compute_commissions`
     (misma lógica que producía el liquidacion_AAAA-MM.xlsx legacy).
  3. Escribe en el Sheet:
     - Tab `historico`: una fila por vendedor con los agregados del mes.
     - Tab `cobranzas_pagadas`: una fila por cobranza individual del mes.

Uso:
    python3 sembrar_historico_desde_xlsx.py --periodo 2026-03

Opcionalmente:
    python3 sembrar_historico_desde_xlsx.py --periodo 2026-03 \\
        --inputs-dir "/path/custom/inputs/2026 MARZO"

Por default busca en:
    /mnt/c/.../Liquidación de Comisiones - Claude + GSU/inputs/AAAA MES/

NO toca la API de Contabilium. Solo lee xlsx y escribe Sheet. Se puede
correr offline.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import commissions  # noqa: E402
import gsheets  # noqa: E402


MESES_AAAA_MES = {
    1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL",
    5: "MAYO", 6: "JUNIO", 7: "JULIO", 8: "AGOSTO",
    9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE", 12: "DICIEMBRE",
}

DEFAULT_BASE_LEGACY = Path(
    "/mnt/c/Users/MarianoPappalardo/OneDrive - suprabond.com.uy/"
    "Claude + GSU/Liquidación de Comisiones - Claude + GSU/inputs"
)


def _parse_periodo(periodo: str) -> tuple[int, int]:
    if not periodo or len(periodo) != 7 or periodo[4] != "-":
        raise ValueError(f"Formato esperado AAAA-MM, recibido: {periodo!r}")
    y, m = int(periodo[:4]), int(periodo[5:7])
    if m < 1 or m > 12:
        raise ValueError(f"Mes fuera de rango: {m}")
    return y, m


def _resolver_inputs_dir(periodo: str, override: str | None) -> Path:
    if override:
        return Path(override)
    y, m = _parse_periodo(periodo)
    folder = f"{y} {MESES_AAAA_MES[m]}"
    return DEFAULT_BASE_LEGACY / folder


def _cargar_secrets_gsheets() -> dict:
    secrets_path = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        raise FileNotFoundError(f"No existe {secrets_path}")
    with open(secrets_path, "rb") as f:
        secrets = tomllib.load(f)
    if "gsheets" not in secrets:
        raise KeyError("Falta la sección [gsheets] en secrets.toml")
    return dict(secrets["gsheets"])


def _construir_filas_cobranzas(cobranzas_dict: dict) -> list[dict]:
    """Convierte el dict legacy de cobranzas a las filas que espera
    `gsheets.write_cobranzas_periodo`.

    NOTA: el xlsx legacy usa `Codigo` interno como identificador de
    cliente. Lo guardamos en `rut_cliente` aunque técnicamente sea
    el ID interno, no el RUT. La próxima corrida del mes desde la app
    va a sobreescribir esto con el RUT real (vía `cobranzas_para_persistir`).
    """
    out = []
    for v, lista in cobranzas_dict.get("detalle", {}).items():
        for c in lista:
            out.append({
                "numero": c.get("numero", ""),
                "vendedor": v,
                "rut_cliente": str(c.get("codigo", "")),
                "razon_social": c.get("razon", ""),
                "fecha_cobranza": c.get("fecha", ""),
                "importe": float(c.get("importe", 0.0)),
            })
    for cod, razon, nro, imp in cobranzas_dict.get("descartadas_sin_vendedor", []):
        out.append({
            "numero": nro,
            "vendedor": "",
            "rut_cliente": str(cod),
            "razon_social": razon,
            "fecha_cobranza": "",
            "importe": float(imp),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--periodo", required=True,
        help="Período a sembrar, formato AAAA-MM (ej. 2026-03)",
    )
    parser.add_argument(
        "--inputs-dir",
        help="Path a la carpeta con los 3 xlsx (override). "
             f"Default: {DEFAULT_BASE_LEGACY}/AAAA MES",
    )
    parser.add_argument(
        "--si", action="store_true",
        help="No pedir confirmación interactiva (asume sí).",
    )
    args = parser.parse_args()

    try:
        y, m = _parse_periodo(args.periodo)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2

    inputs_dir = _resolver_inputs_dir(args.periodo, args.inputs_dir)
    print(f"Periodo: {args.periodo}")
    print(f"Inputs:  {inputs_dir}")

    if not inputs_dir.exists():
        print(f"ERROR: no existe la carpeta {inputs_dir}")
        return 2

    clientes_xlsx = inputs_dir / "clientes.xlsx"
    ventas_xlsx = inputs_dir / "ventas.xlsx"
    cobranzas_xlsx = inputs_dir / "cobranzas.xlsx"
    for p in (clientes_xlsx, ventas_xlsx, cobranzas_xlsx):
        if not p.exists():
            print(f"ERROR: falta {p}")
            return 2

    print("\n--- Cargando xlsx legacy ---")
    mapa_clientes, valid_vendors = commissions.load_clientes(clientes_xlsx)
    print(f"  {len(mapa_clientes)} clientes con código, "
          f"{len(valid_vendors)} vendedores válidos")

    ventas = commissions.load_ventas(ventas_xlsx, valid_vendors)
    print(f"  Ventas: {sum(len(v) for v in ventas['detalle'].values())} filas, "
          f"excluidas={ventas['excluidas']}")

    cobranzas = commissions.load_cobranzas(cobranzas_xlsx, mapa_clientes)
    n_cobranzas = sum(len(v) for v in cobranzas["detalle"].values())
    n_huerfanas = len(cobranzas["huerfanas_a_mario"])
    n_descartadas = len(cobranzas["descartadas_sin_vendedor"])
    print(f"  Cobranzas: {n_cobranzas} asignadas + "
          f"{n_huerfanas} huérfanas a MARIO + "
          f"{n_descartadas} descartadas")

    print("\n--- Calculando resumen ---")
    resumen = commissions.compute_commissions(ventas, cobranzas)
    total_neta = sum(int(r["comision_neta"]) for r in resumen)
    print(f"  {len(resumen)} vendedores con comisión, "
          f"TOTAL neta: ${total_neta:,}")
    for r in sorted(resumen, key=lambda x: x["comision_neta"], reverse=True):
        print(f"    {r['vendedor']:<35} ${int(r['comision_neta']):>12,}")

    # Construir filas para cobranzas_pagadas
    filas_cobranzas = _construir_filas_cobranzas(cobranzas)
    print(f"\n  Total cobranzas a persistir: {len(filas_cobranzas)}")

    # Confirmación
    if not args.si:
        print("\n¿Confirmás escribir esto al Sheet? (yes/no): ", end="")
        ans = input().strip().lower()
        if ans not in ("yes", "y", "sí", "si"):
            print("Cancelado.")
            return 0

    print("\n--- Escribiendo al Sheet ---")
    secrets_g = _cargar_secrets_gsheets()

    print("  Tab 'historico'...")
    stats_h = gsheets.write_historico_periodo(
        secrets_g, args.periodo, resumen, sobreescribir=True,
    )
    print(f"    OK · agregadas={stats_h['filas_agregadas']}, "
          f"eliminadas={stats_h['filas_eliminadas']}, "
          f"períodos en Sheet: {stats_h['periodos_total']}")

    print("  Tab 'cobranzas_pagadas'...")
    stats_c = gsheets.write_cobranzas_periodo(
        secrets_g, args.periodo, filas_cobranzas,
    )
    print(f"    OK · agregadas={stats_c['filas_agregadas']}, "
          f"eliminadas={stats_c['filas_eliminadas']}")

    print(f"\n=== Bootstrap del período {args.periodo} completado ===")
    print(f"Próxima corrida del mes siguiente va a detectar tardías "
          f"correctamente comparando contra este snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
