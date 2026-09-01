"""
cuotas.py — Dividir una factura en cuotas, a mano, una sola vez.

Contabilium emite UNA factura con UN saldo aunque la venta sea a 30/60/90:
no sabe qué parte vence en cada cuota (ver `credito.PLAZO_POR_CONDICION`,
que la resume en el vencimiento promedio). Ernesto lo aceptó como tarea
manual: *"una vez que tenemos ese detalle, ya queda hasta que se cobra"*.

Este módulo es esa división. La carga la hace una persona desde la app, se
guarda en una planilla (`gsheets.append_cuotas`) y a partir de ahí la
deudora muestra cada cuota con su vencimiento real en vez de una sola línea
con el promedio.

────────────────────────────────────────────────────────────────────────────
CÓMO SE SABE QUÉ CUOTA ESTÁ PAGA SIN QUE NADIE LO ANOTE
────────────────────────────────────────────────────────────────────────────
Nadie marca cuotas pagas: sería una segunda tarea manual y se desincronizaría
con el ERP a la primera distracción. El saldo lo sigue mandando Contabilium.

Lo que hacemos es **repartir el saldo del ERP entre las cuotas, de la más
vieja a la más nueva**. Si una factura de $3.000 en tres cuotas de $1.000
tiene saldo $1.500, entonces la cuota 1 está paga, la 2 debe $500 y la 3
debe $1.000.

Es un supuesto, y es el correcto para este caso: en una venta a 30/60/90 el
cliente paga las cuotas en orden. No sirve para un cliente que paga
salteado — pero ese caso tampoco tiene cuotas cargadas.

Módulo puro: no importa streamlit ni toca red.
"""

from __future__ import annotations

import re

import pandas as pd

# Tolerancia para dar una cuota por cubierta. Misma que usa `credito`.
TOL = 1.0

COLS_CUOTA = [
    "id_comprobante", "nro_cuota", "vencimiento", "importe",
]

COLS_ESTADO = [
    "id_comprobante", "nro_cuota", "vencimiento", "importe", "pendiente",
]


def plazos_de_condicion(condicion: str) -> list[int]:
    """"30/60/90 Cuenta Corriente" → [30, 60, 90]. Una sola cuota si no hay.

    Se leen los números del nombre de la condición, que es donde Contabilium
    guarda el plazo (no hay un campo aparte). Una condición sin números
    —"Contado", "Transferencia"— no se divide: devuelve [] y el llamador la
    trata como pago único.
    """
    numeros = re.findall(r"\d+", str(condicion or ""))
    return [int(n) for n in numeros] if len(numeros) > 1 else []


def repartir_importe(total: float, partes: int) -> list[float]:
    """Divide un importe en `partes` que suman EXACTAMENTE el total.

    Redondea cada cuota a 2 decimales y le da la diferencia a la última. Sin
    esto, tres cuotas de $1.000,33 suman $3.000,99 y no $3.001: la factura
    dejaría de cerrar por un centavo, que es justo el tipo de diferencia que
    después nadie encuentra.
    """
    if partes <= 0:
        return []
    total = round(float(total), 2)
    base = round(total / partes, 2)
    cuotas = [base] * (partes - 1)
    cuotas.append(round(total - base * (partes - 1), 2))
    return cuotas


def sugerir(
    id_comprobante: int,
    condicion: str,
    emision,
    total: float,
) -> pd.DataFrame:
    """Propuesta de cuotas para una factura, para que la persona la edite.

    Los vencimientos salen de los números de la condición contados desde la
    emisión; los importes, de partir el total en partes iguales. Es una
    sugerencia: la fecha y el monto reales los confirma quien carga.

    Devuelve vacío si la condición no tiene más de un plazo.
    """
    plazos = plazos_de_condicion(condicion)
    if not plazos:
        return pd.DataFrame(columns=COLS_CUOTA)

    base = pd.Timestamp(emision)
    importes = repartir_importe(total, len(plazos))
    return pd.DataFrame([
        {
            "id_comprobante": id_comprobante,
            "nro_cuota": i,
            "vencimiento": base + pd.Timedelta(days=p),
            "importe": imp,
        }
        for i, (p, imp) in enumerate(zip(plazos, importes), start=1)
    ], columns=COLS_CUOTA)


def validar(cuotas: pd.DataFrame, total: float) -> list[str]:
    """Problemas que impiden guardar. Lista vacía = se puede guardar.

    Se valida antes de escribir y no después: una división que no cierra con
    el total de la factura desbalancea la deudora del cliente entero, y el
    error aparecería lejos de acá.
    """
    problemas = []
    if cuotas is None or cuotas.empty:
        return ["No hay cuotas para guardar."]

    imp = pd.to_numeric(cuotas["importe"], errors="coerce")
    if imp.isna().any():
        problemas.append("Hay importes que no son números.")
    elif (imp <= 0).any():
        problemas.append("Hay cuotas con importe cero o negativo.")
    elif abs(imp.sum() - float(total)) > 0.05:
        problemas.append(
            f"Las cuotas suman {imp.sum():,.2f} y la factura es de "
            f"{float(total):,.2f}. Tienen que coincidir."
        )

    venc = pd.to_datetime(cuotas["vencimiento"], errors="coerce")
    if venc.isna().any():
        problemas.append("Hay vencimientos vacíos o con fecha inválida.")
    elif not venc.is_monotonic_increasing:
        problemas.append("Los vencimientos tienen que ir de menor a mayor.")
    elif venc.duplicated().any():
        problemas.append("Hay dos cuotas con el mismo vencimiento.")

    return problemas


def estado(cuotas: pd.DataFrame, saldo_erp: float) -> pd.DataFrame:
    """Reparte el saldo del ERP entre las cuotas, de la más vieja a la más nueva.

    Agrega la columna `pendiente`: lo que falta cobrar de cada cuota. Las
    cuotas más viejas se dan por cobradas primero (ver el encabezado del
    módulo). La suma de `pendiente` es siempre igual al saldo del ERP, así
    que la deuda del cliente no cambia por dividir una factura.

    Si el saldo es mayor que la suma de las cuotas —puede pasar si alguien
    cargó mal los importes— el excedente se apila en la última cuota en vez
    de perderse.
    """
    if cuotas is None or cuotas.empty:
        return pd.DataFrame(columns=COLS_ESTADO)

    out = cuotas.copy()
    out["vencimiento"] = pd.to_datetime(out["vencimiento"], errors="coerce")
    out["importe"] = pd.to_numeric(out["importe"], errors="coerce").fillna(0.0)
    out = out.sort_values(["vencimiento", "nro_cuota"]).reset_index(drop=True)

    restante = max(float(saldo_erp or 0.0), 0.0)
    total_cuotas = out["importe"].sum()
    # Lo ya cobrado es la diferencia entre lo facturado y lo que queda.
    cobrado = max(total_cuotas - restante, 0.0)

    pendientes = []
    for imp in out["importe"]:
        aplicado = min(cobrado, imp)
        cobrado -= aplicado
        pendientes.append(round(imp - aplicado, 2))
    out["pendiente"] = pendientes

    # El saldo tiene que cerrar exacto con el del ERP: si sobró (importes
    # cargados de menos), va a la última cuota.
    diferencia = round(restante - out["pendiente"].sum(), 2)
    if abs(diferencia) > 0.005 and len(out):
        out.loc[out.index[-1], "pendiente"] = round(
            out.loc[out.index[-1], "pendiente"] + diferencia, 2)

    return out[COLS_ESTADO]


def indexar(df_cuotas: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """{id_comprobante: cuotas} desde la planilla, quedándose con la última carga.

    La planilla es append-only: corregir una división es volver a cargarla
    entera con un timestamp nuevo. Al leer gana el juego más reciente de cada
    factura, y una carga marcada `anulado` borra la división (la factura
    vuelve a mostrarse como una sola línea).
    """
    if df_cuotas is None or df_cuotas.empty:
        return {}

    d = df_cuotas.copy()
    for col in ("timestamp", "id_comprobante", "nro_cuota", "vencimiento",
                "importe"):
        if col not in d.columns:
            return {}

    d["id_comprobante"] = pd.to_numeric(d["id_comprobante"], errors="coerce")
    d = d.dropna(subset=["id_comprobante"])
    d["id_comprobante"] = d["id_comprobante"].astype("int64")
    d["nro_cuota"] = pd.to_numeric(d["nro_cuota"], errors="coerce").fillna(0).astype(int)
    d["vencimiento"] = pd.to_datetime(d["vencimiento"], errors="coerce")
    d["importe"] = pd.to_numeric(d["importe"], errors="coerce").fillna(0.0)
    d["anulado"] = (d.get("anulado", "").astype(str).str.strip().str.upper()
                    == "SI")

    salida: dict[int, pd.DataFrame] = {}
    for cid, g in d.groupby("id_comprobante"):
        ultimo = g["timestamp"].max()
        carga = g[g["timestamp"] == ultimo]
        if carga["anulado"].any():
            continue
        salida[int(cid)] = (carga[COLS_CUOTA]
                            .sort_values("nro_cuota")
                            .reset_index(drop=True))
    return salida
