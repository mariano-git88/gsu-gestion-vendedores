"""
credito.py — Scoring crediticio de la cartera de Suprabond UY.

Responde una pregunta concreta: **a qué cliente le podemos ofrecer más plazo
de financiación (con cheque diferido y con interés) sin que se nos vuelva
incobrable.**

Funciones puras: no importa streamlit. La app (`credito_app.py`) arma la UI,
el pull de datos vive en `credito_api.py`.

────────────────────────────────────────────────────────────────────────────
QUÉ MIDE Y QUÉ NO MIDE
────────────────────────────────────────────────────────────────────────────
Mide **comportamiento de pago observado**: cuántos días tarda cada cliente en
pagar respecto del plazo que se le pactó, con qué consistencia, hace cuánto
que compra y cuánto pesa.

NO es una probabilidad de default calibrada. Para eso haría falta una serie de
incobrables observados y en la cartera de GSU prácticamente no hay. El score
ordena de mejor a peor pagador —eso sí lo hace con datos duros— pero no dice
"este cliente tiene 3% de chance de no pagar". Cualquier lectura en esa clave
está sobre-interpretando el número.

Tampoco ve nada de afuera: sin Clearing de Informes ni Central de Riesgos del
BCU, un cliente puede estar impecable con nosotros y estar en default con
medio mercado. Por eso la política de límites arranca conservadora.

────────────────────────────────────────────────────────────────────────────
DOS DECISIONES DE DATOS QUE HAY QUE CONOCER
────────────────────────────────────────────────────────────────────────────
1. **`FechaVencimiento` de Contabilium no sirve.** Medido sobre 10.882
   facturas (ago-2024 → ago-2026): el campo trae SIEMPRE emisión + 30 días,
   sin importar la condición de venta. Una factura a "90 Cuenta Corriente"
   figura venciendo a los 30. Usar ese campo inflaba el atraso de los
   clientes con plazo largo —justo los que nos importan— en 60 días.
   Acá el vencimiento se **deriva de la condición de venta** (ver
   `PLAZO_POR_CONDICION`).

2. **La fecha de pago no está en el comprobante.** El campo `Pagos[]` del
   detalle viene siempre vacío. Sale de cruzar contra los recibos
   (`/api/cobranzas`), que sí traen fecha, imputación por factura y forma de
   pago. Ver `credito_api.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ────────────────────────────────────────────────────────────────────────────
# Plazos
# ────────────────────────────────────────────────────────────────────────────

# Días de plazo real que implica cada condición de venta de Contabilium UY.
# Las condiciones en cuotas ("30/60") se resumen en el **vencimiento
# promedio** de las cuotas, porque el ERP emite UNA sola factura con UN solo
# saldo: no hay forma de saber qué parte correspondía a cada cuota. Es una
# aproximación conservadora en el sentido correcto (si el cliente paga todo
# junto al final, aparece atrasado; si paga en cuotas, queda parejo).
PLAZO_POR_CONDICION: dict[str, int] = {
    "Contado": 0,
    "MercadoPago": 0,
    "30 Cuenta Corriente": 30,
    "45 Cuenta Corriente": 45,
    "60 Cuenta Corriente": 60,
    "90 Cuenta Corriente": 90,
    "30/60 Cuenta Corriente": 45,
    "30/60/90 Cuenta Corriente": 60,
    "60/90 Cuenta Corriente": 75,
}

# Si aparece una condición nueva que no está en el mapa, se asume 30 días
# (es la condición por defecto de la cuenta) y se reporta aparte para que
# alguien la agregue. Nunca se descarta la factura en silencio.
PLAZO_DEFAULT = 30

TIPOS_NOTA_CREDITO = frozenset({"NCF", "NCT", "NCE", "NCTK"})

# Tolerancia para considerar saldada una factura. Contabilium redondea a 2
# decimales y las diferencias de centavos por conversión de moneda son
# habituales.
TOL_SALDO = 1.0  # UYU


def plazo_pactado(condicion: str) -> int:
    """Días de plazo que implica una condición de venta."""
    return PLAZO_POR_CONDICION.get((condicion or "").strip(), PLAZO_DEFAULT)


# ────────────────────────────────────────────────────────────────────────────
# Historial factura por factura
# ────────────────────────────────────────────────────────────────────────────

FECHA_ANCLA = pd.Timestamp("2020-01-01")


def armar_historial(
    df_comp: pd.DataFrame,
    df_pagos: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Una fila por factura, con cuántos días tardó en cobrarse.

    Args:
      df_comp: comprobantes. Columnas: id, id_cliente, razon_social, tipo,
        emision, cond_venta, total (CON IVA), saldo (CON IVA).
      df_pagos: imputaciones de recibos. Columnas: id_comprobante,
        fecha_pago, importe.
      hoy: fecha de corte. Default: hoy.

    Devuelve columnas agregadas:
      plazo_pactado, vencimiento, pagado, n_pagos, fecha_pago_pond,
      estado, dpd, dpd_corriente.

    `estado`:
      - `cobrada`     — saldo 0 y hay recibos que lo explican.
      - `parcial`     — saldo > 0 pero ya pagó algo.
      - `abierta`     — saldo > 0 y sin un peso pagado.
      - `sin_recibo`  — saldo 0 pero NO encontramos el recibo. Se cobró, pero
        no sabemos cuándo: se EXCLUYE del cálculo de mora en vez de contarse
        como impaga. Pasa con los recibos que no pudimos bajar (ver la nota
        de paginación en `credito_api`) y con facturas canceladas contra una
        NC que el recibo no imputa.

    `dpd` (days past due) solo tiene valor en las cobradas: días entre el pago
    y el vencimiento. Negativo = pagó antes. En las que siguen abiertas se usa
    `dpd_corriente` (días vencidos al día de hoy), que es otra cosa y no se
    mezcla: una factura que todavía no venció no es un pago puntual.
    """
    hoy = pd.Timestamp(hoy or pd.Timestamp.today().normalize())

    if df_comp is None or df_comp.empty or "tipo" not in df_comp:
        return pd.DataFrame(
            columns=list(getattr(df_comp, "columns", []))
            + ["plazo_pactado", "vencimiento", "pagado", "n_pagos",
               "fecha_pago_pond", "estado", "dpd", "dpd_corriente"]
        )

    fac = df_comp[~df_comp["tipo"].isin(TIPOS_NOTA_CREDITO)].copy()
    if fac.empty:
        return fac

    fac["plazo_pactado"] = fac["cond_venta"].map(plazo_pactado)
    fac["vencimiento"] = fac["emision"] + pd.to_timedelta(
        fac["plazo_pactado"], unit="D"
    )

    # --- Agregado de pagos por factura -------------------------------------
    # La fecha de pago de una factura cobrada en varios recibos se pondera por
    # importe. OJO: ponderar sobre el epoch en nanosegundos (int64) DESBORDA
    # al multiplicar por el importe (1,7e18 × 1e4 > 9,2e18). Se pondera sobre
    # días desde una fecha ancla.
    # Un DataFrame vacío puede venir sin columnas (`pd.DataFrame([])`), así que
    # no se puede indexar a ciegas.
    if df_pagos is None or df_pagos.empty or "id_comprobante" not in df_pagos:
        pag = pd.DataFrame(columns=["id_comprobante", "fecha_pago", "importe"])
    else:
        pag = df_pagos[df_pagos["id_comprobante"].isin(set(fac["id"]))].copy()
    if pag.empty:
        agg = pd.DataFrame(
            columns=["pagado", "n_pagos", "fecha_pago_pond"]
        ).rename_axis("id_comprobante")
    else:
        pag["dias_ancla"] = (
            pag["fecha_pago"] - FECHA_ANCLA
        ).dt.total_seconds() / 86400.0
        g = pag.groupby("id_comprobante")
        suma = g["importe"].sum()
        pond = g.apply(
            lambda x: (
                (x["dias_ancla"] * x["importe"]).sum() / x["importe"].sum()
                if x["importe"].sum() > 0
                else x["dias_ancla"].max()
            ),
            include_groups=False,
        )
        agg = pd.DataFrame(
            {
                "pagado": suma,
                "n_pagos": g.size(),
                "fecha_pago_pond": FECHA_ANCLA
                + pd.to_timedelta(pond, unit="D"),
            }
        )

    fac = fac.merge(agg, left_on="id", right_index=True, how="left")
    fac["pagado"] = pd.to_numeric(fac["pagado"], errors="coerce").fillna(0.0)
    fac["n_pagos"] = pd.to_numeric(fac["n_pagos"], errors="coerce").fillna(0).astype(int)
    # Si NINGUNA factura tiene pago, la columna queda toda vacía y con dtype
    # `object`: restarle un datetime tira `cannot subtract DatetimeArray from
    # ndarray[object]`. Forzar el dtype es obligatorio, no cosmético.
    fac["fecha_pago_pond"] = pd.to_datetime(
        fac.get("fecha_pago_pond"), errors="coerce"
    )

    saldada = fac["saldo"] <= TOL_SALDO
    con_pago = fac["pagado"] > TOL_SALDO
    fac["estado"] = np.select(
        [saldada & con_pago, saldada & ~con_pago, con_pago],
        ["cobrada", "sin_recibo", "parcial"],
        default="abierta",
    )

    fac["dpd"] = np.where(
        fac["estado"] == "cobrada",
        (fac["fecha_pago_pond"] - fac["vencimiento"]).dt.days,
        np.nan,
    )
    fac["dpd_corriente"] = np.where(
        fac["estado"].isin(["abierta", "parcial"]),
        (hoy - fac["vencimiento"]).dt.days,
        np.nan,
    )
    return fac


# ────────────────────────────────────────────────────────────────────────────
# Features por cliente
# ────────────────────────────────────────────────────────────────────────────

MIN_FACTURAS_SCORE = 3  # abajo de esto no hay historia suficiente para opinar

# Una factura vieja a la que le quedó un resto chico casi nunca es deuda: es
# la **Nota de Crédito del 10% de la rendición que nunca se imputó**. Medido
# sobre 704 facturas parciales vencidas hace más de 60 días: 125 tienen un
# resto de exactamente 10% (pico clarísimo en el histograma) y 563 tienen un
# resto menor al 25%, por apenas $376k en total. Las 141 con un resto mayor al
# 25% concentran $816k — esas sí son deuda.
#
# Tratar las colas como mora ensucia el score de los mejores clientes: hay
# quien paga 14 días ANTES del vencimiento y quedaba en rojo por un resto de
# $820 de una factura de 2025. Se marcan aparte y se reportan; no se borran.
RESIDUO_RATIO_MAX = 0.25
RESIDUO_DIAS_MIN = 60


def marcar_residuos(
    viva: pd.DataFrame,
    ratio_max: float = RESIDUO_RATIO_MAX,
    dias_min: float = RESIDUO_DIAS_MIN,
) -> pd.DataFrame:
    """Marca `es_residuo` en las facturas con saldo: cola de rendición vs deuda.

    Es un criterio de negocio, no una verdad: se apaga pasando
    `ratio_max=-1` (así nada califica como residuo y todo cuenta como deuda).
    Va por parámetro y no por constante de módulo justamente para que
    apagarlo en la app no deje el módulo mutado para el resto de la sesión.
    """
    d = viva.copy()
    if d.empty:
        d["ratio_residuo"] = pd.Series(dtype=float)
        d["es_residuo"] = pd.Series(dtype=bool)
        return d
    ratio = np.where(d["total"] > 0, d["saldo"] / d["total"], 1.0)
    d["ratio_residuo"] = ratio
    d["es_residuo"] = (
        (d["estado"] == "parcial")
        & (ratio <= ratio_max)
        & (d["dpd_corriente"] > dias_min)
    )
    return d


def resumen_notas_credito(
    df_comp: pd.DataFrame,
    hoy: pd.Timestamp | None = None,
    ventana_dias: int = 365,
) -> pd.DataFrame:
    """Notas de crédito por cliente: cuánto descontaron y cuánto quedó sin usar.

    Las NC vienen con `total` negativo. Importa por dos motivos:
      - Las ventas del cliente hay que netearlas de NC, o el DSO y el límite
        de crédito quedan inflados (en GSU la NC del 10% de la rendición es
        rutina: 8.873.838 en NC sobre 24 meses).
      - Una NC con saldo propio distinto de cero es un crédito que el cliente
        todavía tiene a favor y que no se aplicó a ninguna factura (553 casos,
        $1,46M). Resta de la exposición real.
    """
    hoy = pd.Timestamp(hoy or pd.Timestamp.today().normalize())
    desde = hoy - pd.Timedelta(days=ventana_dias)
    nc = df_comp[df_comp["tipo"].isin(TIPOS_NOTA_CREDITO)]
    if nc.empty:
        return pd.DataFrame(columns=["nc_12m", "saldo_nc"]).rename_axis("id_cliente")
    return pd.DataFrame(
        {
            "nc_12m": nc[nc["emision"] >= desde].groupby("id_cliente")["total"].sum(),
            "saldo_nc": nc.groupby("id_cliente")["saldo"].sum(),
        }
    ).fillna(0.0)


def features_por_cliente(
    hist: pd.DataFrame,
    df_formas: pd.DataFrame | None = None,
    hoy: pd.Timestamp | None = None,
    ventana_dias: int = 365,
    resumen_nc: pd.DataFrame | None = None,
    residuo_ratio_max: float = RESIDUO_RATIO_MAX,
) -> pd.DataFrame:
    """Resume el historial en una fila por cliente.

    `ventana_dias` acota el comportamiento de pago a lo reciente (default 12
    meses): un cliente que pagaba mal hace dos años y hace un año que paga
    bien tiene que puntuar por lo que hace hoy. La antigüedad y el volumen sí
    miran todo el historial disponible.
    """
    hoy = pd.Timestamp(hoy or pd.Timestamp.today().normalize())
    desde = hoy - pd.Timedelta(days=ventana_dias)

    if hist.empty:
        return pd.DataFrame()

    # --- comportamiento de pago (ventana) ----------------------------------
    cob = hist[(hist["estado"] == "cobrada") & (hist["emision"] >= desde)]

    def _pago(g: pd.DataFrame) -> pd.Series:
        peso = g["total"].clip(lower=0)
        total = peso.sum()
        return pd.Series(
            {
                "facturas_cobradas": float(len(g)),
                "dpd_pond": (
                    (g["dpd"] * peso).sum() / total if total > 0 else g["dpd"].mean()
                ),
                "dpd_mediana": g["dpd"].median(),
                "dpd_p90": g["dpd"].quantile(0.90),
                "dpd_max": g["dpd"].max(),
                "pct_puntual": float((g["dpd"] <= 5).mean()),
                "plazo_pactado_max": float(g["plazo_pactado"].max()),
            }
        )

    f_pago = (
        cob.groupby("id_cliente").apply(_pago, include_groups=False)
        if not cob.empty
        else pd.DataFrame()
    )

    # --- volumen, antigüedad, continuidad (todo el historial) --------------
    def _act(g: pd.DataFrame) -> pd.Series:
        en_vent = g[g["emision"] >= desde]
        meses = g["emision"].dt.to_period("M")
        meses_vent = en_vent["emision"].dt.to_period("M").nunique()
        # Tendencia: últimos 3 meses contra los 3 anteriores.
        u3 = g[g["emision"] >= hoy - pd.Timedelta(days=90)]["total"].sum()
        p3 = g[
            (g["emision"] >= hoy - pd.Timedelta(days=180))
            & (g["emision"] < hoy - pd.Timedelta(days=90))
        ]["total"].sum()
        return pd.Series(
            {
                "razon_social": g["razon_social"].iloc[-1],
                "primera_compra": g["emision"].min(),
                "ultima_compra": g["emision"].max(),
                "antiguedad_meses": float(
                    (hoy - g["emision"].min()).days / 30.44
                ),
                "meses_activos_12m": float(meses_vent),
                "meses_con_compra": float(meses.nunique()),
                "facturas_12m": float(len(en_vent)),
                "monto_12m": float(en_vent["total"].sum()),
                "monto_total": float(g["total"].sum()),
                "tendencia_3m": float(u3 / p3 - 1.0) if p3 > 0 else np.nan,
            }
        )

    f_act = hist.sort_values("emision").groupby("id_cliente").apply(
        _act, include_groups=False
    )

    # --- exposición y mora viva (hoy) --------------------------------------
    viva = marcar_residuos(
        hist[hist["estado"].isin(["abierta", "parcial"])].copy(),
        ratio_max=residuo_ratio_max,
    )
    if viva.empty:
        f_exp = pd.DataFrame(
            columns=[
                "saldo_vivo", "saldo_vencido", "saldo_vencido_60",
                "saldo_residual", "dpd_vivo_max",
            ]
        )
    else:
        real = ~viva["es_residuo"]
        f_exp = viva.assign(_real=real).groupby("id_cliente").apply(
            lambda g: pd.Series(
                {
                    "saldo_vivo": g["saldo"].sum(),
                    "saldo_residual": g.loc[~g["_real"], "saldo"].sum(),
                    "saldo_vencido": g.loc[
                        g["_real"] & (g["dpd_corriente"] > 0), "saldo"
                    ].sum(),
                    "saldo_vencido_60": g.loc[
                        g["_real"] & (g["dpd_corriente"] > 60), "saldo"
                    ].sum(),
                    "dpd_vivo_max": g.loc[g["_real"], "dpd_corriente"].max(),
                }
            ),
            include_groups=False,
        )

    # --- exposición máxima histórica ---------------------------------------
    # Cuánto llegó a deberle este cliente a la empresa en algún momento. Es el
    # techo que la empresa ya toleró de hecho: sirve de referencia para no
    # proponer un límite absurdo hacia arriba ni hacia abajo.
    f_max = _exposicion_maxima(hist)

    feat = (
        f_act.join(f_pago, how="left")
        .join(f_exp, how="left")
        .join(f_max, how="left")
        .fillna(
            {
                "saldo_vivo": 0.0,
                "saldo_vencido": 0.0,
                "saldo_vencido_60": 0.0,
                "facturas_cobradas": 0.0,
                "exposicion_max": 0.0,
            }
        )
    )

    # --- forma de pago: ¿ya opera con cheque? ------------------------------
    feat["pct_cheque"] = 0.0
    feat["usa_cheque"] = False
    if df_formas is not None and not df_formas.empty:
        fp = df_formas[df_formas["id_cliente"].notna()]
        tot = fp.groupby("id_cliente")["importe"].sum()
        chq = (
            fp[fp["forma"].str.strip().str.lower() == "cheque"]
            .groupby("id_cliente")["importe"]
            .sum()
        )
        pct = (chq / tot).dropna()
        feat["pct_cheque"] = feat.index.map(pct).astype(float)
        feat["pct_cheque"] = feat["pct_cheque"].fillna(0.0)
        feat["usa_cheque"] = feat["pct_cheque"] > 0

    # --- notas de crédito: netear ventas y exposición -----------------------
    if resumen_nc is not None and not resumen_nc.empty:
        feat = feat.join(resumen_nc, how="left")
    for c in ("nc_12m", "saldo_nc"):
        if c not in feat.columns:
            feat[c] = 0.0
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    # Todas las columnas de plata y de días tienen que ser numéricas de verdad.
    # Cuando un cliente no tiene ninguna factura abierta, el join deja esas
    # columnas en NaN con dtype `object`, y después `.round()` o `.clip()`
    # revientan con "no callable rint method". Coerción explícita, no
    # cosmética.
    for c in (
        "monto_12m", "nc_12m", "saldo_vivo", "saldo_nc", "saldo_vencido",
        "saldo_vencido_60", "saldo_residual", "exposicion_max", "dpd_vivo_max",
    ):
        if c not in feat.columns:
            feat[c] = 0.0
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    # `nc_12m` y `saldo_nc` vienen negativos: se SUMAN para netear.
    feat["ventas_netas_12m"] = (feat["monto_12m"] + feat["nc_12m"]).clip(lower=0)
    feat["exposicion_neta"] = (feat["saldo_vivo"] + feat["saldo_nc"]).clip(lower=0)
    feat["compra_mensual"] = feat["ventas_netas_12m"] / 12.0

    # --- DSO: días de venta que la empresa tiene prestados a este cliente ---
    # Es la medida robusta. El DPD por factura depende de a qué factura se
    # imputó cada recibo, y en Contabilium los recibos NO se aplican a la más
    # vieja: se cierran facturas nuevas y queda una cola de facturas viejas
    # abiertas. Eso hace que las facturas cerradas parezcan puntuales aunque
    # la deuda real no baje. El DSO no se deja engañar por eso: mira el saldo
    # total contra el ritmo de venta, sin importar cómo se imputó.
    #
    # Caso testigo: SODIMAC tiene DPD ponderado de -2,7 días (pagaría "antes
    # del vencimiento") y un DSO de 203 días con plazo pactado de 60.
    venta_diaria = feat["ventas_netas_12m"] / 365.0
    feat["dso"] = pd.Series(
        np.where(
            venta_diaria > 0, feat["exposicion_neta"] / venta_diaria, np.nan
        ),
        index=feat.index,
        dtype="float64",
    ).round(0)
    feat["exceso_dso"] = (
        feat["dso"]
        - pd.to_numeric(feat.get("plazo_pactado_max"), errors="coerce").fillna(
            PLAZO_DEFAULT
        )
    ).round(0)
    # Plata inmovilizada por encima de lo pactado: los días de exceso valuados
    # al ritmo de venta del cliente. Es el costo financiero que ya se está
    # pagando hoy, sin cobrar interés.
    feat["capital_excedido"] = (
        feat["exceso_dso"].clip(lower=0) * venta_diaria
    ).round(0)

    feat["historia_suficiente"] = feat["facturas_cobradas"] >= MIN_FACTURAS_SCORE
    return feat.reset_index()


def _exposicion_maxima(hist: pd.DataFrame) -> pd.DataFrame:
    """Saldo adeudado máximo que alcanzó cada cliente en el historial.

    Se reconstruye por evento: cada factura suma su total el día de emisión y
    lo resta el día del pago. El máximo del acumulado es el pico de deuda.
    Las facturas `sin_recibo` se restan en su vencimiento (no sabemos cuándo
    se pagaron; asumir que nunca se pagaron inflaría el pico).
    """
    if hist.empty:
        return pd.DataFrame(columns=["exposicion_max"])

    ev = []
    for _, r in hist.iterrows():
        if pd.isna(r["emision"]):
            continue
        ev.append((r["id_cliente"], r["emision"], float(r["total"])))
        if r["estado"] == "cobrada" and pd.notna(r["fecha_pago_pond"]):
            ev.append((r["id_cliente"], r["fecha_pago_pond"], -float(r["total"])))
        elif r["estado"] == "sin_recibo":
            ev.append((r["id_cliente"], r["vencimiento"], -float(r["total"])))
        elif r["estado"] == "parcial":
            ev.append(
                (r["id_cliente"], r["emision"], -float(r["pagado"]))
            )

    if not ev:
        return pd.DataFrame(columns=["exposicion_max"])

    de = pd.DataFrame(ev, columns=["id_cliente", "fecha", "delta"]).sort_values(
        ["id_cliente", "fecha"]
    )
    de["acum"] = de.groupby("id_cliente")["delta"].cumsum()
    return (
        de.groupby("id_cliente")["acum"]
        .max()
        .clip(lower=0)
        .rename("exposicion_max")
        .to_frame()
    )


# ────────────────────────────────────────────────────────────────────────────
# Score
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ConfigScore:
    """Parámetros del score. Todos visibles y editables desde la app.

    Nada de esto es una constante de la naturaleza: son criterios de negocio.
    Están acá afuera justamente para que se puedan discutir y mover sin tocar
    la lógica.
    """

    # Puntaje: cuánto pesa cada pilar (suman 100).
    #
    # El DSO pesa más que el DPD a propósito: es la única de las dos que no
    # depende de a qué factura se imputó cada recibo. El DPD aporta el matiz
    # (¿paga puntual cuando paga?), el DSO aporta la verdad (¿cuánta plata mía
    # tiene, y hace cuánto?).
    peso_dso: float = 25.0        # días de venta prestados por encima del plazo
    peso_dpd: float = 20.0        # atraso promedio ponderado por monto
    peso_p90: float = 10.0        # el peor caso habitual (consistencia)
    peso_puntualidad: float = 10.0
    peso_antiguedad: float = 15.0
    peso_volumen: float = 10.0
    peso_situacion: float = 10.0  # mora viva hoy

    # Escalas de los pilares de pago: dónde se saca el máximo y dónde cero.
    dso_optimo: float = 0.0    # no debe más días que los pactados
    dso_cero: float = 90.0     # 90 días de exceso sobre lo pactado = 0 puntos
    dpd_optimo: float = 0.0    # pagar al vencimiento o antes = puntaje pleno
    dpd_cero: float = 60.0     # 60 días de atraso promedio = 0 puntos
    p90_optimo: float = 10.0
    p90_cero: float = 90.0

    # Cortes de banda.
    corte_a: float = 85.0
    corte_b: float = 70.0
    corte_c: float = 55.0
    corte_d: float = 40.0

    # Vetos: cualquiera de estos manda al cliente a rojo sin importar el
    # puntaje. Son las señales que no se compensan con volumen ni antigüedad.
    #
    # El monto tiene que ser MATERIAL, si no el veto es inútil: con el umbral
    # en $1 se vetaba al 44% de la cartera, incluida gente que paga 14 días
    # antes del vencimiento y arrastraba $820 de una factura de 2025. El
    # umbral es el mayor entre un piso fijo y una fracción de lo que el
    # cliente compra por mes — deber $20.000 no significa lo mismo para quien
    # compra $30.000 al mes que para quien compra $400.000.
    veto_dias_vencido: float = 60.0
    veto_monto_piso: float = 20_000.0      # UYU
    veto_monto_meses_compra: float = 0.5   # ...o medio mes de compra, el mayor

    # Política de plazos por banda (días).
    plazo_por_banda: dict[str, int] = field(
        default_factory=lambda: {"A": 90, "B": 60, "C": 45, "D": 30, "E": 0}
    )
    # Colchón sobre la exposición natural del plazo.
    factor_limite: dict[str, float] = field(
        default_factory=lambda: {"A": 1.3, "B": 1.15, "C": 1.0, "D": 0.8, "E": 0.0}
    )

    # Tasa. `costo_fondos` es lo que le cuesta a GSU financiar (o lo que
    # rendiría esa plata en otro lado). `spread` es el margen que se le quiere
    # sacar al negocio de financiar. La prima de riesgo por banda NO está
    # calibrada contra defaults: sale de la mora observada de cada banda
    # (ver `prima_por_mora_observada`).
    costo_fondos_anual: float = 0.14   # 14% anual en UYU — PARÁMETRO A CONFIRMAR
    spread_anual: float = 0.06
    prima_riesgo: dict[str, float] = field(
        default_factory=lambda: {"A": 0.01, "B": 0.03, "C": 0.06, "D": 0.10, "E": 0.0}
    )

    # Tope duro de exposición por cliente, como múltiplo de su compra mensual.
    tope_meses_compra: float = 3.0


def _escala(v, optimo: float, cero: float, peso: float):
    """Escala lineal: `optimo` → peso, `cero` → 0. Fuera de rango, recorta."""
    v = pd.to_numeric(v, errors="coerce")
    frac = (cero - v) / (cero - optimo)
    return (frac.clip(0.0, 1.0) * peso).fillna(0.0)


def scorear(feat: pd.DataFrame, cfg: ConfigScore | None = None) -> pd.DataFrame:
    """Calcula el score 0-100 y la banda de cada cliente.

    Devuelve `feat` con las columnas del puntaje y:
      score, banda, semaforo, motivo_veto, historia_suficiente.

    Los clientes sin historia suficiente (menos de `MIN_FACTURAS_SCORE`
    facturas cobradas en la ventana) reciben banda "S/D" — sin datos. No se
    los castiga con un puntaje bajo, porque no sabemos nada de ellos; se los
    manda a decisión manual, que es lo honesto.
    """
    cfg = cfg or ConfigScore()
    d = feat.copy()

    # --- Pilar 0: exceso de DSO sobre el plazo pactado ---------------------
    # Un cliente sin saldo (exceso_dso muy negativo) saca el puntaje pleno.
    # Un cliente sin ventas en la ventana queda en NaN: `_escala` lo lleva a
    # 0, pero además va a caer en "sin historia suficiente" más abajo.
    d["p_dso"] = _escala(d["exceso_dso"], cfg.dso_optimo, cfg.dso_cero, cfg.peso_dso)

    # --- Pilar 1: atraso promedio ponderado por monto ----------------------
    d["p_dpd"] = _escala(d["dpd_pond"], cfg.dpd_optimo, cfg.dpd_cero, cfg.peso_dpd)

    # --- Pilar 2: el peor caso habitual (p90) ------------------------------
    d["p_p90"] = _escala(d["dpd_p90"], cfg.p90_optimo, cfg.p90_cero, cfg.peso_p90)

    # --- Pilar 3: puntualidad (% de facturas pagadas casi a término) -------
    d["p_puntual"] = (
        pd.to_numeric(d["pct_puntual"], errors="coerce").fillna(0.0)
        * cfg.peso_puntualidad
    )

    # --- Pilar 4: antigüedad + continuidad ---------------------------------
    # Mitad por antigüedad (tope a 24 meses) y mitad por cuántos de los
    # últimos 12 meses compró. Un cliente viejo que dejó de comprar no puede
    # puntuar igual que uno viejo y activo.
    ant = (pd.to_numeric(d["antiguedad_meses"], errors="coerce") / 24.0).clip(0, 1)
    cont = (pd.to_numeric(d["meses_activos_12m"], errors="coerce") / 12.0).clip(0, 1)
    d["p_antiguedad"] = (ant * 0.5 + cont * 0.5).fillna(0.0) * cfg.peso_antiguedad

    # --- Pilar 5: volumen y tendencia --------------------------------------
    # El volumen entra en escala log: la diferencia entre un cliente de
    # $50k y uno de $500k importa; entre uno de $3M y uno de $5M, casi no.
    m = pd.to_numeric(d["monto_12m"], errors="coerce").fillna(0.0).clip(lower=0)
    v_norm = np.log1p(m) / np.log1p(max(m.max(), 1.0)) if m.max() > 0 else m * 0
    tend = pd.to_numeric(d["tendencia_3m"], errors="coerce").fillna(0.0)
    t_norm = ((tend + 0.5) / 1.0).clip(0, 1)  # -50% → 0 ; +50% → 1
    d["p_volumen"] = (v_norm * 0.7 + t_norm * 0.3) * cfg.peso_volumen

    # --- Pilar 6: cómo está hoy --------------------------------------------
    # Proporción de la deuda viva que está vencida. Sin deuda vencida = pleno.
    vivo = pd.to_numeric(d["saldo_vivo"], errors="coerce").fillna(0.0)
    venc = pd.to_numeric(d["saldo_vencido"], errors="coerce").fillna(0.0)
    frac_venc = np.where(vivo > 0, venc / vivo, 0.0)
    d["p_situacion"] = (1.0 - np.clip(frac_venc, 0, 1)) * cfg.peso_situacion

    d["score"] = (
        d["p_dso"]
        + d["p_dpd"]
        + d["p_p90"]
        + d["p_puntual"]
        + d["p_antiguedad"]
        + d["p_volumen"]
        + d["p_situacion"]
    ).round(1)

    # --- Bandas ------------------------------------------------------------
    d["banda"] = pd.cut(
        d["score"],
        bins=[-0.1, cfg.corte_d, cfg.corte_c, cfg.corte_b, cfg.corte_a, 100.1],
        labels=["E", "D", "C", "B", "A"],
    ).astype(str)

    # --- Vetos -------------------------------------------------------------
    umbral = np.maximum(
        cfg.veto_monto_piso,
        pd.to_numeric(d["compra_mensual"], errors="coerce").fillna(0.0)
        * cfg.veto_monto_meses_compra,
    )
    d["umbral_veto"] = umbral.round(0)
    vencido_60 = pd.to_numeric(d["saldo_vencido_60"], errors="coerce").fillna(0.0)
    veto = (
        pd.to_numeric(d["dpd_vivo_max"], errors="coerce").fillna(-999)
        > cfg.veto_dias_vencido
    ) & (vencido_60 > umbral)

    d["motivo_veto"] = np.where(
        veto,
        "Debe "
        + vencido_60.round(0).map("{:,.0f}".format)
        + f" hace más de {int(cfg.veto_dias_vencido)} días",
        "",
    )
    d.loc[veto, "banda"] = "E"

    # --- Sin datos ---------------------------------------------------------
    sin_datos = ~d["historia_suficiente"].fillna(False)
    d.loc[sin_datos & ~veto, "banda"] = "S/D"
    d.loc[sin_datos & ~veto, "motivo_veto"] = (
        f"Menos de {MIN_FACTURAS_SCORE} facturas cobradas en la ventana"
    )

    d["semaforo"] = d["banda"].map(
        {"A": "🟢", "B": "🟢", "C": "🟡", "D": "🟡", "E": "🔴", "S/D": "⚪"}
    )
    return d


# ────────────────────────────────────────────────────────────────────────────
# Política: plazo, límite y tasa
# ────────────────────────────────────────────────────────────────────────────


def politica(scored: pd.DataFrame, cfg: ConfigScore | None = None) -> pd.DataFrame:
    """Traduce el score en una oferta concreta por cliente.

    Agrega:
      plazo_sugerido    — días de financiación que se le pueden ofrecer.
      plazo_actual      — el plazo más largo que ya se le venía dando.
      limite_sugerido   — exposición máxima en $ (con IVA).
      margen_disponible — límite menos lo que ya debe.
      tasa_anual        — tasa a cobrar por la financiación.
      recargo_pct       — cuánto encarece el precio ese plazo extra, en %.

    El límite sale de la **exposición natural** del plazo: si a un cliente que
    me compra $X por mes le doy 60 días, en régimen me va a estar debiendo 2
    meses de compra. Eso es lo que hay que dimensionar, no un número al azar.
    Después se recorta por el tope duro (`tope_meses_compra`) para no
    concentrar riesgo en un solo nombre.
    """
    cfg = cfg or ConfigScore()
    d = scored.copy()

    d["plazo_sugerido"] = d["banda"].map(cfg.plazo_por_banda).fillna(0).astype(int)
    d["plazo_actual"] = (
        pd.to_numeric(d.get("plazo_pactado_max"), errors="coerce")
        .fillna(PLAZO_DEFAULT)
        .astype(int)
    )

    compra_mes = pd.to_numeric(d["compra_mensual"], errors="coerce").fillna(0.0)
    factor = d["banda"].map(cfg.factor_limite).fillna(0.0).astype(float)

    exposicion_natural = compra_mes * (d["plazo_sugerido"] / 30.0)
    tope = compra_mes * cfg.tope_meses_compra
    d["limite_sugerido"] = np.minimum(exposicion_natural * factor, tope).round(0)
    d.loc[d["banda"].isin(["E", "S/D"]), "limite_sugerido"] = 0.0

    # Contra el límite se mide la exposición NETA (saldo de facturas menos las
    # NC que el cliente todavía tiene a favor sin aplicar), que es la plata
    # que realmente se le debe reclamar.
    d["margen_disponible"] = (
        d["limite_sugerido"]
        - pd.to_numeric(d["exposicion_neta"], errors="coerce").fillna(0)
    ).round(0)

    # --- Tasa ---------------------------------------------------------------
    prima = d["banda"].map(cfg.prima_riesgo).fillna(0.0).astype(float)
    d["tasa_anual"] = (cfg.costo_fondos_anual + cfg.spread_anual + prima).round(4)

    # Cuánto encarece el producto ese plazo EXTRA respecto de los 30 días que
    # ya se dan hoy sin cargo. Es el número que el vendedor necesita para
    # cotizar: "a 90 días te sale 3,9% más caro".
    dias_extra = (d["plazo_sugerido"] - PLAZO_DEFAULT).clip(lower=0)
    d["dias_financiados_extra"] = dias_extra
    d["recargo_pct"] = (d["tasa_anual"] * dias_extra / 365.0 * 100).round(2)
    d.loc[d["banda"].isin(["E", "S/D"]), ["tasa_anual", "recargo_pct"]] = 0.0

    return d


def prima_por_mora_observada(
    scored: pd.DataFrame, costo_fondos_anual: float
) -> pd.DataFrame:
    """Prima de riesgo derivada de la mora que cada banda efectivamente tiene.

    En vez de inventar la prima, se mide: si la banda C se atrasa en promedio
    18 días más que la A, esos 18 días de financiación no pactada tienen un
    costo real = costo_fondos × 18/365. Eso es lo mínimo que habría que
    cobrarle de más para quedar igual que con un cliente A.

    No cubre el riesgo de NO cobrar (para eso haría falta serie de
    incobrables): cubre el costo de cobrar tarde, que es lo que sí se puede
    medir con estos datos. Es un piso, no la prima final.
    """
    base = scored[scored["banda"].isin(["A", "B", "C", "D", "E"])]
    if base.empty:
        return pd.DataFrame()

    res = base.groupby("banda", observed=True).apply(
        lambda g: pd.Series(
            {
                "clientes": float(len(g)),
                "dpd_pond_prom": float(
                    np.average(
                        pd.to_numeric(g["dpd_pond"], errors="coerce").fillna(0),
                        weights=pd.to_numeric(g["monto_12m"], errors="coerce")
                        .fillna(0)
                        .clip(lower=0.01),
                    )
                ),
                "monto_12m": float(g["ventas_netas_12m"].sum()),
            }
        ),
        include_groups=False,
    )
    ref = res.loc["A", "dpd_pond_prom"] if "A" in res.index else res["dpd_pond_prom"].min()
    res["dias_mora_extra"] = (res["dpd_pond_prom"] - ref).clip(lower=0).round(1)
    res["prima_piso"] = (
        costo_fondos_anual * res["dias_mora_extra"] / 365.0
    ).round(4)
    return res.reset_index()


def resumen_cartera(scored: pd.DataFrame) -> pd.DataFrame:
    """Una fila por banda: cuántos clientes, cuánta facturación, cuánta mora."""
    if scored.empty:
        return pd.DataFrame()
    orden = ["A", "B", "C", "D", "E", "S/D"]
    r = (
        scored.groupby("banda", observed=True)
        .agg(
            clientes=("id_cliente", "count"),
            ventas_12m=("ventas_netas_12m", "sum"),
            exposicion=("exposicion_neta", "sum"),
            saldo_vencido=("saldo_vencido", "sum"),
            capital_excedido=("capital_excedido", "sum"),
            dso_prom=("dso", "median"),
            dpd_pond_prom=("dpd_pond", "mean"),
        )
        .reindex(orden)
        .dropna(how="all")
    )
    r["%_clientes"] = (r["clientes"] / r["clientes"].sum() * 100).round(1)
    r["%_ventas"] = (r["ventas_12m"] / r["ventas_12m"].sum() * 100).round(1)
    return r.reset_index()
