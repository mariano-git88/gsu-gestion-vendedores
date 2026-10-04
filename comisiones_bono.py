"""
comisiones_bono.py — Armado del bono trimestral (esquema v1.2).

La fórmula del bono vive en `commissions.compute_bono_trimestral()`; este
módulo es el puente entre esa función y el histórico del Google Sheet:

  tab `historico` (una fila por vendedor × período)
        │
        ├─ ventas  (BRUTAS, con IVA)  ──► / 1,22 ──► venta neta mensual
        └─ cobranzas                  ──────────────► cobranza mensual
        │
        ▼
  commissions.compute_bono_trimestral()  ──►  Cat A/B por pilar + monto

Por qué un módulo nuevo y no una función más en `commissions.py`: ese
archivo está bajo el invariante "las comisiones no pueden fallar" y cada
cambio obliga a correr el smoke. Acá no hay fórmula, solo armado de datos
y presentación, así que el riesgo queda afuera del archivo crítico.

Dos decisiones que están acá y no en la fórmula:

1. **Venta neta = ventas brutas ÷ 1,22.** El histórico guarda las ventas
   CON IVA (columna `ventas`) y los umbrales del bono son sobre la venta
   neta. No se agregó columna al histórico: se deriva igual que lo hace
   `compute_commissions` mes a mes, así que da el mismo número.

2. **Base = los volúmenes tal como se liquidaron cada mes.** Los ajustes
   retroactivos NO entran al bono: tienen su propia regla (spec §4, se
   pagan con la fórmula vigente al momento del ajuste). Si una cobranza
   tardía de julio se pagó como ajuste en agosto, para el bono sigue
   contando en el volumen de julio tal como quedó liquidado.

El gate duro: si alguno de los 3 meses del trimestre NO está en el
histórico, NO se calcula nada. Un mes faltante leído como 0 le borraría
la Cat A a todo el mundo sin decir por qué.
"""

from __future__ import annotations

import io
import math
from calendar import monthrange

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import commissions


# Regla para los meses con licencia por vacaciones. El spec v1.2 §4 dice
# "el mes de licencia se computa con el promedio de los otros dos", pero
# fue escrito pensando en un mes entero. Las licencias reales son de ~10
# días y caen partidas entre dos meses, así que hay dos lecturas:
#
#   PISO          — VIGENTE (decidida 2026-10-04). El mes vale el MAYOR
#                   entre lo que produjo de verdad y el promedio de los
#                   otros dos. En una frase: si la licencia te bajó el mes
#                   te lo reponemos, y si no te lo bajó no te tocamos nada.
#   MES_COMPLETO  — literal del spec original: si hubo aunque sea un día de
#                   licencia, el mes se reemplaza entero por el promedio de
#                   los otros dos. Es `commissions._sustituir_licencia`.
#   PROPORCIONAL  — el mes se parte en dos: los días trabajados valen lo
#                   que el vendedor produjo, y los días de licencia se
#                   imputan al ritmo de los otros dos meses:
#                   computado = real + (días_licencia / días_del_mes) ×
#                   promedio de los otros dos meses.
#
# Por qué PISO y no las otras dos (caso real de Q3 2026): un vendedor se tomó
# 10 días en julio y JULIO LE FUE MEJOR QUE AGOSTO ENTERO — la licencia no le
# bajó la producción, porque las órdenes ya estaban puestas y las cobranzas
# entran solas. Con MES_COMPLETO se le tiraba su mejor mes a la basura y
# cobraba MENOS que si nadie hubiera mirado la licencia: la regla que existe
# para no perjudicarlo lo perjudicaba. Con PROPORCIONAL cobraba casi el triple,
# porque esa regla asume que esos días produjeron cero y los números decían que
# no. Las dos fallan por lo mismo: aplican el ajuste sin mirar si hizo falta.
#
# El costo honesto del PISO: nunca juega en contra del vendedor, así que paga
# siempre igual o más que la fórmula sola. Ese es exactamente el compromiso que
# el spec ya había asumido, pero sin el filo que corta para el lado equivocado.
REGLA_PISO = "piso"
REGLA_MES_COMPLETO = "mes_completo"
REGLA_PROPORCIONAL = "proporcional"
REGLAS_LICENCIA = (REGLA_PISO, REGLA_MES_COMPLETO, REGLA_PROPORCIONAL)
REGLA_VIGENTE = REGLA_PISO


# =====================================================================
# Trimestres
# =====================================================================

MESES_POR_TRIMESTRE = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}

NOMBRE_MES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}


# El bono nació con la fórmula v1.2, vigente desde la liquidación de JULIO
# 2026. Q3 2026 (jul-ago-sep) es el primer trimestre entero bajo el esquema
# nuevo y el primero que genera bono. Antes de eso el bono no existía: un
# trimestre anterior se puede mirar como simulación, pero NO se paga.
PRIMER_TRIMESTRE_CON_BONO = (2026, 3)


def trimestre_genera_bono(anio: int, q: int) -> bool:
    """False para los trimestres anteriores a la vigencia de la v1.2."""
    return (anio, q) >= PRIMER_TRIMESTRE_CON_BONO


def periodos_del_trimestre(anio: int, q: int) -> list[str]:
    """['AAAA-MM', 'AAAA-MM', 'AAAA-MM'] en orden cronológico."""
    if q not in MESES_POR_TRIMESTRE:
        raise ValueError(f"Trimestre inválido: {q!r}. Esperaba 1, 2, 3 o 4.")
    return [f"{anio}-{m:02d}" for m in MESES_POR_TRIMESTRE[q]]


def trimestre_de_periodo(periodo: str) -> tuple[int, int]:
    """'2026-08' → (2026, 3). Sirve para saber a qué Q pertenece un mes."""
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    return anio, (mes - 1) // 3 + 1


def label_trimestre(anio: int, q: int) -> str:
    meses = MESES_POR_TRIMESTRE[q]
    return f"Q{q} {anio} ({NOMBRE_MES[meses[0]]}–{NOMBRE_MES[meses[-1]]})"


def trimestres_en_historico(df_historico: pd.DataFrame) -> list[tuple[int, int]]:
    """Trimestres que tienen al menos un mes en el histórico, del más
    reciente al más viejo. No garantiza que estén los 3 meses — para eso
    está `periodos_faltantes`."""
    if df_historico is None or df_historico.empty:
        return []
    vistos = set()
    for p in df_historico["periodo"].astype(str):
        if len(p) >= 7 and p[4] == "-":
            vistos.add(trimestre_de_periodo(p))
    return sorted(vistos, reverse=True)


def periodos_faltantes(df_historico: pd.DataFrame, periodos: list[str]) -> list[str]:
    """Los períodos del trimestre que NO tienen ninguna fila en el
    histórico. Si devuelve algo, el bono no se puede calcular."""
    if df_historico is None or df_historico.empty:
        return list(periodos)
    presentes = set(df_historico["periodo"].astype(str))
    return [p for p in periodos if p not in presentes]


# =====================================================================
# Armado de los datos del trimestre
# =====================================================================

def normalizar_licencias(licencias, periodos) -> dict:
    """Acepta las dos formas de declarar licencia y devuelve siempre
    `vendedor -> {indice_mes: dias}`.

    Formas aceptadas por vendedor:
      - set/list de índices  {0,2}      → mes entero (días = días del mes)
      - dict {indice: dias}  {0: 10}    → días concretos
    Los índices fuera de {0,1,2} y los días ≤ 0 se descartan en silencio:
    vienen de un editor de pantalla, no de un cálculo.
    """
    out = {}
    for vend, decl in (licencias or {}).items():
        meses = {}
        if isinstance(decl, dict):
            items = decl.items()
        else:
            items = ((i, None) for i in decl)
        for i, dias in items:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if not 0 <= i <= 2:
                continue
            tope = dias_del_mes(periodos[i])
            d = tope if dias is None else float(dias)
            if d <= 0:
                continue
            meses[i] = min(d, tope)
        if meses:
            out[str(vend).strip().upper()] = meses
    return out


def dias_del_mes(periodo: str) -> int:
    """Días calendario del período 'AAAA-MM'."""
    return monthrange(int(periodo[:4]), int(periodo[5:7]))[1]


def sustituir_proporcional(valores_3m, licencia_dias, periodos):
    """Repone solo la fracción del mes que no se trabajó.

        computado[i] = real[i] + (dias[i] / dias_del_mes[i]) × promedio_otros

    `promedio_otros` es el promedio de los meses SIN licencia, igual que la
    regla del mes completo. La idea: los días trabajados valen lo que el
    vendedor efectivamente produjo, y los días de licencia se imputan al
    ritmo diario de los otros dos meses.

    OJO con el caso límite: si `dias[i] == días_del_mes[i]` el resultado es
    `real[i] + promedio_otros`, que coincide con la regla literal del spec
    SOLO si ese mes no tiene producción propia — que es lo esperable si no
    trabajó ningún día, pero no está garantizado (una orden puede facturarse
    estando él de licencia). Con licencias parciales, que es el caso real,
    la cuestión no se presenta.

    Si TODOS los meses tienen licencia no hay referencia de qué reponer y se
    devuelven los valores crudos (misma salida que
    `commissions._sustituir_licencia`).
    """
    idx_ok = [i for i in range(3) if i not in licencia_dias]
    if not licencia_dias or not idx_ok:
        return list(valores_3m)
    prom = sum(valores_3m[i] for i in idx_ok) / len(idx_ok)
    out = []
    for i in range(3):
        if i in licencia_dias:
            frac = min(1.0, licencia_dias[i] / dias_del_mes(periodos[i]))
            out.append(valores_3m[i] + frac * prom)
        else:
            out.append(valores_3m[i])
    return out


def sustituir_piso(valores_3m, licencia_dias):
    """Regla VIGENTE. El mes con licencia vale el MAYOR entre lo que produjo
    de verdad y el promedio de los otros dos.

    La protección por licencia es un PISO, no un canje: el spec existe para
    que tomarse vacaciones no te cueste plata, no para cambiarte un mes bueno
    por un promedio peor. Los meses sin licencia no se tocan — para ellos
    `_sustituir_licencia` ya devuelve el valor crudo, así que el `max` es
    la identidad.

    Si los 3 meses tienen licencia no hay promedio de referencia y
    `_sustituir_licencia` devuelve los crudos: el `max` también es la
    identidad. No inventa nada.
    """
    sustituido = commissions._sustituir_licencia(valores_3m, set(licencia_dias))
    return [max(real, sust) for real, sust in zip(valores_3m, sustituido)]


def _computar(valores_3m, licencia_dias, periodos, regla):
    """Aplica la regla de licencia elegida y devuelve los 3 valores que van
    a clasificar y a pagar."""
    if not licencia_dias:
        return list(valores_3m)
    if regla == REGLA_PISO:
        return sustituir_piso(valores_3m, licencia_dias)
    if regla == REGLA_PROPORCIONAL:
        return sustituir_proporcional(valores_3m, licencia_dias, periodos)
    return commissions._sustituir_licencia(valores_3m, set(licencia_dias))


def licencias_desde_editor(df_editor: pd.DataFrame) -> dict:
    """Convierte la tabla de licencias del `st.data_editor` en el dict que
    espera `armar_datos_bono`: vendedor -> {indice_mes: días}.

    Se espera la forma `[vendedor, mes1, mes2, mes3]`. El acceso es POSICIONAL
    a propósito: los encabezados de mes ("jul (31 d)") no son identificadores
    válidos y cualquier acceso por nombre se desalinearía en silencio si
    alguien cambia la etiqueta. Un valor vacío, nulo o no numérico es 0, que
    es lo que escribe el editor cuando se borra una celda.
    """
    out = {}
    if df_editor is None or len(df_editor) == 0:
        return out
    if df_editor.shape[1] < 4:
        raise ValueError(
            f"La tabla de licencias debe tener vendedor + 3 meses; "
            f"vinieron {df_editor.shape[1]} columna(s)."
        )
    for i_fila in range(len(df_editor)):
        fila = df_editor.iloc[i_fila]
        dias = {}
        for i_mes in range(3):
            v = fila.iloc[1 + i_mes]
            try:
                d = 0.0 if v is None or pd.isna(v) else float(v)
            except (TypeError, ValueError):
                d = 0.0
            if d > 0:
                dias[i_mes] = d
        if dias:
            out[str(fila.iloc[0]).strip().upper()] = dias
    return out


def armar_datos_bono(
    df_historico: pd.DataFrame,
    periodos: list[str],
    licencias: dict | None = None,
    *,
    regla_licencia: str = REGLA_VIGENTE,
    excluidos: set | None = None,
) -> tuple[dict, dict]:
    """Arma el input de `commissions.compute_bono_trimestral`.

    Args:
        df_historico: DataFrame con las columnas de `gsheets.HISTORICO_COLUMNS`.
        periodos: los 3 'AAAA-MM' del trimestre, en orden.
        licencias: vendedor -> set de índices de mes, o vendedor ->
            {indice_mes: días de licencia}. Ver `normalizar_licencias`.
        regla_licencia: REGLA_PISO (vigente), REGLA_MES_COMPLETO
            (literal del spec original) o REGLA_PROPORCIONAL.
        excluidos: vendedores a dejar afuera. Default: los operarios
            (`commissions.VENDEDORES_EXCLUIDOS_OP`), por si alguna fila
            vieja del histórico los tiene.

    Returns:
        (datos, detalle)
        - `datos`: lo que come `compute_bono_trimestral` — vendedor ->
          {'ventas_netas', 'cobranzas', 'licencia_meses'}. Los volúmenes ya
          vienen COMPUTADOS (con la licencia aplicada según la regla) y
          `licencia_meses` va vacío: así la clasificación y el monto salen
          de los mismos números que muestra la pantalla, con una sola
          implementación de la sustitución por regla.
        - `detalle`: vendedor -> {'ventas_netas', 'cobranzas' (crudos),
          'ventas_computadas', 'cobranzas_computadas', 'meses_sin_fila',
          'licencia_dias'} para la tabla de auditoría.
    """
    if len(periodos) != 3:
        raise ValueError(f"Esperaba 3 períodos, recibí {len(periodos)}.")
    if regla_licencia not in REGLAS_LICENCIA:
        raise ValueError(
            f"Regla de licencia inválida: {regla_licencia!r}. "
            f"Esperaba una de {REGLAS_LICENCIA}."
        )
    excl = commissions.VENDEDORES_EXCLUIDOS_OP if excluidos is None else excluidos
    lic_norm = normalizar_licencias(licencias, periodos)

    if df_historico is None or df_historico.empty:
        return {}, {}

    df = df_historico.copy()
    df["periodo"] = df["periodo"].astype(str)
    df["vendedor"] = df["vendedor"].astype(str).str.strip().str.upper()
    df = df[df["periodo"].isin(periodos)]
    df = df[~df["vendedor"].isin({v.upper() for v in excl})]
    # Las columnas pueden venir como texto si alguien editó el Sheet a mano.
    for c in ("ventas", "cobranzas"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    # Un vendedor podría tener más de una fila por período si el Sheet se
    # editó a mano. Sumar es lo correcto: son dos pedazos del mismo mes.
    agg = df.groupby(["vendedor", "periodo"], as_index=False)[
        ["ventas", "cobranzas"]
    ].sum()

    por_vend: dict[str, dict[str, tuple[float, float]]] = {}
    for row in agg.itertuples(index=False):
        por_vend.setdefault(row.vendedor, {})[row.periodo] = (
            float(row.ventas), float(row.cobranzas)
        )

    datos, detalle = {}, {}
    for vend in sorted(por_vend):
        meses = por_vend[vend]
        ventas_netas, cobranzas, sin_fila = [], [], []
        for p in periodos:
            if p in meses:
                vb, co = meses[p]
            else:
                # Sin fila en el histórico = no produjo ese mes. Se computa
                # 0, pero queda anotado para que se vea en pantalla.
                vb, co = 0.0, 0.0
                sin_fila.append(p)
            ventas_netas.append(vb / commissions.DIVISOR_IVA)
            cobranzas.append(co)

        lic = lic_norm.get(vend, {})
        vn_comp = _computar(ventas_netas, lic, periodos, regla_licencia)
        co_comp = _computar(cobranzas, lic, periodos, regla_licencia)
        datos[vend] = {
            "ventas_netas": vn_comp,
            "cobranzas": co_comp,
            "licencia_meses": set(),  # ya aplicada acá, no la repita la fórmula
        }
        detalle[vend] = {
            "ventas_netas": ventas_netas,
            "cobranzas": cobranzas,
            "ventas_computadas": vn_comp,
            "cobranzas_computadas": co_comp,
            "meses_sin_fila": sin_fila,
            "licencia_dias": lic,
        }
    return datos, detalle


# =====================================================================
# Tablas de salida
# =====================================================================

COL_RESUMEN = [
    "vendedor", "cat_venta", "com_venta_trim", "bono_venta",
    "cat_cobranza", "com_cobranza_trim", "bono_cobranza", "bono_total",
]


def tabla_resumen(bono: dict) -> pd.DataFrame:
    """Una fila por vendedor con las dos categorías y el bono."""
    filas = []
    for vend in sorted(bono):
        b = bono[vend]
        filas.append({
            "vendedor": vend,
            "cat_venta": b["cat_venta"],
            "com_venta_trim": b["com_venta_trim"],
            "bono_venta": math.ceil(b["bono_venta"]),
            "cat_cobranza": b["cat_cobranza"],
            "com_cobranza_trim": b["com_cobranza_trim"],
            "bono_cobranza": math.ceil(b["bono_cobranza"]),
            "bono_total": b["bono_total"],
        })
    return pd.DataFrame(filas, columns=COL_RESUMEN)


COL_DETALLE = [
    "vendedor", "mes", "dias_licencia", "venta_neta", "venta_computada",
    "venta_plena", "cobranza", "cobranza_computada", "cobranza_plena",
]


def tabla_detalle(detalle: dict, periodos: list[str]) -> pd.DataFrame:
    """Mes a mes, con el volumen crudo, el computado (post-licencia) y si
    llegó al umbral pleno. Es la tabla que se mira cuando un vendedor
    pregunta por qué quedó en B y no en A.

    `venta_plena` / `cobranza_plena` se evalúan sobre el volumen COMPUTADO,
    que es lo que usa la clasificación.
    """
    filas = []
    for vend in sorted(detalle):
        d = detalle[vend]
        lic = d["licencia_dias"]
        for i, p in enumerate(periodos):
            vn_c = d["ventas_computadas"][i]
            co_c = d["cobranzas_computadas"][i]
            filas.append({
                "vendedor": vend,
                "mes": p,
                "dias_licencia": lic.get(i, 0),
                "venta_neta": d["ventas_netas"][i],
                "venta_computada": vn_c,
                "venta_plena": "✔" if vn_c >= commissions.UMBRAL_VENTA_PLENO else "✗",
                "cobranza": d["cobranzas"][i],
                "cobranza_computada": co_c,
                "cobranza_plena": "✔" if co_c >= commissions.UMBRAL_COBRANZA_PLENO else "✗",
            })
    return pd.DataFrame(filas, columns=COL_DETALLE)


# =====================================================================
# Texto explicativo (va a la app y al xlsx — una sola fuente)
# =====================================================================

_EXPLICACION_BASE = [
    "El bono se paga una vez por trimestre calendario, junto con la "
    "liquidación del último mes del trimestre.",
    f"Cada pilar se evalúa por separado. Venta: umbral pleno "
    f"${commissions.UMBRAL_VENTA_PLENO:,.0f} de venta NETA (= facturado ÷ "
    f"{commissions.DIVISOR_IVA}). Cobranza: umbral pleno "
    f"${commissions.UMBRAL_COBRANZA_PLENO:,.0f} cobrados.",
    f"Cat A = los 3 meses sobre el umbral pleno → "
    f"{commissions.PCT_BONO['venta']['A']*100:.0f}% de la comisión de venta "
    f"del trimestre y {commissions.PCT_BONO['cobranza']['A']*100:.0f}% de la "
    f"de cobranza.",
    f"Cat B = promedio del trimestre ≥ la mitad del umbral Y al menos 1 mes "
    f"pleno → {commissions.PCT_BONO['venta']['B']*100:.0f}% de venta y "
    f"{commissions.PCT_BONO['cobranza']['B']*100:.0f}% de cobranza.",
    "Sin categoría (—) = no llega a ninguna de las dos: bono 0 en ese pilar.",
    "La comisión del trimestre se recalcula desde el volumen de cada mes "
    "(los tramos no son lineales, así que no se pueden promediar comisiones).",
    "Los ajustes retroactivos NO entran al bono: el bono usa los volúmenes "
    "tal como se liquidó cada mes. El retro ya se pagó en su momento con la "
    "fórmula vigente.",
]

_EXPLICACION_LICENCIA = {
    REGLA_PISO:
        "Licencia por vacaciones (regla VIGENTE: PISO): el mes en el que hubo "
        "licencia vale el mayor entre lo que el vendedor produjo de verdad y "
        "el promedio de los otros dos del trimestre. Si la licencia le bajó el "
        "mes, se lo repone; si no se lo bajó, no se le toca nada. La "
        "protección de la Ley UY es un piso, no un canje: tomarse vacaciones "
        "nunca puede costarle plata, pero tampoco le cambia un mes bueno por "
        "un promedio peor.",
    REGLA_MES_COMPLETO:
        "Licencia por vacaciones (regla NO vigente, MES COMPLETO): el mes en el que hubo "
        "licencia se computa entero con el promedio de los otros dos del "
        "trimestre, tanto para clasificar como para el monto. Es la lectura "
        "literal del spec v1.2 §4 (Ley UY: las vacaciones se pagan como si "
        "hubiera trabajado).",
    REGLA_PROPORCIONAL:
        "Licencia por vacaciones (regla NO vigente, PROPORCIONAL): se repone solo la "
        "parte del mes que no se trabajó — al volumen real del mes se le suma "
        "(días de licencia ÷ días del mes) × el promedio de los otros dos. "
        "Si la licencia ocupa el mes entero, da el mismo resultado que la "
        "regla de mes completo.",
}


def explicacion_bono(regla: str = REGLA_VIGENTE) -> list[str]:
    """Las reglas del bono en texto, con la de licencia que se haya elegido.
    Una sola fuente para la app y para la hoja 'Cómo se calcula' del xlsx."""
    return _EXPLICACION_BASE + [_EXPLICACION_LICENCIA[regla]]


def _styles():
    return {
        "bold": Font(bold=True),
        "header_fill": PatternFill("solid", fgColor="305496"),
        "header_font": Font(bold=True, color="FFFFFF"),
        "money0": "#,##0",
        "money2": "#,##0.00",
        "center": Alignment(horizontal="center", vertical="center"),
        "border": Border(
            left=Side(border_style="thin", color="999999"),
            right=Side(border_style="thin", color="999999"),
            top=Side(border_style="thin", color="999999"),
            bottom=Side(border_style="thin", color="999999"),
        ),
    }


def _escribir_tabla(ws, df, fila_inicial, s, formatos):
    """Escribe el DataFrame con encabezado formateado. Devuelve la fila
    siguiente a la tabla."""
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=fila_inicial, column=j, value=col)
        c.fill = s["header_fill"]
        c.font = s["header_font"]
        c.alignment = s["center"]
        c.border = s["border"]
    for i, row in enumerate(df.itertuples(index=False), start=fila_inicial + 1):
        for j, (col, val) in enumerate(zip(df.columns, row), start=1):
            c = ws.cell(row=i, column=j, value=val)
            c.border = s["border"]
            if col in formatos:
                c.number_format = formatos[col]
    return fila_inicial + len(df) + 1


def build_xlsx_bono(bono: dict, detalle: dict, periodos: list[str],
                    label: str, regla: str = REGLA_VIGENTE) -> bytes:
    """Planilla del bono para RRHH: resumen + detalle mes a mes + reglas."""
    s = _styles()
    wb = Workbook()

    resumen = tabla_resumen(bono)
    ws = wb.active
    ws.title = "Bono trimestral"
    ws["A1"] = f"Bono trimestral — {label}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (
        f"Meses del trimestre: {', '.join(periodos)}. "
        f"Esquema v1.2. Importes en UYU."
    )
    fila = _escribir_tabla(
        ws, resumen, 4, s,
        {"com_venta_trim": s["money2"], "bono_venta": s["money0"],
         "com_cobranza_trim": s["money2"], "bono_cobranza": s["money0"],
         "bono_total": s["money0"]},
    )
    # Fila de total
    ws.cell(row=fila, column=1, value="TOTAL").font = s["bold"]
    for col_name in ("bono_venta", "bono_cobranza", "bono_total"):
        j = list(resumen.columns).index(col_name) + 1
        c = ws.cell(row=fila, column=j, value=int(resumen[col_name].sum()))
        c.font = s["bold"]
        c.number_format = s["money0"]

    for j, col in enumerate(resumen.columns, start=1):
        ws.column_dimensions[get_column_letter(j)].width = max(14, len(col) + 4)

    ws2 = wb.create_sheet("Detalle mensual")
    det = tabla_detalle(detalle, periodos)
    ws2["A1"] = "Detalle mes a mes (volumen crudo y computado)"
    ws2["A1"].font = Font(bold=True, size=12)
    _escribir_tabla(
        ws2, det, 3, s,
        {"venta_neta": s["money2"], "venta_computada": s["money2"],
         "cobranza": s["money2"], "cobranza_computada": s["money2"]},
    )
    for j, col in enumerate(det.columns, start=1):
        ws2.column_dimensions[get_column_letter(j)].width = max(14, len(col) + 4)

    ws3 = wb.create_sheet("Cómo se calcula")
    ws3["A1"] = "Reglas del bono trimestral (esquema v1.2)"
    ws3["A1"].font = Font(bold=True, size=12)
    for i, linea in enumerate(explicacion_bono(regla), start=3):
        ws3.cell(row=i, column=1, value=f"· {linea}")
    ws3.column_dimensions["A"].width = 120

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
