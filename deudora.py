"""
deudora.py — Estado de cuenta corriente por cliente y por vendedor.

Es la "deudora" que pidió Ernesto (mail del 27-ago-2026), modelada sobre los
dos informes que usan en ERPA: `Deudora 049.pdf` (el PDF que se le mandaba a
cada vendedor desde el SUMMA viejo) y `Detalle_Jefatura_EAbreu.xlsx` (el que
baja Romina del SUMMA Advanced). Ambos en `assets/`.

Módulo puro: recibe DataFrames, devuelve DataFrames. No importa streamlit.
La bajada vive en `credito_api.py` y la UI en `deudora_app.py`.

Reusa `credito.py` para todo lo que ya estaba resuelto — el vencimiento
derivado de la condición de venta y la regla de fin de mes de las grandes
superficies — en vez de recalcularlo con otro criterio. Ya tuvimos el caso de
la misma pregunta contestada con dos números distintos en `metrics.py` y
`credito.py`; no agregamos un tercero.

────────────────────────────────────────────────────────────────────────────
POR QUÉ EL SALDO NO SE CALCULA SUMANDO LOS MOVIMIENTOS
────────────────────────────────────────────────────────────────────────────
El informe de ERPA trae una columna "Saldo Acum." que va sumando debe y
haber. Acá esa columna sería **mentira** por dos razones independientes:

1. **La ventana.** Los recibos se bajan por rango de fechas. Un pago hecho
   antes del rango no aparece, así que el acumulado arrancaría de un saldo
   inicial desconocido. Las facturas tienen el mismo problema.

2. **El descuento del 10% se contaría dos veces.** En GSU el descuento por
   pago se instrumenta como nota de crédito, y esa NC aparece *también*
   como forma de pago dentro del recibo que cancela la factura. Sumar la NC
   como movimiento y además el recibo completo duplica el mismo peso.

Por eso la deuda sale del campo `saldo` de cada comprobante, que es lo que
el ERP considera pendiente y ya está neto de todo lo aplicado (misma fuente
que usa `credito.armar_historial`). Los movimientos se muestran como
**detalle cronológico informativo**, nunca como la aritmética del total.
"""

from __future__ import annotations

import pandas as pd

import credito
import cuotas as CU

# ────────────────────────────────────────────────────────────────────────────
# Constantes
# ────────────────────────────────────────────────────────────────────────────

# Etiqueta para los clientes sin vendedor asignado en Contabilium. NO se
# filtran: al cierre de la sesión del 18-ago había 148 clientes sin vendedor
# concentrando $1,7M de deuda vencida. Si la deudora los escondiera, esa
# plata no aparecería en ninguna pantalla de nadie.
SIN_VENDEDOR = "— Sin vendedor asignado —"

# Tramos de antigüedad. Mismos cortes que `metrics.aging_por_cliente` para
# que las dos pantallas se puedan comparar sin traducir.
BUCKETS = [
    ("al_dia", "Al día", None, 0),
    ("b_1_30", "1 a 30", 1, 30),
    ("b_31_60", "31 a 60", 31, 60),
    ("b_61_90", "61 a 90", 61, 90),
    ("b_90_mas", "Más de 90", 91, None),
]
COLS_BUCKET = [b[0] for b in BUCKETS]

TIPO_FACTURA = "Factura"
TIPO_NOTA_CREDITO = "Nota de crédito"
TIPO_RECIBO = "Recibo"

COLS_MOV = [
    "id_cliente", "razon_social", "fecha", "tipo_mov", "comprobante",
    "cuota", "cond_pago", "vencimiento", "dias_vencido", "debe", "haber",
    "saldo_pendiente", "id_comprobante",
]

COLS_RESUMEN = [
    "id_cliente", "razon_social", "documento", "vendedor", "ciudad",
    "telefono", "email", "facturas_abiertas", "deuda_total", "vencido",
    "vencido_neto", "credito_a_favor", "dias_mas_vieja", "peor_tramo",
    "ultima_compra", "ultimo_pago", *COLS_BUCKET,
]


# ────────────────────────────────────────────────────────────────────────────
# Movimientos — el detalle cronológico de la cuenta
# ────────────────────────────────────────────────────────────────────────────

def _expandir_facturas(
    fac: pd.DataFrame,
    hoy: pd.Timestamp,
    clientes_fin_de_mes: frozenset[int] | set[int] | None,
    cuotas_idx: dict[int, pd.DataFrame] | None,
) -> pd.DataFrame:
    """Una fila por factura, o una por cuota si la factura fue dividida.

    Es la base común del extracto y del resumen: si cada uno armara sus
    propias filas, la deudora podría mostrar una antigüedad en la tabla de
    clientes y otra en el detalle del cliente.

    Columnas: id, id_cliente, razon_social, numero, cond_venta, emision,
    vencimiento, total, saldo, cuota.
    """
    cols = ["id", "id_cliente", "razon_social", "numero", "cond_venta",
            "emision", "vencimiento", "total", "saldo", "cuota"]
    if fac.empty:
        # Los dtypes NO son cosmética: un cliente que solo tiene notas de
        # crédito deja esto vacío, y una columna de fechas vacía queda como
        # `object`. Restarle un Timestamp tira "Can only use .dt accessor
        # with datetimelike values" lejos de acá (errors.md 2026-08-13).
        vacio = pd.DataFrame(columns=cols)
        for c in ("emision", "vencimiento"):
            vacio[c] = pd.to_datetime(vacio[c], errors="coerce")
        for c in ("total", "saldo"):
            vacio[c] = pd.to_numeric(vacio[c], errors="coerce")
        return vacio

    base = fac.copy()
    base["plazo"] = base["cond_venta"].map(credito.plazo_pactado)
    base["vencimiento"] = credito.fecha_vencimiento(
        base["emision"], base["plazo"], base.get("id_cliente"),
        clientes_fin_de_mes=clientes_fin_de_mes,
    )
    base["cuota"] = ""
    base["total"] = base["total"].abs()

    if not cuotas_idx:
        return base[cols]

    # Las facturas divididas se reemplazan por sus cuotas. El saldo del ERP
    # se reparte de la más vieja a la más nueva (ver `cuotas.estado`), así
    # que la deuda del cliente no cambia por dividir una factura.
    con_cuotas = base[base["id"].isin(cuotas_idx)]
    if con_cuotas.empty:
        return base[cols]

    filas = []
    for _, f in con_cuotas.iterrows():
        est = CU.estado(cuotas_idx[int(f["id"])], f["saldo"])
        n = len(est)
        for _, c in est.iterrows():
            filas.append({
                "id": f["id"],
                "id_cliente": f["id_cliente"],
                "razon_social": f["razon_social"],
                "numero": f["numero"],
                "cond_venta": f["cond_venta"],
                "emision": f["emision"],
                "vencimiento": c["vencimiento"],
                "total": c["importe"],
                "saldo": c["pendiente"],
                "cuota": f"{int(c['nro_cuota'])}/{n}",
            })

    enteras = base[~base["id"].isin(cuotas_idx)][cols]
    return pd.concat([enteras, pd.DataFrame(filas, columns=cols)],
                     ignore_index=True)


def armar_movimientos(
    df_comp: pd.DataFrame,
    df_imp: pd.DataFrame | None = None,
    hoy: pd.Timestamp | None = None,
    clientes_fin_de_mes: frozenset[int] | set[int] | None = None,
    cuotas_idx: dict[int, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Una fila por movimiento de la cuenta, ordenada por cliente y fecha.

    Args:
      df_comp: comprobantes de `credito_api.parse_comprobantes`.
      df_imp: imputaciones de recibos de `credito_api.parse_cobranzas`.
        Opcional: sin esto la deudora igual muestra facturas y notas de
        crédito, solo que sin los pagos intercalados.
      hoy: fecha de corte. Default: hoy.
      clientes_fin_de_mes: ids que cuentan el plazo desde el cierre del mes.
        Default: `credito.VENCE_FIN_DE_MES`.

    Columnas: id_cliente, razon_social, fecha, tipo_mov, comprobante,
    cond_pago, vencimiento, dias_vencido, debe, haber, saldo_pendiente,
    id_comprobante.

    `debe` y `haber` van siempre positivos (es un extracto, no un balance):
    las facturas cargan en `debe`, las notas de crédito y los recibos en
    `haber`. `saldo_pendiente` solo tiene valor en las facturas — es lo que
    queda por cobrar de ESA factura, según el ERP.

    `dias_vencido` solo se calcula en facturas con saldo: una factura ya
    cobrada no tiene atraso corriente, y mezclarlas infla el promedio.
    """
    hoy = pd.Timestamp(hoy or pd.Timestamp.today().normalize())

    if df_comp is None or df_comp.empty or "tipo" not in df_comp:
        return pd.DataFrame(columns=COLS_MOV)

    comp = df_comp.copy()
    comp["emision"] = pd.to_datetime(comp["emision"], errors="coerce")
    es_nc = comp["tipo"].isin(credito.TIPOS_NOTA_CREDITO)

    # --- Facturas (una fila por factura, o una por cuota si fue dividida) ---
    fac = _expandir_facturas(comp[~es_nc], hoy, clientes_fin_de_mes, cuotas_idx)
    if not fac.empty:
        abierta = fac["saldo"] > credito.TOL_SALDO
        fac_mov = pd.DataFrame({
            "id_cliente": fac["id_cliente"],
            "razon_social": fac["razon_social"],
            "fecha": fac["emision"],
            "tipo_mov": TIPO_FACTURA,
            "comprobante": fac["numero"],
            "cuota": fac["cuota"],
            "cond_pago": fac["cond_venta"],
            "vencimiento": fac["vencimiento"],
            "dias_vencido": ((hoy - fac["vencimiento"]).dt.days)
                            .where(abierta & (fac["vencimiento"] < hoy)),
            "debe": fac["total"],
            "haber": 0.0,
            "saldo_pendiente": fac["saldo"].where(abierta, 0.0),
            "id_comprobante": fac["id"],
        })
    else:
        fac_mov = pd.DataFrame(columns=COLS_MOV)

    # --- Notas de crédito ---------------------------------------------------
    # `total` (ImporteTotalNeto) viene negativo en las NC, pero no siempre:
    # ver errors.md 2026-08-13. Se normaliza con abs() fila por fila, nunca
    # sobre la suma — una NC de signo invertido cancelaría a las otras.
    nc = comp[es_nc].copy()
    if not nc.empty:
        nc_mov = pd.DataFrame({
            "id_cliente": nc["id_cliente"],
            "razon_social": nc["razon_social"],
            "fecha": nc["emision"],
            "tipo_mov": TIPO_NOTA_CREDITO,
            "comprobante": nc["numero"],
            "cuota": "",
            "cond_pago": "",
            "vencimiento": pd.NaT,
            "dias_vencido": pd.NA,
            "debe": 0.0,
            "haber": nc["total"].abs(),
            "saldo_pendiente": pd.NA,
            "id_comprobante": nc["id"],
        })
    else:
        nc_mov = pd.DataFrame(columns=COLS_MOV)

    # --- Recibos ------------------------------------------------------------
    rec_mov = _movimientos_de_recibos(df_imp, comp)

    partes = [p for p in (fac_mov, nc_mov, rec_mov) if not p.empty]
    if not partes:
        return pd.DataFrame(columns=COLS_MOV)

    mov = pd.concat(partes, ignore_index=True)
    mov["fecha"] = pd.to_datetime(mov["fecha"], errors="coerce")
    # Dentro del mismo día primero la factura y después lo que la cancela:
    # leído de arriba abajo, el extracto cuenta la historia en orden.
    orden = {TIPO_FACTURA: 0, TIPO_NOTA_CREDITO: 1, TIPO_RECIBO: 2}
    mov["_ord"] = mov["tipo_mov"].map(orden).fillna(9)
    # Las cuotas de una misma factura comparten fecha de emisión, así que se
    # desempatan por vencimiento: 1/3, 2/3, 3/3 en ese orden.
    mov = (mov.sort_values(["id_cliente", "fecha", "_ord", "comprobante",
                            "vencimiento"])
              .drop(columns="_ord")
              .reset_index(drop=True))
    return mov[COLS_MOV]


def _movimientos_de_recibos(
    df_imp: pd.DataFrame | None, comp: pd.DataFrame
) -> pd.DataFrame:
    """Imputaciones de recibos → filas de movimiento.

    Un recibo puede imputarse a varias facturas: cada imputación es una
    fila, porque en la deudora interesa contra qué factura fue cada peso.
    Las imputaciones con `id_comprobante == 0` son pagos a cuenta (plata que
    entró sin factura asignada) y se muestran como tales.
    """
    if df_imp is None or df_imp.empty or "id_comprobante" not in df_imp:
        return pd.DataFrame(columns=COLS_MOV)

    imp = df_imp.copy()
    imp["fecha_pago"] = pd.to_datetime(imp["fecha_pago"], errors="coerce")
    imp["id_comprobante"] = pd.to_numeric(
        imp["id_comprobante"], errors="coerce").fillna(0).astype("int64")

    # La razón social viaja en el comprobante, no en el recibo. Para los
    # pagos a cuenta no hay comprobante que consultar, así que se completa
    # con el nombre que tenga el cliente en cualquier otra fila suya.
    nombres = (comp.dropna(subset=["id_cliente"])
                   .groupby("id_cliente")["razon_social"].first())
    numeros = comp.set_index("id")["numero"].to_dict()

    a_cuenta = imp["id_comprobante"] == 0
    detalle = imp["id_comprobante"].map(numeros)
    return pd.DataFrame({
        "id_cliente": imp["id_cliente"],
        "razon_social": imp["id_cliente"].map(nombres).fillna(""),
        "fecha": imp["fecha_pago"],
        "tipo_mov": TIPO_RECIBO,
        "comprobante": detalle.where(~a_cuenta, "Pago a cuenta").fillna(
            "Pago (factura fuera del período)"),
        "cuota": "",
        "cond_pago": "",
        "vencimiento": pd.NaT,
        "dias_vencido": pd.NA,
        "debe": 0.0,
        "haber": pd.to_numeric(imp["importe"], errors="coerce").fillna(0.0).abs(),
        "saldo_pendiente": pd.NA,
        "id_comprobante": imp["id_comprobante"],
    })


# ────────────────────────────────────────────────────────────────────────────
# Resumen por cliente
# ────────────────────────────────────────────────────────────────────────────

def _tramo(dias: float) -> str:
    """Días de atraso → clave del tramo. Sin atraso o sin dato → al día."""
    if pd.isna(dias) or dias <= 0:
        return "al_dia"
    for clave, _, desde, hasta in BUCKETS[1:]:
        if (desde is None or dias >= desde) and (hasta is None or dias <= hasta):
            return clave
    return "b_90_mas"


def resumen_por_cliente(
    df_comp: pd.DataFrame,
    df_imp: pd.DataFrame | None = None,
    df_cli: pd.DataFrame | None = None,
    hoy: pd.Timestamp | None = None,
    clientes_fin_de_mes: frozenset[int] | set[int] | None = None,
    cuotas_idx: dict[int, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Una fila por cliente con deuda viva, con su antigüedad y contacto.

    La deuda sale del `saldo` de cada comprobante (ver el encabezado del
    módulo). `deuda_total` es lo que el cliente debe hoy; `credito_a_favor`
    es el saldo de notas de crédito que todavía no se aplicó a ninguna
    factura, y va **restado** de la deuda: es plata que el cliente ya no
    debe.

    Los clientes cuyo neto queda en cero o a favor igual aparecen, con
    `deuda_total` <= 0. Esconderlos haría que un cliente con crédito
    pendiente desaparezca de la vista del vendedor justo cuando conviene
    que lo use.
    """
    hoy = pd.Timestamp(hoy or pd.Timestamp.today().normalize())

    if df_comp is None or df_comp.empty or "tipo" not in df_comp:
        return pd.DataFrame(columns=COLS_RESUMEN)

    comp = df_comp.copy()
    comp["emision"] = pd.to_datetime(comp["emision"], errors="coerce")
    comp["saldo"] = pd.to_numeric(comp["saldo"], errors="coerce").fillna(0.0)
    es_nc = comp["tipo"].isin(credito.TIPOS_NOTA_CREDITO)

    # --- Facturas abiertas, por tramo de antigüedad -------------------------
    # Misma expansión que el extracto: si una factura está dividida en
    # cuotas, cada cuota cae en el tramo que le corresponde por su propio
    # vencimiento. `facturas_abiertas` cuenta cuotas, no comprobantes.
    fac = _expandir_facturas(comp[~es_nc], hoy, clientes_fin_de_mes, cuotas_idx)
    abiertas = fac[fac["saldo"] > credito.TOL_SALDO].copy()
    abiertas["dias"] = (hoy - abiertas["vencimiento"]).dt.days
    abiertas["tramo"] = abiertas["dias"].map(_tramo)

    if abiertas.empty:
        base = pd.DataFrame(columns=[
            "id_cliente", "razon_social", "facturas_abiertas", "bruto",
            "vencido", "dias_mas_vieja",
        ])
    else:
        base = abiertas.groupby("id_cliente", as_index=False).agg(
            razon_social=("razon_social", "first"),
            facturas_abiertas=("id", "size"),
            bruto=("saldo", "sum"),
            dias_mas_vieja=("dias", "max"),
        )
        vencido = (abiertas[abiertas["dias"] > 0]
                   .groupby("id_cliente")["saldo"].sum()
                   .rename("vencido").reset_index())
        base = base.merge(vencido, on="id_cliente", how="left")
        base["vencido"] = base["vencido"].fillna(0.0)

        pivot = (abiertas.pivot_table(index="id_cliente", columns="tramo",
                                      values="saldo", aggfunc="sum",
                                      fill_value=0.0)
                 .reset_index())
        base = base.merge(pivot, on="id_cliente", how="left")

    for col in COLS_BUCKET:
        if col not in base.columns:
            base[col] = 0.0
        base[col] = pd.to_numeric(base[col], errors="coerce").fillna(0.0)

    # --- Crédito a favor: NC con saldo sin aplicar --------------------------
    # `Saldo` de una NC viene POSITIVO (magnitud), salvo excepciones. Se
    # normaliza fila por fila, nunca sobre la suma (errors.md 2026-08-13).
    nc = comp[es_nc]
    nc_abiertas = nc[nc["saldo"].abs() > credito.TOL_SALDO]
    if nc_abiertas.empty:
        favor = pd.DataFrame(columns=["id_cliente", "credito_a_favor"])
    else:
        favor = (nc_abiertas.assign(m=nc_abiertas["saldo"].abs())
                 .groupby("id_cliente")["m"].sum()
                 .rename("credito_a_favor").reset_index())

    # Los tipos tienen que coincidir o el merge no cruza nada y los clientes
    # se duplican en silencio: uno con deuda y otro con el crédito.
    base["id_cliente"] = pd.to_numeric(base["id_cliente"], errors="coerce")
    favor["id_cliente"] = pd.to_numeric(favor["id_cliente"], errors="coerce")
    base = base.merge(favor, on="id_cliente", how="outer")
    base["credito_a_favor"] = pd.to_numeric(
        base["credito_a_favor"], errors="coerce").fillna(0.0)
    for col in ["bruto", "vencido", *COLS_BUCKET]:
        base[col] = pd.to_numeric(base.get(col), errors="coerce").fillna(0.0)
    base["facturas_abiertas"] = pd.to_numeric(
        base.get("facturas_abiertas"), errors="coerce").fillna(0).astype(int)

    base["deuda_total"] = base["bruto"] - base["credito_a_favor"]
    # `vencido` es BRUTO: la suma de las facturas pasadas de fecha, sin tocar
    # el crédito a favor. Se deja así porque es el número comparable con el
    # scoring y con el tablero. Pero un cliente con notas de crédito sin
    # aplicar puede quedar con `vencido` MAYOR que `deuda_total`, que leído
    # en una tabla no tiene sentido. `vencido_neto` es lo que de verdad hay
    # que ir a cobrar: el crédito pendiente cancela primero lo más viejo,
    # que es la convención de cualquier cuenta corriente.
    base["vencido_neto"] = (base["vencido"] - base["credito_a_favor"]).clip(
        lower=0.0)
    base["peor_tramo"] = base["dias_mas_vieja"].map(_tramo)
    base = base.drop(columns=["bruto"])

    # --- Última compra y último pago ---------------------------------------
    if not fac.empty:
        ult_compra = (fac.groupby("id_cliente")["emision"].max()
                      .rename("ultima_compra").reset_index())
        base = base.merge(ult_compra, on="id_cliente", how="left")
    else:
        base["ultima_compra"] = pd.NaT

    if df_imp is not None and not df_imp.empty and "fecha_pago" in df_imp:
        pagos = df_imp.copy()
        pagos["fecha_pago"] = pd.to_datetime(pagos["fecha_pago"], errors="coerce")
        pagos["id_cliente"] = pd.to_numeric(pagos["id_cliente"], errors="coerce")
        ult_pago = (pagos.groupby("id_cliente")["fecha_pago"].max()
                    .rename("ultimo_pago").reset_index())
        base = base.merge(ult_pago, on="id_cliente", how="left")
    else:
        base["ultimo_pago"] = pd.NaT

    # --- Datos del cliente --------------------------------------------------
    base = _pegar_ficha(base, df_cli, comp)

    for col in COLS_RESUMEN:
        if col not in base.columns:
            base[col] = pd.NA
    return (base[COLS_RESUMEN]
            .sort_values("deuda_total", ascending=False)
            .reset_index(drop=True))


def _pegar_ficha(
    base: pd.DataFrame, df_cli: pd.DataFrame | None, comp: pd.DataFrame
) -> pd.DataFrame:
    """Agrega razón social, documento, contacto y vendedor.

    El vendedor sale del maestro de clientes (`IdUsuarioAdicional`), que es
    la asignación real de cartera. El comprobante también trae un vendedor,
    pero es el que facturó esa venta puntual: para una deudora interesa de
    quién es el cliente, no quién le hizo la última factura.
    """
    if "razon_social" not in base.columns:
        base["razon_social"] = pd.NA
    nombres = (comp.dropna(subset=["id_cliente"])
                   .groupby("id_cliente")["razon_social"].first())
    base["razon_social"] = base["razon_social"].fillna(
        base["id_cliente"].map(nombres))

    if df_cli is None or df_cli.empty or "id_cliente" not in df_cli:
        for col in ["documento", "ciudad", "telefono", "email"]:
            base[col] = ""
        base["vendedor"] = SIN_VENDEDOR
        return base

    cli = df_cli.drop_duplicates(subset="id_cliente").copy()
    # El documento va SIEMPRE como string: un RUT con ceros a la izquierda
    # se destroza como entero (regla del proyecto, claude.md.txt).
    cli["documento"] = cli["documento"].astype(str).str.strip()
    cols = [c for c in ["id_cliente", "documento", "ciudad", "telefono",
                        "email", "vendedor", "id_vendedor"]
            if c in cli.columns]
    base = base.merge(cli[cols], on="id_cliente", how="left")

    if "vendedor" not in base.columns:
        base["vendedor"] = pd.NA
    base["vendedor"] = (base["vendedor"].astype("object")
                        .where(base["vendedor"].notna() & (base["vendedor"] != ""),
                               SIN_VENDEDOR))
    for col in ["documento", "ciudad", "telefono", "email"]:
        if col not in base.columns:
            base[col] = ""
        base[col] = base[col].fillna("")
    return base


# ────────────────────────────────────────────────────────────────────────────
# Vendedor
# ────────────────────────────────────────────────────────────────────────────

def agregar_vendedor(
    df_cli: pd.DataFrame,
    vendedores_map: dict[int, str],
    excluidos: frozenset[int] | set[int] | None = None,
) -> pd.DataFrame:
    """Agrega la columna `vendedor` al maestro de clientes.

    Contabilium no expone un maestro de vendedores, así que el nombre sale
    del mapping manual de `vendedores.py`. Un id que no esté mapeado NO se
    descarta: queda como `ID_<n>`, que es visible y obliga a mapearlo, en
    vez de desaparecer del informe.

    `excluidos` son las cuentas operativas (Jesica, Valeria): sus clientes
    pasan a `SIN_VENDEDOR` porque no son cartera de nadie.
    """
    if df_cli is None or df_cli.empty:
        return df_cli
    out = df_cli.copy()
    if "id_vendedor" not in out.columns:
        out["vendedor"] = SIN_VENDEDOR
        return out

    ids = pd.to_numeric(out["id_vendedor"], errors="coerce")
    excl = set(excluidos or ())

    def _nombre(v):
        if pd.isna(v):
            return SIN_VENDEDOR
        v = int(v)
        if v in excl or v == 0:
            return SIN_VENDEDOR
        return vendedores_map.get(v, f"ID_{v}")

    out["vendedor"] = ids.map(_nombre)
    return out


def totales_por_vendedor(resumen: pd.DataFrame) -> pd.DataFrame:
    """Una fila por vendedor: cartera con deuda, total, vencido y tramos."""
    cols = ["vendedor", "clientes", "deuda_total", "vencido", "vencido_neto",
            "%_vencido", *COLS_BUCKET]
    if resumen is None or resumen.empty:
        return pd.DataFrame(columns=cols)

    g = resumen.groupby("vendedor", as_index=False).agg(
        clientes=("id_cliente", "nunique"),
        deuda_total=("deuda_total", "sum"),
        vencido=("vencido", "sum"),
        vencido_neto=("vencido_neto", "sum"),
        **{c: (c, "sum") for c in COLS_BUCKET},
    )
    # Sobre el total de la cartera del vendedor, no sobre el bruto: si un
    # cliente tiene crédito a favor, ese crédito no está vencido.
    g["%_vencido"] = (g["vencido"] / g["deuda_total"].where(
        g["deuda_total"] > 0)) * 100
    g["%_vencido"] = g["%_vencido"].fillna(0.0)
    return g[cols].sort_values("vencido", ascending=False).reset_index(drop=True)


def extracto_de_cliente(
    mov: pd.DataFrame, id_cliente: int, solo_abiertas: bool = False
) -> pd.DataFrame:
    """Movimientos de un cliente, listos para mostrar.

    Con `solo_abiertas=True` deja únicamente las facturas que todavía tienen
    saldo — es la vista que sirve para ir a cobrar. Sin eso muestra la
    cuenta completa, que es la que sirve cuando el cliente discute.
    """
    if mov is None or mov.empty:
        return pd.DataFrame(columns=COLS_MOV)
    out = mov[mov["id_cliente"] == id_cliente].copy()
    if solo_abiertas:
        saldo = pd.to_numeric(out["saldo_pendiente"], errors="coerce").fillna(0)
        out = out[(out["tipo_mov"] == TIPO_FACTURA) & (saldo > credito.TOL_SALDO)]
    return out.reset_index(drop=True)
