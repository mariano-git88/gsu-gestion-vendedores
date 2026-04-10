# Decisiones — Gestión de Vendedores GSU

Append-only. Cada entrada documenta un criterio acordado, su contexto y
las alternativas descartadas.

---

## 2026-04-10 — Excluir vendedores OPJESICA y OPVALERIA del dashboard

**Decisión:** las filas de facturación cuyo `vendedor` es uno de los
siguientes NO se contabilizan en NINGUNA métrica del dashboard:

- `OPJESICA@SUPRABOND.COM.UY`
- `OPVALERIA@SUPRABOND.COM.UY`

**Contexto:** son cuentas operativas/administrativas de Suprabond, no
representantes comerciales. Sus operaciones aparecen en la facturación
de Contabilium pero no deben contar para venta, cobertura ni ningún
cálculo de performance del equipo comercial. Es la misma regla que se
aplica en el proyecto "Liquidación de Comisiones GSU" — ahí están
documentadas las dos cuentas con la misma justificación.

**Implementación:**

- Constante `VENDEDORES_OP_EXCLUIDOS` definida en `transforms.py`.
- Función `exclude_op_vendedores(df_fc)` que separa las filas
  conservadas de las excluidas.
- Es el **PRIMER paso** del orquestador `prepare_facturacion()`, antes
  incluso del filtrado de NCF, así el resto del pipeline no procesa
  filas que igual van a descartarse.
- El check `check_vendedores_sin_cartera()` opera sobre el DataFrame
  ya post-exclusión (snapshot `df_post_op` dentro del orquestador), de
  modo que estos dos vendedores **no aparecen flagueados como
  huérfanos** en el panel de salud.
- Se reportan las filas excluidas en el panel de salud como
  **info** (no warning, no error) para trazabilidad: el usuario ve
  cuántas filas se removieron y de qué cuentas.

**Match:** se hace por **igualdad exacta** de string (case-sensitive,
con dominio incluido). Si un día el ERP exporta los emails en otra
capitalización o con un dominio distinto, el filtro no va a matchear y
hay que actualizar la lista en `transforms.py`.

**Alternativas descartadas:**

- Match case-insensitive con `.upper()`: descartado por consistencia con
  el proyecto de Liquidación de Comisiones, donde tampoco se hace.
- Excluirlos solo en `metrics.py` y dejar que pasen por el resto del
  pipeline: descartado porque (a) ensucia el panel de salud con
  warnings irrelevantes (vendedores sin cartera, etc.), (b) consume
  procesamiento sin fin, y (c) si en el futuro se agrega una métrica
  nueva, hay que acordarse de excluirlos ahí también.
- Filtrarlos en `data_loader.py`: descartado porque el data loader
  tiene que ser agnóstico a las reglas de negocio. Su responsabilidad
  es leer y validar estructura, no decidir qué filas contar.

**Confirmado por:** Mariano, sesión 2026-04-10.

---

## 2026-04-10 — Rename `Producto` → `producto` para fc

**Decisión:** la columna `Producto` (nombre del producto, para display)
de las planillas de facturación se renombra internamente a `producto`
(snake_case directo).

**Contexto:** durante la implementación de `data_loader.load_fc()`
(Tanda 2 de esta sesión) detecté que el manual lista la columna `Producto`
como relevante en las planillas `fc_semanal.xlsx` y `fc_mensual.xlsx`,
pero **no la incluye** en el mapping de "Rename a nombres internos" del
mismo manual. Es una omisión del manual original, no una contradicción.

**Implementación:**
- En `data_loader.RENAME_FC`, agregada la entrada `"Producto": "producto"`.
- La columna queda preservada en todo el pipeline post-rename.
- En `transforms.classify_skus()`, se usa como **fallback de display**
  cuando un SKU cae en la categoría `SIN ASIGNAR`: el nombre canónico
  `nombre` se rellena con el `producto` original de fc para que la UI
  tenga algo que mostrar.

**Alternativas descartadas:**
- Pisar la columna `producto` con el `nombre` canónico de productos/combos
  después de la clasificación: descartado para preservar la columna
  original como auditoría/debugging si algún SKU clasifica raro.
- Llamarla `producto_nombre` o `descripcion`: descartado por simplicidad
  y porque "producto" es el nombre más natural en castellano.

**Confirmado por:** Mariano, sesión 2026-04-10 (durante Tanda 2).

---

## 2026-04-10 — Join clientes ↔ facturación: solo se trae `razon_social`

**Decisión:** al hacer el left join entre facturación y clientes en
`transforms.join_facturacion_clientes()`, del DataFrame de clientes
**solo se trae la columna `razon_social`**. El `vendedor` (que en
clientes representa al vendedor asignado) **NO se trae** al merge.

**Contexto:** ambos DataFrames tienen una columna `vendedor`:
- En facturación, `vendedor` es el vendedor de la operación (quien hizo
  esa venta específica).
- En clientes, `vendedor` es el vendedor asignado al cliente en cartera.

Si trajéramos ambas, hay conflicto de nombres y semánticamente es
distinto. En cada fila de facturación, lo importante operativamente es
**quién hizo la venta**, no a quién pertenece el cliente en cartera.

**Implementación:**
- En `join_facturacion_clientes()`, el subset que se trae del df_clientes
  es solo `df_clientes[["documento", "razon_social"]]`.
- La asignación cliente → vendedor (cartera) se evalúa **a posteriori** en
  `metrics.py` cuando se calcula cobertura, mediante un merge dedicado
  por `(vendedor, documento)`.

**Alternativas descartadas:**
- Renombrar el `vendedor` de clientes a `vendedor_asignado` antes del
  merge: descartado porque agrega ruido al esquema final y porque la
  asignación cliente→vendedor solo se necesita en cobertura, no en
  cada fila de facturación.

**Confirmado por:** Mariano, sesión 2026-04-10 (durante Tanda 3).

---

## 2026-04-10 — Match estricto en cobertura: `(vendedor_op, documento)`

**Decisión:** todas las métricas de cobertura
(`cobertura_por_vendedor`, `cobertura_por_sub_rubro`, `cobertura_por_sku`)
hacen el matching entre facturación y cartera por la **tupla
`(vendedor, documento)`**, no por `documento` solo.

**Significado operativo:** un cliente solo cuenta como "cubierto" por
un vendedor V si **el mismo V le hizo la venta**. Si el cliente C1 está
asignado a V1 en cartera pero V2 le facturó, esa venta NO cuenta como
cobertura para nadie:
- Para V1: C1 sigue contando como "asignado pero no comprado" (V1 no le
  vendió).
- Para V2: C1 no cuenta como "asignado" (no está en su cartera).

**Implementación:**
- Helper `_fac_en_cartera_propia()` en `metrics.py` que hace el merge
  por `(vendedor, documento)` con `how="inner"`. Es la base de las tres
  funciones de cobertura.

**Por qué importa:** sin el match estricto, una venta cruzada (vendedor
A le vende a un cliente de B) inflaría artificialmente las métricas de
cobertura. La cobertura mide "qué porcentaje de mi cartera realmente
recibió mi atención comercial", no "qué porcentaje de mi cartera compró
en general".

**Alternativas descartadas:**
- Match laxo por `documento` solo: descartado porque pierde la señal
  de a qué vendedor "le toca" cada cliente. Confunde performance comercial
  con actividad de venta general.

**Confirmado por:** Mariano, sesión 2026-04-10 (durante Tanda 4).

---

## 2026-04-10 — `clientes_sin_compra_sku` con match estricto

**Decisión:** la función `metrics.clientes_sin_compra_sku()` (que alimenta
la sección "Clientes que NO compraron este SKU en el mes" de la vista
Cobertura) usa el **mismo match estricto** que el resto de las funciones
de cobertura.

**Significado:** un cliente aparece en la lista de "no compradores" si
su **vendedor asignado** no le vendió ese SKU con FAC en lo que va del
mes. **Aunque otro vendedor distinto le haya vendido el mismo SKU**, el
cliente igual aparece — porque la oportunidad de venta para el vendedor
asignado sigue abierta.

**Contexto:** Mariano lo confirmó explícitamente en la sesión cuando le
ofrecí elegir entre estricto y laxo. La razón es **consistencia** con las
funciones de cobertura existentes (decisión anterior en este mismo
archivo): si la cobertura por SKU dice "V1 cubre 60% del SKU X", la
lista de no-compradores tiene que dar exactamente el 40% restante.

**Edge case:** la sección siempre opera sobre `df_mes`, independientemente
del selector de Semana/Mes en la vista. Si el usuario selecciona un SKU
que solo aparece en la semana (no en el mes), la vista muestra un
mensaje informativo en lugar de una lista vacía o todos los clientes.

**Confirmado por:** Mariano, sesión 2026-04-10.
