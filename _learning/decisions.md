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

---

## 2026-04-10 — Tab "Análisis profundo" para visualizaciones estratégicas

**Decisión:** se agrega una **5ta tab "Análisis"** entre Cobertura y
Salud, con tres bloques de exploración estratégica:

1. **Penetración por sub-rubro** — matriz pivot vendedor × sub_rubro
   con % de cobertura, coloreada en una escala roja → amarilla → verde.
2. **Heatmap cliente × sub-rubro** — para un vendedor específico, los
   top N clientes en filas y los sub-rubros en columnas, con monto en
   cada celda y escala de grises según el monto.
3. **Pareto de clientes** — distribución 80/20 con marcador del CORE 80%.

**Contexto:** Mariano pidió evolucionar el dashboard para que el Jefe
de Ventas salga de la reunión semanal con **acciones concretas para
vender más**, no solo con números para mirar. Las tres visualizaciones
identifican oportunidades específicas: huecos de cross-sell (penetración
y heatmap) y clientes a blindar (Pareto).

**Por qué una tab nueva** (en lugar de extender las existentes):

- Las tres son **exploración estratégica**, no del uso diario. Tener
  una tab dedicada las hace fáciles de encontrar pero las separa del
  flujo cotidiano (Resumen / Sub-rubro / Cobertura).
- Permite agregar más visualizaciones de análisis profundo en el futuro
  sin saturar las tabs habituales.

**Decisiones de diseño dentro de la tab:**

- **Selector de período común** a las 3 secciones (Mes / Semana, default
  Mes). La penetración semanal tiende a ser baja para todos y aporta
  poca señal, por eso default Mes.

- **Heatmap por sub_rubro, no por SKU**: ~10–20 columnas vs cientos.
  Da panorama estratégico legible. Si en el futuro se quiere ver SKU
  específico, ya existe la sección "Cobertura por SKU" en la tab anterior.

- **Heatmap top N = 30 clientes por default** (rango 5–100). Filtrar
  por vendedor obligatorio. Sin esto, mostrar 986 clientes × N sub-rubros
  es ilegible. 30 es un balance entre "ver lo importante" y "no saturar
  la pantalla".

- **Heatmap con escala de grises monocromática** (no colorida) para
  encajar con el theme Dieter Rams.

- **Penetración con escala rojo→amarillo→verde** suave (no saturada).
  Aquí sí se justifica el color porque el rojo es semánticamente "alerta"
  — los huecos de cross-sell son un llamado a la acción.

- **Pareto incluye al menos el primer cliente** aunque ya supere el
  80% por sí solo. Sin esa salvaguarda, vendedores con cartera muy
  concentrada en pocos clientes podrían recibir un Pareto vacío.

- **Pareto con selector "Todos los vendedores" o vendedor específico**.
  En modo "Todos" aparece la columna `vendedor` para identificar quién
  atiende cada cliente del top.

- **Match estricto en las 3 funciones nuevas** (`penetracion_por_sub_rubro_pivot`,
  `heatmap_cliente_sub_rubro`, `pareto_clientes`), consistente con el
  resto de las funciones de cobertura — ver entrada del 2026-04-10
  sobre "Match estricto en cobertura".

**Confirmado por:** Mariano, sesión 2026-04-10.

---

## 2026-04-10 — Export de agenda personal por vendedor (Excel, 5 hojas)

**Decisión:** se agrega un bloque **"Exportar agenda"** en la sidebar
del app (debajo de los uploaders, después del procesamiento de datos)
con un **selector de vendedor** y un **botón de descarga** que genera
un archivo `.xlsx` con la agenda personal de ese vendedor.

**Contexto:** complementa la tab "Análisis" para que el resultado de la
reunión sea **tangible**. El vendedor sale con un Excel en mano que
contiene todo lo que tiene que hacer en la semana — no solo "datos
en pantalla que ya no recuerda al volver al auto".

**Estructura del .xlsx (5 hojas):**

| Hoja | Contenido |
|---|---|
| 1. Resumen | Performance del período (mes y semana) + cobertura general + comparativa vs promedio del equipo |
| 2. Mi cartera | Listado completo: documento, razón social, monto mes, monto semana, unidades, ¿compró este mes? Ordenado: los que compraron arriba (por monto desc), los dormidos abajo |
| 3. Clientes dormidos | Solo los que no compraron este mes, ordenados por razón social |
| 4. Penetración | La fila propia del vendedor de la matriz de penetración por sub-rubro, ordenada descendente |
| 5. Top 80% | Los clientes que conforman el CORE 80% del Pareto del vendedor |

**Decisiones de implementación:**

- **Excel, no PDF**. Razones:
  - Implementación trivial con `openpyxl` (ya tenemos esa dependencia).
  - Datos manipulables: el vendedor puede ordenar/filtrar a su gusto.
  - PDF requeriría `reportlab` o `weasyprint`, formato fijo, mucho más
    esfuerzo. Si en el futuro lo piden, lo agregamos como segundo botón
    sin tocar el primero.

- **Un solo selector + un solo botón** (no descarga masiva en ZIP).
  Razón: simplicidad operativa. Si el Jefe necesita las agendas de
  todo el equipo, las descarga una por una. Para 5–10 vendedores no
  vale el esfuerzo de implementar el ZIP.

- **El bloque vive DESPUÉS del procesamiento de datos** (no junto a los
  uploaders), porque necesita `df_clientes`, `df_sem` y `df_mes` ya
  cargados. Si el usuario todavía no subió las planillas, simplemente
  no aparece — sin riesgo de "click sin datos".

- **Cacheado con `@st.cache_data`** por (df_sem, df_mes, df_clientes,
  vendedor). Si el usuario cambia de vendedor varias veces, solo
  regenera para el vendedor que cambia.

- **Match estricto en todas las hojas**, igual que el resto del dashboard.
  Las ventas cruzadas no aparecen en la agenda de ningún vendedor.

- **Stylo consistente con el theme Dieter Rams**: headers negros con
  texto blanco, bordes finísimos grises, sin sombras, formato de moneda
  `$#,##0`, porcentajes `0.0%`.

- **Top 80% incluye al menos 1 cliente** (misma salvaguarda que en la
  tab de Análisis).

**Módulo nuevo:** `exports.py` (separado de `metrics.py` para no mezclar
"cálculo de datos" con "generación de archivos"). Función pública:
`exportar_agenda_vendedor(df_sem, df_mes, df_clientes, vendedor) -> BytesIO`.

**Confirmado por:** Mariano, sesión 2026-04-10.

---

## 2026-04-17 — Integración con API de Contabilium: coexistencia con "Modo Manual Secundario"

**Decisión:** el dashboard va a evolucionar a cargar los datos directamente
desde la **API REST de Contabilium** (`https://rest.contabilium.com`) en
lugar de depender exclusivamente del upload de las 5 planillas xlsx.

**Modelo operativo:** **coexistencia**, no reemplazo.

- **Modo primario (default):** carga desde la API. Selector de mes en la
  sidebar principal + botón "Sincronizar desde Contabilium". El usuario
  abre la app y sincroniza sin tocar archivos.
- **Modo secundario (fallback):** upload manual de las 5 planillas. Vive
  en una **sección aparte** de la sidebar etiquetada explícitamente como
  **"Modo Manual Secundario"** (o un expander colapsado con ese nombre).
  La intención es que quede disponible pero visualmente degradado,
  señalando que es el plan B.

**Por qué coexistencia y no reemplazo:**

- Si la API de Contabilium está caída o cambia un campo silenciosamente
  un viernes, Mariano / el Jefe de Ventas tienen que poder exportar las
  planillas desde Contabilium y seguir la reunión del lunes sin
  depender de resolver un bug.
- Hasta que el modo API acumule varios meses sin sorpresas, el modo
  manual es la red de seguridad operacional.
- El upload manual ya existe y funciona — no cuesta mantenerlo, solo
  hay que reubicarlo en la UI.

**Por qué el manual queda visualmente secundario** (y no a la par):

- Si están al mismo nivel, el usuario no sabe cuál usar y cada uno
  arma un hábito distinto.
- Marcarlo como "secundario" comunica implícitamente: "usá API por
  default, vení acá solo si la API falla".

**Alternativas descartadas:**

- **Reemplazo total inmediato** de xlsx por API: descartado por el
  riesgo operativo del primer mes en producción. Un bug sutil de
  integración (tipo FAC/eFC distinto, redondeo de moneda, campo faltante
  en algún cliente) se descubre solo cuando Mariano compara el dashboard
  contra Excel — y si no hay Excel como plan B, el dashboard queda
  inutilizable.
- **Ambos modos al mismo nivel visual**: descartado porque genera
  fricción de decisión cada vez que se abre la app.
- **Sacar el upload manual del proyecto y mantenerlo solo en git history**:
  descartado porque cuesta más restaurarlo en una emergencia que
  dejarlo ahí latente.

**Cuándo reevaluar:** una vez que el modo API haya corrido bien durante
~2 meses (≈8 reuniones semanales sin incidentes), discutir si el modo
manual sigue justificando su espacio en la UI o se mueve a un botón
admin / se elimina del todo.

**Confirmado por:** Mariano, sesión 2026-04-17.

---

## 2026-04-17 — Signo negativo de NCF aplicado manualmente en load_fc_api

**Decisión:** en `api_loader.load_fc_api`, cuando `TipoFc` es una nota
de crédito (`NCF`, `NCT`, `NCE`), el loader **multiplica por −1** los
valores de `unidades` y `monto` de cada item del comprobante.

**Contexto y validación empírica:**

Contabilium UY devuelve el `ImporteTotalBruto` del comprobante con
signo negativo para NCF (ej. `"-826,45"`), pero los `Items` del detalle
traen `Cantidad` y `PrecioUnitario` **siempre positivos**. Nuestra
fórmula canónica de monto por item (`PrecioUnitario × Cantidad ×
(1 − Bonificacion/100)`) da por lo tanto un valor positivo.

Para preservar la paridad con el `fc_mensual.xlsx` actual (donde NCF
vienen con cantidad y monto negativos, ya resueltos desde Contabilium
al exportar), aplicamos el signo nosotros.

**Hipótesis validada** con script
`_exploracion-api-contabilium/verificar_signo_ncf.py` sobre 5 NCF
reales de marzo 2026: en todos los casos,
`ratio = monto_calculado / ImporteTotalBruto_header = -1.0000` exacto.
Sin ambigüedad — los items no tienen signo.

**Implementación:**

- Constante `TIPOS_NEGATIVOS = frozenset({"NCF", "NCT", "NCE"})` en
  `api_loader.py`.
- Las notas de **débito** (`NDF`, `NDT`, `NDE`) NO entran en este set:
  suman como las facturas, con signo positivo.
- El test de equivalencia con el xlsx de marzo 2026 cuadra al centavo
  en FAC, NCF y TIK por separado, confirmando que el signo se aplica
  correctamente.

**Alternativas descartadas:**

- **Dejar que `transforms.py` aplique el signo** post-pull: descartado
  porque rompe el principio "api_loader produce DFs idénticos al
  xlsx". El contrato del pipeline interno espera que las filas NCF
  ya vengan con signo, igual que en el xlsx actual.
- **Preguntar a Contabilium si pueden devolver items con signo**:
  innecesario una vez confirmada la fórmula. Y no tenemos garantía
  de que lo cambien sin romper integraciones de otros clientes.

**Riesgo residual:** si en el futuro Contabilium decide devolver los
items de NCF con signo negativo, nuestro doble `-1` los haría
positivos. Mitigación: el test de equivalencia que vive en
`_exploracion-api-contabilium/comparar_api_vs_xlsx.py` detecta el
problema al instante (los totales por tipo dejarían de cuadrar).

**Confirmado por:** Mariano, sesión 2026-04-17 (tras validación
empírica con 5 NCF de marzo 2026).

---

## 2026-04-17 — Mappings de IDs → valores humanos como archivos del repo

**Decisión:** los mappings
- `IDVendedor → email/nombre` (`vendedores.py`)
- `IdSubrubro → código corto` (`subrubros.py` → `SUBRUBROS`)
- `IdRubro → nombre` (`subrubros.py` → `RUBROS`)

viven como **dicts Python en archivos commiteados al repo**.

**Por qué no vienen de la API:**

Contabilium UY no expone endpoints de maestro de vendedores (probado
con 6 paths candidatos, todos 404). Los endpoints del folder "Common"
de Postman (SubRubros, Rubros, ObtenerInfo) tampoco responden bajo
`/api/common/<Name>` que era el path especulado. Sin URL exacta
confirmada, no podemos pullearlos.

**Por qué en archivos del repo y no en secrets:**

- No son sensibles: los emails de vendedores ya están en todos los
  xlsx que procesa la app; los códigos de sub-rubro son negocio
  público no confidencial.
- Cambian muy pocas veces al año (alta/baja de un comercial, nuevo
  sub-rubro). El flujo de actualización con git es perfectamente OK.
- Tenerlos en código permite versionarlos, hacer PR si hay debate
  sobre un mapping, y validarlos en code review.

**Por qué derivarlos automáticamente:**

En lugar de pedirle a Mariano que complete los dicts a mano (riesgoso
— un error tipográfico y una métrica entera queda mal), el script
`_exploracion-api-contabilium/derivar_mappings.py` **cruza el xlsx
vigente con la API** (por `Numero` de comprobante para vendedores,
por `SKU`/`Codigo` para sub-rubros) y deduce los mappings con ratio
100% de certeza.

La derivación del 2026-04-17 produjo 8 vendedores y 14 subrubros +
10 rubros, todos con ratio de match 1:1 (sin ambigüedad).

**Exclusión OP migra a IDs:**

`VENDEDORES_OP_EXCLUIDOS = frozenset({232, 260})` (IDs de OPJESICA y
OPVALERIA) reemplaza al set de emails histórico. Pero como el mapping
`VENDEDORES` traduce 232→"OPJESICA@..." y 260→"OPVALERIA@...", la
función existente `transforms.exclude_op_vendedores` (que filtra por
email) sigue funcionando sin cambios.

**Cuándo re-derivar:**

- Cuando se incorpora o egresa un vendedor del equipo comercial.
- Cuando se crea un sub-rubro nuevo en Contabilium.
- Si un mes el test de equivalencia `comparar_api_vs_xlsx.py` deja
  de cuadrar y la causa parece ser un ID desmapeado (aparece
  `"ID_<n>"` en las columnas `vendedor` o `sub_rubro`).

**Confirmado por:** Mariano, sesión 2026-04-17.

---

## 2026-04-17 — Concurrencia del N+1 y estrategia de cache del app

**Decisión:** en `api_loader.load_fc_api`, el N+1 de `GetById` sobre
~1000 comprobantes del mes se hace con
`concurrent.futures.ThreadPoolExecutor(max_workers=10)`. En `app.py`
el pull de la API se cachea con `@st.cache_data(ttl=3600)` (1h) y
el token con `@st.cache_resource`.

**Racional del paralelismo:**

- Serial: ~1000 comprobantes × ~200 ms/request ≈ 3-5 minutos.
  Inaceptable para la UX de la reunión semanal.
- Pool de 10 workers: ~60 seg end-to-end (validado 58-78 seg empíricos).
- Más workers (20, 50): marginal improvement pero riesgo de rate
  limit. Contabilium no documenta el límite pero 10 requests
  concurrentes nunca tiró 429 en ~10 corridas del smoke test.
- `asyncio` + `aiohttp`: descartado por complejidad innecesaria. El
  tradeoff "ThreadPoolExecutor con 30 líneas" vs "aiohttp+async
  propagado por todo el módulo" favorece la simplicidad.

**Manejo de errores en el pool:**

Si un GetById individual falla tras los retries de `api_get`, el
comprobante se omite del DataFrame final y se imprime un warning
a stdout. Prioriza "dashboard con 99% de los datos" sobre "sin
dashboard". En una Tanda futura (F) este warning se va a elevar
al panel de salud.

**Racional del cache:**

- **Token** (`@st.cache_resource`): no tiene TTL explícito. El
  `ApiSession` contiene `expires_at` y `api_get` se auto-refresca
  cuando está por vencer. Compartido en el proceso.
- **Pull de maestros + facturación** (`@st.cache_data(ttl=3600)`):
  cache de 1h es un compromiso razonable. Datos del mes en curso
  cambian a diario pero no cada 15 minutos; un resync manual (volver
  a tocar "Sincronizar") invalida el cache si el usuario necesita
  fresco.
- **No se diferencia TTL por mes** (mes en curso vs meses cerrados):
  decisión explícita de simplicidad. TTL único = fácil de razonar
  para Mariano. Si en el futuro hay fricción real, se puede separar.

**Pre-refresco del token antes del pool:**

Al empezar el batch paralelo, `load_fc_api` llama
`_refrescar_si_expirado(session)` explícitamente. Evita el caso
teórico donde varios threads detectan simultáneamente un token
vencido y regeneran cada uno su propio token. Como el TTL del token
es 24h y el sync dura ~1 min, la probabilidad real es ~cero, pero
es cheap insurance.

**Confirmado por:** Mariano, sesión 2026-04-17.

---

## 2026-04-17 — Clasificación `Familia` como nivel paralelo a `sub_rubro`

**Decisión:** se incorpora una clasificación adicional `Familia` por
SKU que **convive en paralelo** con el `sub_rubro` existente. Ambos
niveles viven como columnas del DataFrame de facturación y se usan
en paralelo, sin reemplazarse entre sí.

**Origen del dato:**

El maestro viene de un archivo externo que Mariano mantiene:
`assets/sku_familia_subgrupo.xlsx`, hoja `SKU Familia Sub-grupo`.
Columnas relevantes: `Producto_Id` (SKU), `Familia_Id` (código corto
de la familia, ej. "ACC", "AFX", "BULIT"). La columna `Sub-Grupo`
del archivo **se descarta al cargar** — no la usamos porque el
`sub_rubro` vigente viene del maestro de productos de Contabilium
(decisión previa, mapping dinámico).

**Implementación:**

- `data_loader.load_familia(path)` lee solo `sku` + `familia`,
  dedupea por SKU.
- `transforms.enrich_familia(df_fc, df_familia)` hace left-join por
  SKU sobre la facturación ya clasificada. SKUs sin match caen en
  `FAMILIA_SIN_ASIGNAR = "SIN FAMILIA"` (análogo semántico a `SIN ASIGNAR`
  para sub_rubro).
- `transforms.prepare_facturacion` recibe `df_familia=None` como
  parámetro opcional (retrocompatibilidad) y llama a `enrich_familia`
  como paso 4b del pipeline (después de `classify_skus`).
- `app.py` carga el archivo con `@st.cache_resource` desde
  `assets/sku_familia_subgrupo.xlsx` (compartido entre modo API y
  Modo Manual Secundario — no depende de la fuente).
- El dict `health` ahora incluye `skus_sin_familia` (lista de SKUs
  sin match en el maestro de familias, para trazabilidad).

**Por qué convivencia y no reemplazo:**

- `sub_rubro` es la clasificación que ya usa el dashboard en múltiples
  lugares (tab Cobertura, tab Análisis, exports de agenda, heatmaps,
  Pareto). Reemplazarla arriesga romper todas esas vistas.
- `Familia` es una agrupación más amplia (~15 familias vs ~30
  sub-rubros). Ver ambos en paralelo permite preguntas distintas:
  "¿cuánto vendí de la familia AFX?" vs "¿cuánto del sub-rubro H?".
- El archivo maestro de Mariano tiene una columna `Sub-Grupo` que
  SÍ parece similar al sub_rubro actual, pero mientras no haya
  necesidad explícita, no introducimos una segunda fuente de verdad
  para el mismo concepto.

**Alternativas descartadas:**

- **Reemplazar `sub_rubro` con `Sub-Grupo` del nuevo archivo**:
  descartado por el riesgo de romper todas las vistas que ya usan
  sub_rubro. Si en el futuro se valida que son equivalentes al
  100%, se puede migrar — pero no es urgente.
- **Commitear el archivo en `data/` o en otra carpeta**: descartado.
  `assets/` ya existía y es donde vive `logo.png`. Mantener los
  maestros estáticos del proyecto todos juntos simplifica.
- **Descargar la Familia desde un endpoint de Contabilium**: no
  existe endpoint conocido para eso. Mariano la mantiene en Excel.

**Dónde aparece el filtro en la UI:**

Solo en la tab **Sub-rubro**, como un tercer selectbox lado a lado
con los de sub-rubro y SKU. Los 3 filtros son acumulativos. El
resto de las tabs no se tocaron.

**Confirmado por:** Mariano, sesión 2026-04-17 (post-deploy).

---

## 2026-04-17 — Vista Trimestral en Cobertura (calendario actual por default)

**Decisión:** se agrega un tercer rango temporal **"Trimestre"** al
pipeline de la app, disponible en la tab **Cobertura**. El rango por
default es el **trimestre calendario actual** (Q1=ene-mar, Q2=abr-jun,
Q3=jul-sep, Q4=oct-dic). El selector de trimestre en la sidebar
ofrece los últimos 8 trimestres y el usuario puede elegir cualquiera.

**Solo disponible en Modo API:**

El Modo Manual Secundario no soporta trimestre. Los xlsx de
facturación actuales cubren semana + mes, y no tiene sentido pedirle
a Mariano que descargue manualmente otro xlsx de 3 meses solo para
una vista adicional.

Cuando el usuario está en Modo Manual, `st.session_state.df_tri` es
`None` y la opción "Trimestre" directamente **no aparece** en el
selector de período de `views/cobertura.py`.

**Dónde se invoca el pull:**

El botón "Sincronizar" del modo API ahora pullea **3 rangos** (mes +
semana + trimestre) en serie. El sync pasa de ~1 min a ~2-3 min
total. Alternativa descartada: botón separado para el trimestre. La
simplicidad ("un solo botón para todo") gana sobre la latencia
marginal.

**Por qué calendario y no últimos 3 meses rolling:**

- Alineación con reportes contables y QBRs ("rendimiento del Q2 2026"
  vs "últimos 90 días").
- Estabilidad del rango: el Q2 siempre es abr-jun, independientemente
  de qué día del mes se esté viendo el dashboard.
- Mariano confirmó explícitamente el calendario actual como default.

**Cache:**

Cada pull (mes, semana, trimestre) usa `@st.cache_data(ttl=3600)`
con `(fecha_desde, fecha_hasta)` como key. Si en la próxima sesión
el rango del trimestre no cambió (mismo Q del mismo año), reusa el
cache — no re-pullea 900+ comprobantes. El botón "Resync forzado"
limpia los 3 caches a la vez.

**Alternativas descartadas:**

- **Trimestre rolling (últimos 3 meses hacia atrás)**: no se alinea
  con QBRs ni con el lenguaje contable. Descartado.
- **Calcular trimestre por composición de meses ya pulleados**: solo
  funcionaría si el usuario ya tiene cache de los 3 meses
  individuales, lo cual casi nunca es el caso. Mejor un pull único.
- **Agregar trimestre también a las tabs Resumen, Sub-rubro y Análisis**:
  descartado por ahora. Mariano pidió solo Cobertura. Si aparece
  demanda, se replica el mismo patrón (trivial con `st.session_state.df_tri`).

**Confirmado por:** Mariano, sesión 2026-04-17 (post-deploy).

---

## 2026-04-17 — Filtrado del dropdown "Exportar agenda"

**Decisión:** el selectbox del bloque "Exportar agenda" en la sidebar
excluye dos clases de valores que son técnicamente vendedores en el
DataFrame pero no representan personas a las que tenga sentido
generar una agenda:

- **String vacío `""`**: clientes del maestro con
  `IdUsuarioAdicional = 0` o `null` — sin vendedor asignado en
  Contabilium. En marzo 2026 son 33 clientes (~3% de la cartera).
- **Prefijo `"ID_"`**: clientes asignados a un `IdUsuarioAdicional`
  que no aparece en el dict `VENDEDORES` (ej. `"ID_239"`). Son IDs
  que no facturaron en el rango usado para derivar el mapping
  (probablemente ex-vendedores o usuarios inactivos).

**Implementación:**

En `app.py`, el list-comp que arma `_vendedores_export` filtra ambos
casos:

```python
_vendedores_export = sorted(
    v
    for v in df_clientes["vendedor"].dropna().astype(str).unique().tolist()
    if v and not v.startswith("ID_")
)
```

**Por qué filtrar en la UI y no en el loader:**

Los clientes sin vendedor o con ID huérfano **sí tienen que existir**
en el DataFrame `df_clientes` — aparecen en otros cálculos (ej.
total de clientes en el maestro, joins de facturación por documento).
Filtrarlos en el loader los eliminaría del resto del pipeline, lo
cual no es correcto.

La solución es filtrar **solo el dropdown de exportación de agenda**
(donde carece de sentido mostrarlos), dejando el DataFrame base
intacto.

**Tareas operativas futuras (no bloqueantes):**

- Identificar quién es `IdUsuarioAdicional = 239` (1 cliente asignado:
  "ANDREA DELGADO") y reasignarlo en Contabilium o agregarlo al dict
  `VENDEDORES` si resulta ser un vendedor válido.
- Asignar los 33 clientes sin vendedor a quien corresponda en el
  maestro de Contabilium.

**Confirmado por:** Mariano, sesión 2026-04-17 (post-deploy).


---

## 2026-04-18 — Trimestre como ventana móvil de 3 meses + recorte del mes en curso

**Decisión:** la opción "Trimestre" del dashboard deja de ser un
trimestre calendario fijo (Q1=ene-mar, Q2=abr-jun, …) y pasa a ser
**una ventana móvil de 3 meses consecutivos** definida por un "mes
final" elegible por el usuario. Ejemplo: mes final = abril 2026 →
ventana = feb+mar+abr 2026.

Adicionalmente, **cuando el rango incluye el mes en curso, el sync
recorta la fecha_hasta al día de hoy** en lugar del último día del
mes. Esto aplica tanto al selector de **Mes** como al último mes del
**Trimestre** cuando coincide con el mes actual.

**Contexto:** Mariano planteó dos problemas con el diseño anterior:

1. **Q1/Q2/Q3/Q4 calendario es arbitrario para el negocio.** Si el
   Jefe de Ventas quiere analizar "los últimos 3 meses cerrados" o
   "feb-mar-abr porque abril es el arranque del segundo trimestre
   comercial", el trimestre calendario no le sirve — siempre mostraba
   el trimestre en curso (completo o incompleto) o un trimestre
   pasado entero, sin flexibilidad intermedia.

2. **Comparar meses cerrados vs el mes en curso distorsiona la
   lectura.** Si el dashboard se usa el 18 de abril, mostrar el "mes
   de abril" hasta el 30 de abril implica incluir facturación que
   todavía no existe. Los promedios por vendedor quedan subvaluados
   porque se dividen entre días que aún no transcurrieron.

**Alternativas descartadas:**

- **Dejar Q calendario y agregar un segundo selector "últimos 3
  meses".** Complica la UI y duplica conceptos ("¿qué trimestre
  miro?"). Preferimos reemplazar.
- **Permitir 3 meses no-contiguos** (ej: ene + mar + may). Requiere
  3 rangos de API separados, 3 caches independientes, y la
  justificación comercial es débil — los análisis normalmente son
  sobre 3 meses seguidos.
- **Proyectar el mes en curso a 30 días** (regla de 3 sobre los días
  transcurridos) para que sea comparable. Descartado: agregaría
  complejidad conceptual ("esto es la venta proyectada") y el usuario
  prefiere ver la cifra real al día, no una estimación.

**Implementación (app.py):**

- `_rango_mes(y, m, today=None)` ahora acepta `today` y recorta
  `fecha_hasta` a `today` si `(y, m) == (today.year, today.month)`.
- `_rango_trimestre(y_final, m_final, today=None)` devuelve la
  ventana `[primer día del mes de 2 meses atrás, último día del mes
  final recortado]`.
- `_opciones_trimestres_recientes(n)` devuelve (año, mes) de los
  últimos `n` meses como candidatos a "mes final".
- `_label_trimestre(y_final, m_final)` produce labels tipo
  `"Feb → Abr 2026"` o `"Nov 2025 → Ene 2026"` cuando cruza año.
- Selector en sidebar renombrado a "Trimestre (mes final) — para
  Cobertura". El nombre visible "Trimestre" **se mantiene** a pedido
  del usuario (evitar retrabajo de capacitación).
- Caption debajo del selector muestra el rango real calculado.

**Cambios que NO se hicieron (consciente):**

- El rango **Semana** ya se comporta bien — `_semana_default()` usa
  `lunes → hoy`, que es un recorte natural. No requiere cambios.
- El resto del pipeline (`transforms.py`, `metrics.py`, `views/`)
  sigue agnóstico al origen de los rangos. Solo cambia qué fechas
  se pasan a `_api_sync_fc`.

**Confirmado por:** Mariano, sesión 2026-04-18.


---

## 2026-04-18 — Mensaje amigable + timeout de 10 min en sync API

**Decisión:** cuando la sincronización con Contabilium falla por
timeout global (>10 min) o por cualquier error de red/API genérico,
el dashboard muestra el mensaje:

> **Lamentablemente Contabilium está caído.** Por favor probá
> nuevamente más tarde o utilizá la opción de carga manual más abajo.

Los errores de autenticación (credenciales mal configuradas)
mantienen su mensaje específico — son un problema distinto y requieren
otra acción del operador.

**Contexto:** el sync API normal tarda ~2-3 min; los requests
individuales tienen 30s de timeout × 3 retries. Si Contabilium
está degradado pero no devuelve 500 directo, el sync puede
prolongarse mucho y el usuario queda mirando el spinner sin saber
si esperar o abortar. Sin mensaje claro, termina cerrando la
pestaña en lugar de caer al Modo Manual Secundario — que es
exactamente para lo que existe.

**Implementación (app.py):**

- Constante local `SYNC_TIMEOUT_SEC = 600` en el handler del botón
  "Sincronizar".
- Helper local `_check_timeout()` que se invoca entre sub-steps
  del sync (maestros → fc mes → fc semana → fc trimestre) y
  levanta `TimeoutError` si el elapsed global lo supera.
- `except (TimeoutError, api_loader.ApiError)` unificado con el
  mensaje amigable. `AuthError` queda separado.

**Alternativas descartadas:**

- **`ThreadPoolExecutor.submit(...).result(timeout=600)` envolviendo
  todo el sync.** Más robusto (corta mid-call si un request queda
  colgado), pero requiere propagar el contexto de Streamlit al
  thread hijo (`add_script_run_ctx`) y complica el manejo del cache
  de `@st.cache_data`. El approach actual (medir elapsed entre
  sub-steps) captura el 95% de los casos reales con una décima
  parte de complejidad.
- **Agresividad mayor (5 min).** 2-3 min es el tiempo normal; 5 min
  daría poco margen en días de red lenta o Streamlit Cloud
  saturado. 10 min es un punto razonable: si al decimoprimer minuto
  no terminó, asumimos problema real.

**Confirmado por:** Mariano, sesión 2026-04-18.


---

## 2026-04-18 — Sprint 1 de insights: ticket promedio, concentración 80%, mix top-3, patrones temporales

**Decisión:** agregar cuatro nuevas métricas de performance a nivel
vendedor, distribuidas entre las tabs Resumen, Cobertura y Análisis,
sin romper las vistas existentes.

### Qué se agregó

1. **Ticket promedio** — en Resumen. Debajo del total (semana y mes)
   aparece el count de tickets y el monto promedio por ticket. También
   en la tabla "Ventas por vendedor" se agregan las columnas `tickets`
   y `ticket_promedio` para semana y mes.

2. **Concentración 80% por vendedor** — en la tabla de cobertura
   general (en Cobertura y en Resumen). Columna "Conc. 80%" = N
   clientes que concentran el 80% de la venta del vendedor. Cuanto
   más bajo, más dependiente el vendedor de pocos clientes.

3. **Mix top-3 de sub-rubro por vendedor** — misma tabla, columna
   "Mix top-3". Formato `"A 85% · BA 10% · resto 5%"`. Los 3 sub-rubros
   con mayor participación en la venta FAC propia del vendedor.

4. **Patrones temporales** — nueva sub-sección en la tab Análisis
   (4to bloque). Dos gráficos: ventas por día de la semana (Lun-Dom)
   y ventas por quincena (1-15 vs 16-fin). Selector de vendedor
   independiente.

### Contexto

Mariano pidió un paquete grande de mejoras (9 features de performance
+ discovery de cobranzas). Decidimos ordenar por sprints: Sprint 1
captura las 4 features que funcionan solo con los datos actuales
(sin pullear meses históricos) y tienen cálculo trivial. Sprint 2
agregará la capa histórica (12 meses) para habilitar Δ vs mes
anterior, clientes nuevos, dormidos y retención.

### Implementación

- **Nueva columna canónica `id_comprobante`** en el DataFrame de
  facturación, exportada tanto por `api_loader.load_fc_api` (Id real
  del comprobante de Contabilium, como string) como por
  `data_loader.load_fc` (string sintético `"vendedor|documento|fecha|tipo"`).
  Necesaria para contar tickets distintos. El proxy del xlsx subestima
  el count si dos comprobantes del mismo tipo salen al mismo cliente
  el mismo día — raro, aceptable.
- **Nuevas funciones en `metrics.py`**: `ventas_por_vendedor` se
  extendió con `tickets` y `ticket_promedio`; `cobertura_por_vendedor`
  se extendió con `concentracion_80` y `mix_top3` (helpers
  `_concentracion_80_por_vendedor`, `_mix_top3_por_vendedor`).
  Dos funciones nuevas: `ventas_por_dia_semana` y `ventas_por_quincena`.
- **Views actualizadas**: `resumen.py` (2 captions y 4 columnas extras
  en la tabla), `cobertura.py` (column_config para las 2 columnas
  nuevas), `analisis.py` (bloque 4 nuevo con 2 gráficos de barras y
  tablas de detalle debajo).

### Alternativas descartadas

- **Contar tickets con groupby `(vendedor, documento, fecha, tipo)`
  en lugar de agregar una columna**. Funcionaba pero ofuscaba la
  intención. Una columna canónica `id_comprobante` se reusa mejor en
  futuros insights (DSO por vendedor, frecuencia de compra, etc.).
- **Mostrar "N clientes = 80%" como porcentaje (ej: "10%")**. Se
  descartó: el número absoluto ("2 clientes") es más impactante
  visualmente para detectar riesgo.
- **Gráficos de día de semana con Altair / Plotly**. Se usó
  `st.bar_chart` por simplicidad y porque el theme Dieter Rams no
  requiere más customización. Si en algún momento se quieren tooltips
  o interactividad, migrar.

**Confirmado por:** Mariano, sesión 2026-04-18.


---

## 2026-04-18 — Sprint 2: Δ comparativo MoM/YoY + infra histórica liviana

**Decisión:** agregar dos comparativos temporales al Resumen — Δ vs
mes anterior (MoM) y Δ vs mismo mes año pasado (YoY) — sin cambiar la
estructura de tabs ni el resto del pipeline. Ambos comparativos se
calculan sobre un rango **recortado al mismo día del mes** que el
período actual, para que sea apples-to-apples.

### Contexto

Sprint 2 del plan de insights (iteración del 2026-04-18). Mariano
había confirmado pullear 12 meses de histórico para habilitar features
de Sprints 2 y 3. Pero 12 meses → ~20 min de sync, inaceptable para
una feature sola. Tradeoff: **Sprint 2 usa solo 2 rangos chicos
extra** (mes anterior + YoY), amortizados con TTL=24h. Features que
necesitan histórico amplio (dormidos/nuevos/retención) se postergaron
a Sprint 3 donde el pull pesado habilita 4 features a la vez.

### Implementación

- **`app._mes_anterior(y, m)`** y **`app._mes_yoy(y, m)`** — helpers
  de navegación temporal (mes-1 y año-1).
- **`app._rango_mes_comparativo_mismo_dia(y, m, today)`** — recorta
  el mes comparativo al mismo día que `today.day`, con fallback al
  último día del mes si el mes comparativo es más corto (ej. hoy
  2026-03-31, comp feb 2026 → fecha_hasta 2026-02-28).
- **`app._api_sync_fc_historico`** — cache decorator con TTL=86400
  (24h) para rangos cerrados. Mismo payload que `_api_sync_fc`, solo
  cambia el TTL. Las dos funciones comparten el maestro de clientes
  y la `ApiSession` ya cacheada.
- **`metrics.comparativa_temporal(df_actual, df_prev, df_yoy)`** —
  devuelve dict con montos, deltas, tickets. Tolera `df_prev=None`
  y `df_yoy=None` (Modo Manual): devuelve `delta_*_pct = None` y la
  UI degrada con un mensaje.
- **`views/resumen.py`** — `st.metric("Total mes", …, delta=…)` con
  el delta MoM como flecha verde/roja; caption debajo con el delta
  YoY (porque `st.metric` solo soporta un delta). Helper
  `_format_delta(pct)` para el formato.
- **Session state nuevo**: `df_fc_prev_raw`, `df_fc_yoy_raw`,
  `df_prev`, `df_yoy`, `api_rango_comp`, `api_errors_prev`,
  `api_errors_yoy`. Inicializados a `None` y reseteados explícita-
  mente en Modo Manual (que no los soporta).

### Alternativas descartadas

- **Pullear los 12 meses de una sola vez (20 min).** Se habría
  "destrabado todo" pero el sync normal se volvía inaceptable. Se
  postergó al Sprint 3 donde el costo se amortiza entre 4 features.
- **Usar pulls mes-por-mes en Sprint 3** con cache independiente
  por mes. Probablemente sea lo que usemos en Sprint 3 para que si
  un mes falla, los otros sigan disponibles. Pendiente de validar.
- **Delta YoY como segundo `st.metric`** en lugar de caption.
  Se descartó porque `st.metric` ya tiene el MoM y 2 deltas en
  paralelo se vuelve visualmente ruidoso en la vista de 2 columnas.
- **No recortar al mismo día**. Daría un delta distorsionado en los
  primeros días del mes (ej. al día 5 de abril vs marzo completo ≈
  "abril está -84%" cuando en realidad es solo 5/30 del mes).
  El recorte elimina ese artificio.

**Confirmado por:** Mariano, sesión 2026-04-18 (continuación de
Sprint 1 del mismo día).


---

## 2026-04-18 — Sprint 3: análisis longitudinal con histórico 12 meses (dormidos, nuevos, retención, frecuencia)

**Decisión:** agregar 4 features que requieren un histórico amplio
(12 meses calendario previos + mes en curso), todas montadas sobre
un pull único y opt-in que vive en su propio botón de la sidebar.

### Features agregadas

1. **Clientes dormidos** — umbral 90 días sin FAC del vendedor
   asignado. Incluye "nunca compraron". UI en Cobertura (bloque 5).
2. **Clientes nuevos** — primera FAC en el mes actual sin compras
   previas en los 12 meses anteriores. Match estricto. UI en
   Cobertura (bloque 6).
3. **Tasa de retención por vendedor** — A = compraron hace 6 meses
   (mes calendario). B = subset de A que compró en los últimos 90
   días desde hoy. Retención % = |B ∩ A| / |A|. UI en Análisis.
4. **Frecuencia de compra por cliente** — promedio de días entre
   compras consecutivas para clientes con ≥2 FAC propias. UI en
   Análisis.

### Infra: carga opt-in del histórico

- **Botón nuevo en la sidebar**: "Cargar histórico (12 meses)" (o
  "Recargar" si ya hay pull previo). Separado del botón
  "Sincronizar" normal para no encarecerlo.
- **Rango pulleado**: desde `hoy.year - 1, hoy.month, 1` hasta
  `hoy`. Ejemplo hoy=2026-04-18 → desde 2025-04-01, hasta
  2026-04-18. Cubre 12 meses calendario previos + mes en curso.
- **Cache**: `_api_sync_fc_historico` con TTL=86400 (24h, el mismo
  decorator que se usa para los comparativos MoM/YoY). Después del
  primer pull pesado, las siguientes 24 h sirven del cache.
- **Costo estimado primer pull**: ~11-18 min (12000+ comprobantes
  con N+1 de 10 workers). Aceptable para una acción explícita del
  usuario, 1 vez al día.
- **Degradación**: si el histórico no está cargado, las 4 sub-
  secciones muestran un aviso "cargá el histórico" y el resto del
  dashboard funciona normal.

### Alternativas descartadas

- **Hacer el pull al tocar "Sincronizar"**. Habría sumado 11-18 min
  al flujo normal, inaceptable.
- **12 pulls mensuales independientes** (tolerancia a fallos). Más
  robusto pero complejo. En la práctica, si un batch falla de los
  ~240 páginas, la lista `errors` ya lo captura sin romper los
  demás. Se deja mensual-por-mensual como opción si el pull único
  se vuelve inestable.
- **Umbral dormido = 60 días** (propuesta inicial). Mariano
  confirmó 90 días como más representativo del negocio GSU (ciclo
  de compra típico).
- **Cliente nuevo = sin compras EVER** (criterio laxo). Descartado:
  preferimos "sin compras en los últimos 12 meses" para incluir
  reactivaciones reales (cliente histórico que volvió).
- **Frecuencia medida por comprobante en vez de por día**. Se
  colapsa a "una compra por día" para no contar dos FAC del mismo
  día como dos "intervalos de 0 días", lo cual distorsionaba hacia
  abajo el promedio en vendedores con split de facturación.

### Implementación

- **Nuevas funciones puras en `metrics.py`**:
  `clientes_dormidos`, `clientes_nuevos`, `tasa_retencion`,
  `frecuencia_compra_por_cliente`. Match estricto consistente con
  el resto del módulo.
- **Nuevo cache `_api_sync_fc_historico` reutilizado** en el botón
  histórico (ya existía para comparativos).
- **Session state nuevo**: `df_fc_hist12_raw`, `df_hist12`,
  `api_hist_last_sync`, `api_errors_hist`, `api_rango_hist`.
- **`views/cobertura.py`**: refactor del `return` temprano por un
  helper `_render_secciones_historicas(df_mes, df_clientes)` que
  se llama siempre, para que los bloques 5 y 6 funcionen aunque
  el período seleccionado esté vacío.
- **`views/analisis.py`**: dos funciones nuevas
  `_seccion_retencion` y `_seccion_frecuencia` que chequean
  `df_hist12` y degradan elegantemente.

### Validación programática

Smoke test in-memory con cartera de 5 clientes y perfiles
construidos ex profeso (frecuente / dormido / nuevo / retenido /
fuga). Los 4 cálculos cuadraron contra los valores esperados
manualmente.

**Confirmado por:** Mariano, sesión 2026-04-18.


---

## 2026-04-18 — Tab Cobranzas: 5 KPIs desde el detalle del comprobante

**Decisión:** agregar una 6ta tab "Cobranzas" al dashboard con el
estado actual de la deuda viva — aging por cliente, top deudores,
deuda vencida vs corriente, días promedio de deuda por vendedor.
Todos los cálculos salen de enriquecer `load_fc_api` con 4 campos
que ya vienen en el detalle del comprobante; **no se llama a ningún
endpoint nuevo de la API**.

### Contexto

El discovery de 2026-04-18 (sesión 7, `_exploracion-api-contabilium/
smoke_cobranzas*.py`) confirmó empíricamente que los endpoints
tradicionales de cuentas corrientes / saldos / cobros no existen en
Contabilium UY. Pero descubrió que el detalle de cada comprobante
(`GET /api/comprobantes/?id={ID}`, que ya pulleamos en el N+1 de
`load_fc_api`) trae `Saldo`, `FechaVencimiento`, `CondicionVenta` y
`Pagos`. Validación contra marzo 2026: cuando `Saldo = 0` el
comprobante está cobrado; cuando `Saldo > 0` el monto coincide con
`ImporteTotalBruto × 1.22` (IVA UY) — o sea, el saldo bruto del
comprobante.

### Implementación

- **`api_loader.load_fc_api`** agrega 4 columnas nuevas al DataFrame:
  `saldo` (parseado con `parse_monto_uy` ya existente),
  `fecha_vencimiento` (parseado con `parse_fecha_iso`),
  `condicion_venta` (string), `pagos_count` (int). Replicados en
  todas las filas del mismo comprobante.
- **`_empty_fc_df`** actualizado.
- **5 funciones nuevas en `metrics.py`**:
  - `_deuda_viva_por_comprobante(df)` (helper privado): colapsa a
    una fila por comprobante con `saldo > 0` y `tipo == FAC`.
  - `_bucket_aging(dias)` (helper): convierte días desde
    vencimiento en bucket string.
  - `aging_por_cliente(df, hoy)` → matriz cliente × bucket.
  - `top_deudores(df, n=20)` → ranking.
  - `dias_promedio_deuda_por_vendedor(df, hoy)`.
  - `deuda_vencida_vs_corriente(df, hoy)` → dict con KPIs.
- **Nueva vista `views/cobranzas.py`** con 4 bloques: KPIs (4
  metrics), aging, top deudores con slider, días promedio por
  vendedor. Degradación en Modo Manual con aviso.
- **Tab nueva en `app.py`** como 6ta posición, entre Análisis y Salud.

### Alternativas descartadas

- **Pullear `/api/cobranzas/search`** en paralelo al N+1 de
  comprobantes. Fue la hipótesis inicial antes del discovery.
  Descartada al confirmar que el detalle del comprobante ya trae
  todo lo que necesitamos. Queda disponible como fuente
  secundaria si en el futuro quisiéramos un DSO clásico preciso
  (necesitaríamos la fecha de cobro, no solo "hoy - fecha de
  emisión").
- **Aging sobre fecha de emisión en lugar de vencimiento.** Más
  simple pero menos útil para el negocio: un comprobante emitido
  hace 60 días con plazo de 90 días NO está vencido, y contar sus
  60 días como mora sería erróneo. Vamos con vencimiento.
- **Ratio venta/cobro por período.** Descartado para el MVP porque
  `Saldo` está en bruto con IVA y `monto` está en neto sin IVA —
  no son comparables sin una normalización cuidadosa. Si Mariano
  pide la métrica, la agregamos con el cruce explícito.
- **Dashboard hermano separado** (conversación original). Preferimos
  integrar como tab nueva para que el Jefe de Ventas tenga venta
  + cobranza en la misma reunión, sin saltar entre apps.
- **NCF negativas contra deuda**. Las NCF pueden venir con saldo
  negativo (si compensan una FAC no cobrada). Para el MVP se
  ignoran — solo contamos `saldo > 0` en `tipo == FAC`. La
  compensación ocurre a nivel ERP; nuestro dashboard solo
  refleja el estado del saldo de cada FAC, no los asientos.
- **DSO clásico preciso**. Requiere la fecha del cobro, que está
  en `Pagos[0].Fecha` pero ese campo suele ser null. Alternativa
  más rigurosa: cruzar con `/api/cobranzas/search` por
  `IDComprobante`. Para el MVP usamos "días promedio de deuda" =
  `hoy - fecha_emision` sobre comprobantes con saldo > 0. Proxy
  simple, calculable sin requests extras.

### Validación

Smoke test in-memory con 6 comprobantes sintéticos:
- 1 FAC cobrada → no aparece en deuda.
- 1 FAC vencida 15 días (bucket 0-30, saldo 500).
- 1 FAC vencida 45 días (bucket 31-60, saldo 800).
- 1 FAC vencida 100 días (bucket 90+, saldo 1200).
- 1 FAC con vencimiento futuro (bucket Al día, saldo 600).
- 1 FAC sin fecha de vencimiento (bucket Sin vencimiento, saldo 300).

Las 4 funciones retornaron los números esperados al dólar:
- Aging: todos los clientes en sus buckets correctos.
- Top deudores: Cliente C primero con 1800 (2 comprobantes).
- Días promedio: V2=61 días, V1=42.3 días. Cálculo manual verificado.
- Deuda total: 3400, vencida 2500, corriente 900, pct_vencida 73.53%.

**Confirmado por:** Mariano, sesión 2026-04-18 (continuación de
Sprints 1/2/3 del mismo día).


---

## 2026-04-18 — Tab Inventario: stock + semanas de stock bajo 3 cortes

**Decisión:** agregar una 7ma tab "Inventario" que muestra stock
actual por SKU y calcula **semanas de stock** bajo 3 cortes de venta
semanal promedio (últimos 30 días / últimos 90 días / mejor mes de
los últimos 12), marcando **críticos** a los SKUs con menos de 4
semanas según el corte de 3 meses.

### Hallazgos del discovery (2026-04-18, post-Cobranzas)

- `/api/conceptos/search` trae los campos `Stock` y `StockMinimo`
  en cada concepto. **No hay endpoint separado de depósitos ni de
  stock**; todos los candidatos dan 404 (`/api/depositos`,
  `/api/stock`, `/api/inventario`, `/api/clientes/{id}/saldo`,
  etc.). Consolidado alcanza (confirmado por Mariano).
- El **detalle del combo** (`/api/conceptos/?id={ID}` con
  `Tipo == "Combo"`) trae `Items: [{Id, Codigo, Cantidad}]` con la
  composición. Lista de materiales completa, sin N+1 adicional por
  componente (el stock de cada componente ya vino en el listado).
- Ejemplo validado empíricamente: combo "COMBO SLT" con 26
  componentes. Contabilium reporta `Stock = 9` en el combo, pero
  nuestro cálculo derivado da `13` — ambos valores son posibles
  según el método, **elegimos el derivado**.

### Definiciones operativas (Mariano 2026-04-18)

- **Consolidado por SKU** (no por depósito).
- **Crítico = <4 semanas** de stock.
- **Default de venta semanal promedio**: últimos 3 meses.
- **Combos**: considerar componentes para determinar el stock del combo.

### Implementación

- **`api_loader.load_productos_api`** agrega columnas `stock` y
  `stock_minimo` (del listado de conceptos).
- **`api_loader.load_combos_api`** reescrita: ahora hace N+1 sobre
  los combos (~9 en GSU, ~1 s adicional) para obtener `Items` y
  calcular `stock_combo = floor(min(stock_componente / cantidad))`.
  Si falta Items o un componente no tiene stock, cae en 0. Nueva
  columna `stock` en el DF de combos.
- **Dos funciones nuevas en `metrics.py`**:
  - `ventas_semanales_por_sku(df_hist, hoy)` → 3 cortes, con
    valores negativos clampeados a 0. Helper privado
    `_venta_unidades_por_sku_en_rango` para el rango genérico.
  - `inventario_semanas_stock(df_productos, df_combos, df_hist,
    hoy)` → tabla unificada con stock, 3 cortes de venta, 3 de
    semanas, flag `critico`. Orden: críticos primero por semanas
    ascendente, SKUs sin venta al final.
- **Constantes**: `SEMANAS_POR_MES = 4.345`, `CRITICIDAD_SEMANAS = 4.0`.
- **Nueva vista `views/inventario.py`** con 3 KPIs arriba, filtros
  (Tipo / Sub-rubro / Solo críticos), tabla principal con styling
  de fila roja para críticos.
- **Tab nueva en `app.py`** como 6ta posición (entre Cobranzas y Salud).

### Alternativas descartadas

- **Usar el `Stock` que Contabilium reporta en los combos**. Más
  simple pero menos conservador. Si Contabilium tiene un cálculo
  obsoleto o manual, nuestro cálculo derivado da la foto real de
  "combos efectivamente armables". Mariano lo pidió explícitamente.
- **Ponderar los 3 cortes** (ej. 50% corte 3m + 30% mejor-12m +
  20% último mes). Descartado — es más honesto mostrar los 3 y
  dejar que el usuario elija qué escenario le preocupa. El flag
  crítico se define sobre el default para consistencia.
- **Calcular también stock valorizado (UYU)**. Nice to have pero
  fuera del scope inicial. Si Mariano lo pide, se agrega con
  `precio × stock` multiplicando `PrecioFinal` del concepto.
- **Pullear el stock de forma separada del maestro de productos**
  (ej. cache propio con TTL chico porque el stock cambia mucho).
  Descartado: el stock viene con el mismo pull que productos y el
  TTL de 1h del maestro es suficiente. El "Resync forzado" ya
  atiende el caso de necesitar datos frescos.
- **Filtro de "stock bajo stock mínimo"** (usar `StockMinimo` del
  ERP). Descartado porque en los datos de GSU muchos productos
  tienen `StockMinimo = 0`. El criterio por semanas de stock es
  más universal y útil para la reunión.

### Validación

Smoke test in-memory con 5 productos + 1 combo:
- Producto D (stock 0, venta alta) → 0 semanas, crítico ✓
- Producto B (stock 10, venta ~19/sem) → 0.5 semanas, crítico ✓
- Producto A (stock 100, venta ~5/sem) → 19.8 semanas, OK ✓
- Producto C (stock 500, venta 0.5/sem) → 925 semanas, OK ✓
- Producto E (sin venta) → NA, no crítico ✓
- Orden correcto: críticos primero.

**Validación adicional contra la cuenta UY real**:
- 579 productos pulleados con stock.
- 448 con stock > 0 (77%).
- Stock total = 225,426 unidades.
- 9 combos con stock derivado (5 con stock positivo, 4 en 0 porque
  no tienen Items en el detalle — productos individuales marcados
  como "Combo" en el ERP).

**Confirmado por:** Mariano, sesión 2026-04-18.

---

## 2026-04-29 — Stock valorizado en tab Inventario (precio neto)

### Decisión

Agregar **stock valorizado** a la tab Inventario:

- **Precio**: `PrecioFinal` del concepto en Contabilium, dividido por
  1.22 (IVA básico UY 22%) para que sea **neto sin IVA** y comparable
  con el `monto` del Resumen/Sub-rubro/Análisis.
- **Schema enriquecido**: nueva columna `precio` (float) en
  `df_productos` y en `df_combos` (`api_loader.py`). Helper
  `_precio_neto(v)` tolera tanto número directo como string locale UY.
  Constante `IVA_BASICO_UY = 1.22` al tope del módulo.
- **Métricas**: `metrics.inventario_semanas_stock` agrega columna
  `valor_stock = stock × precio` (UYU netos sin IVA).
- **UI** (`views/inventario.py`):
  - Cuarto KPI arriba "Valor de stock (UYU)".
  - Columna `valor_stock` en la tabla, después de `stock`.
  - Caption explica neto sin IVA.
  - Modo Manual: valor_stock = 0 (xlsx no trae precio); columna se
    oculta elegantemente si falta.

### Alternativas descartadas

- **Bruto con IVA** (precio de lista directo, sin dividir). Más
  simple pero rompe la comparabilidad con los montos del resto del
  dashboard. Mariano eligió neto explícitamente.
- **Valorizar a costo** (lectura CFO). Útil pero requiere otro pull
  o campo (`PrecioCompra`) y la lectura comercial es el caso
  primario para la reunión semanal.
- **No valorizar combos**. Si el inventario incluye combos en
  unidades, omitirlos del valor da una foto incompleta. Combos usan
  su `PrecioFinal` propio (no la suma de componentes — Contabilium
  ya lo gestiona).
- **Hardcodear el factor IVA en los views**. Centralización: la
  división vive en `_precio_neto` del loader y el resto del
  pipeline trabaja siempre con netos.

### Cache caveat

Cambia el schema del maestro de productos/combos. Después del
deploy hay que tocar **'Resync forzado'** + 'Sincronizar' para que
aparezca `precio` (mismo patrón que con Cobranzas e Inventario).

**Confirmado por:** Mariano, sesión 2026-04-29.

---

## 2026-04-29 — Insight cruzado: venta 30d + deuda >90d en Cobranzas

### Decisión

Bloque nuevo al final de la tab Cobranzas: **"Clientes con venta
reciente y deuda vieja"**. Lista los clientes a los que se les
facturó en los **últimos 30 días** (FAC) y al mismo tiempo tienen
comprobantes vencidos hace **más de 90 días** con saldo > 0.

- **Ventanas fijas**, no respetan el período seleccionado: 30 días
  de venta y 90 días de vencimiento son umbrales operativos, no
  derivados del rango de sync. Mantenerlos fijos hace el insight
  comparable entre semanas.
- **Función nueva** `metrics.clientes_venta_reciente_con_deuda_vieja(
  df_fc, hoy, ventana_venta_dias=30, umbral_deuda_dias=90)`. Reutiliza
  `_deuda_viva_por_comprobante` para la parte de saldos.
- **Vendedor reportado** = el de la **venta más reciente** (no el
  asignado en cartera). Coherente con el resto de Cobranzas.
- **Fuente con preferencia**: `df_hist12 > df_tri > df_mes`. La tab
  elige la mejor disponible y avisa en caption. Con histórico 12m
  se ven todos los vencimientos viejos; con `df_mes` se pueden
  perder facturas emitidas hace >120 días.
- **NCF no cuentan como deuda** (consistente con el resto de
  Cobranzas).
- **UI**: tabla con razón social, vendedor, monto venta 30d, deuda
  vieja, fechas. Filtro opcional por vendedor.

### Alternativas descartadas

- **Respetar el período seleccionado** (Mes/Semana/Trimestre). El
  insight tiene umbrales semánticos propios; la ventana del sync
  no debería moverlos.
- **Solo histórico 12m obligatorio**. Degrada elegante a trimestre
  o mes con caption explicativo.
- **Match estricto vendedor=asignado**. El insight es "el cliente
  debe Y le seguimos vendiendo", no es de cobertura.
- **Mostrar comprobantes individuales** en lugar de agregar por
  cliente. Más detalle pero menos accionable. Drill-down si Mariano
  lo pide.

### Validación

Smoke test sintético con 7 comprobantes (4 clientes en estados
mixtos): solo D1 (venta 10d + deuda 120d) cae en el cruce. ✓

**Confirmado por:** Mariano, sesión 2026-04-29.

---

## 2026-04-29 — Cartera depurada por actividad 12m (toggle ON default)

### Decisión

Filtro de "cartera viva" para Cobertura y Análisis: cuando hay
histórico 12m cargado, los clientes **sin FAC de ningún vendedor en
los últimos 365 días** se excluyen del **denominador** de
cobertura, sub-rubro y SKU. Convive con el concepto "Cliente
dormido" (90d, alertable):

- **Dormido (>90d)**: sigue en cartera, alertable. Sub-sección
  Cobertura → Clientes dormidos. **No se toca.**
- **Inactivo (>365d, incluye los que nunca compraron)**: fuera del
  denominador. Sub-sección nueva → Clientes inactivos. Exportable.

### Implementación

- **Helpers nuevos en `metrics.py`**:
  - `clientes_activos_12m(df_hist, hoy, ventana_dias=365) -> set[str]`
    — set de documentos con FAC de **cualquier vendedor** en la
    ventana. Match a nivel cliente, no a la asignación.
  - `clientes_inactivos_12m(df_clientes, df_hist, hoy) -> DataFrame`
    — cartera asignada menos los activos, con `documento`,
    `razon_social`, `vendedor_asignado`, `fecha_ultima_compra` (NaT
    si nunca), `monto_12m` (siempre 0 por construcción).
- **Toggle en sidebar** (`app.py`): checkbox **"Excluir clientes
  inactivos (>12m sin compra)"**, default **ON** cuando hay
  histórico, deshabilitado con caption si no hay. Cuando se activa,
  `df_clientes_act = df_clientes[documento ∈ activos]`.
- **Propagación**: nuevo kwarg `df_clientes_act` (default = None →
  cae a `df_clientes`) en las firmas de:
  - `views/resumen.py` (cobertura general).
  - `views/cobertura.py` (cobertura general / sub-rubro / SKU /
    no compraron SKU).
  - `views/analisis.py` (penetración, heatmap).
  - `exports.exportar_agenda_vendedor` (Resumen y Penetración).
- **NO se toca**: Pareto, concentración 80, mix top-3 (operan sobre
  venta, no cartera); Clientes dormidos, Nuevos, Retención,
  Frecuencia (operan sobre comportamiento, usan cartera completa).
- **Sub-sección nueva** "Clientes inactivos (12m)" al final de
  `views/cobertura.py` con tabla, filtro por vendedor asignado y
  botón **Descargar inactivos.csv**.
- **Hoja nueva "Clientes inactivos"** en
  `exports.exportar_agenda_vendedor` cuando se pasa `df_hist12`.
  Solo lista los del vendedor.

### Definición de "actividad"

- **Cualquier vendedor cuenta** (no match estricto). El objetivo es
  limpiar clientes muertos del denominador, no penalizar al
  asignado por una venta cruzada que no controla. Es diferente del
  cálculo de cobertura, que SÍ usa match estricto.
- **Solo FAC** (NCF no cuenta como actividad).
- **Ventana 365 días**, no calendario (12 meses) — más predecible y
  sin bordes raros con meses de distinta longitud.

### Alternativas descartadas

- **Match estricto vendedor=asignado para definir actividad**.
  Penalizaría al asignado por una venta cruzada que no controla.
- **Auto-aplicar siempre que haya histórico** (sin toggle). Deja al
  usuario comparar contra el comportamiento histórico si lo
  necesita y evita confusión.
- **Unificar dormido e inactivo bajo un solo umbral configurable**.
  Son dos conceptos distintos: dormido (90d) es "alertable, hay que
  reactivar", inactivo (>12m) es "candidato a baja".
- **Bloquear cobertura/penetración si no hay histórico**. Degrada
  con cartera completa + caption.

### Validación

Smoke test sintético:
- 4 clientes en cartera (1 nunca compró, 1 hace 400d, 1 hace 60d,
  1 hace 300d a otro vendedor).
- `clientes_activos_12m` = {D1, D3} ✓ (D3 contó pese a venta cruzada).
- `clientes_inactivos_12m` lista D2 y D4 ✓.
- Cobertura V1: 33.3% (3/3 asignados, 1 con venta) → con cartera
  depurada D2 fuera → 50% (2 asignados, 1 con venta) ✓.

### Cache caveat

`@st.cache_data` de `_agenda_bytes_cached` ahora incluye
`df_clientes_act` y `df_hist12` en la key — al cambiar el toggle o
al cargar histórico, la agenda se regenera.

**Confirmado por:** Mariano, sesión 2026-04-29.

---

## 2026-05-05 — Sprint C (Facturación masiva): tres decisiones arquitectónicas

**Contexto:** la sesión 13 cerró el discovery del workflow REST de emisión
de Contabilium UY (2 endpoints oficiales validados end-to-end con CAE real).
Quedaron tres decisiones de arquitectura para implementar el módulo masivo.

### Decisión 1 — Módulo aislado con su propio entry point

`facturador_app.py` es el 3er entry point Streamlit del repo (después de
`app.py` y `comisiones_app.py`), deployado como app separado en Streamlit
Cloud con su propia URL y password. NO se integra al dashboard principal
como una tab más.

**Razón:** este es el único módulo del proyecto autorizado a llamar
endpoints de escritura de la API de Contabilium (emisión de factura legal
con CAE). El círculo de acceso debe ser el más chico de los tres apps,
y aislar el módulo en un deploy propio permite controlar permisos a
nivel infra (otra password, otro Settings de Secrets) sin afectar el
dashboard principal o Comisiones.

`claude.md.txt` línea 900 fue actualizado para reflejar la excepción
explícita: el dashboard principal sigue siendo read-only, el facturador
es el único con escritura permitida y dentro de garantías específicas
(gate `FACTURAR`, run estrictamente secuencial con throttling, trazabilidad).

### Decisión 2 — Anti-doble-facturación basado en `RefExterna` server-side, sin persistencia local

Caveat fundamental descubierto en discovery: la API REST de Contabilium UY
**no actualiza el `IDComprobante` de la orden de venta** después de emitir
un comprobante via `crear` + `emitirFE`. La orden queda eternamente como
`Estado: Pendiente` con `IDComprobante: 0`. Eso rompe el filtro intuitivo
"órdenes pendientes = `IDComprobante == 0`" para detectar candidatas a
facturar.

Pero el comprobante sí guarda `RefExterna` con el id de la orden de origen
(visible en `GET /api/comprobantes/?id=` y en `/api/comprobantes/search`).

**Decisión:** antes de cada run masivo, paginar
`/api/comprobantes/search?fechaDesde&fechaHasta` (~21 páginas para 1000
items, ~35-42s con throttling), filtrar borradores (`Numero` ending en
`-00000000`) y construir un `dict[RefExterna, IDComprobante]`. Cualquier
orden cuyo id esté en ese dict se descarta del bucket facturable.

**Por qué no persistir local (CSV / Sheet / DB):**
1. La verdad fiscal vive en el comprobante, no en el log local.
2. Si el log se desincroniza con el server, el operador no se entera.
3. El comprobante mismo es el sistema de registro distribuido, sin
   necesidad de mantener un cache adicional.

**Por qué no filtrar server-side:** lo intentamos. `?filtro=`,
`?refExterna=`, `?RefExterna=` no funcionan — el server los ignora o los
interpreta como filtro de cliente. Tenemos que filtrar client-side.

**Costo aceptable:** ~40s por run masivo de 50-150 órdenes — irrelevante
comparado con el tiempo total del run (3 requests × 0.7s × 100 órdenes
= 3.5 minutos).

### Decisión 3 — Asistente conversacional con tool use acotado, no RAG ni text-to-SQL libre

Tab "🤖 Asistente" del dashboard principal usa Claude API (`claude-sonnet-4-6`)
con un set acotado de 18 tools que el LLM elige según la pregunta. Cada
tool consulta los DataFrames cacheados del dashboard via pandas y devuelve
un dict serializable.

**Por qué no text-to-SQL ni text-to-pandas libre:**
1. Predecible: el LLM elige tool de un set conocido, no inventa columnas
   (riesgo común en text-to-SQL: el LLM asume `revenue` cuando la columna
   real es `monto`).
2. Auditable: cada tool call se loguea (input + result preview) y se
   muestra al usuario en un expander de debug.
3. Sin riesgo de inyección.
4. Extensible: agregar tool nueva = definir entry en `TOOLS` + función +
   entry en `_TOOL_FUNCTIONS`. Costo marginal bajo.

**Decisiones específicas de implementación:**
- System prompt dinámico construido por consulta con la fecha actual
  (`date.today()`) + rango real de datos disponibles. Sin esto el LLM
  consultaba 2023/2024 con "últimos 12 meses" usando su training cutoff.
- `df_hist12` (12 meses procesado) como dataset preferido, fallback a
  `df_tri` → `df_mes` → `df_sem`.
- Cap de 6 iteraciones de tool use para prevenir loops infinitos.
- Costo: ~USD 0.005-0.02 por consulta. <USD 5/mes para uso real.

**Confirmado por:** Mariano, sesión 2026-05-05/06.

---

## 2026-05-13 — Facturador cancela orden post-emisión + comisiones cruza RefExterna

### Contexto

Hasta hoy el facturador masivo emitía la factura via API y dejaba la
orden de venta en `Pendiente`. Gabi reportó que "Libres" (StockConReservas)
bajaba doble: una vez porque StockActual bajaba bien (Contabilium descuenta
físico al emitir), otra vez porque StockReservado quedaba colgado en la
orden. Resultado: reservas fantasma que bloqueaban facturaciones nuevas
con error `HTTP 500 — El producto no tiene stock suficiente`.

Único endpoint que toca StockReservado es `POST /api/ordenesventa/Cancel`.
Cancelar la orden post-emisión libera la reserva sin tocar StockActual
(que ya bajó solo por el bug). Pero rompía comisiones, que excluyen
estado `Cancelada`.

### Decisión 1 — Pipeline del facturador agrega `cancelar_orden` post-emisión

`facturador.py::facturar_orden()` llama `cancelar_orden(session, id_orden)`
después de `emitir_fe` exitoso. Best-effort: si Cancel falla, NO rompe
(la factura ya está emitida con CAE válido). Devuelve flags
`orden_cancelada` + `orden_cancel_error` para que el caller los logue.

Schema del Sheet `log_facturacion` extendido con 2 columnas nuevas
(`orden_cancelada`, `orden_cancel_error`). Migración in-place del header
si el Sheet existe con schema viejo.

### Decisión 2 — Comisiones cruza `RefExterna` para no perder canceladas-facturadas

**INVARIANTE CRÍTICO (Mariano, 2026-05-13): el cálculo de comisiones NO
puede fallar nunca.** Cualquier cambio que toque `commissions.py` o
`comisiones_data.py` requiere smoke test que confirme el path legacy
(sin órdenes canceladas-facturadas) da resultado idéntico al cambio
anterior.

Implementación:
- Nueva `comisiones_data.cargar_ids_ordenes_facturadas_via_api(session,
  desde, hasta)` pagina `/api/comprobantes/search` y devuelve set de
  RefExterna (= IDs de órdenes facturadas via API).
- `cargar_ventas_desde_api()` acepta param opcional `ids_facturadas_via_api`.
  Cuando estado == "Cancelada" pero el `ID` de la orden está en el set,
  la cuenta como venta válida (excepción al filtro de cancelada).
- `comisiones_app.py` invoca el cruce antes de cargar ventas.

**Smoke test invariante** (`_exploracion-api-contabilium/smoke_comisiones_refexterna.py`):
con set vacío (pre-rollout) el resultado es IDÉNTICO al legacy. 479 ventas
en abril 2026, brutas y excluidas coinciden bit a bit. **Antes de tocar
comisiones en el futuro, correr este smoke**.

### Decisión 3 — Vencimiento de facturas: emisión + 30 días

`facturador.py::DIAS_VENCIMIENTO_DEFAULT = 30`. Antes mandábamos
`FechaVencimiento: None` y Contabilium aplicaba su default de 10 días,
incorrecto para Suprabond B2B (opera a 30). Cambiar la constante si la
política comercial cambia.

### Cleanup operativo 2026-05-13

90 órdenes Pendientes históricas con factura emitida vía API (reservas
fantasma acumuladas semanas) canceladas en bulk con
`_exploracion-api-contabilium/cleanup_reservas_fantasma.py --apply`.
Total liberado: ~UYU 777.231 en reservas fantasma. Cero fallos.
A futuro el flow nuevo previene la acumulación.

**Confirmado por:** Mariano, sesión 2026-05-13.

---

## 2026-05-21 — Nueva fórmula de compensación v1.2 (propuesta acordada, NO implementada)

**Decisión:** se acordó una nueva fórmula de compensación para los
vendedores GSU activos, motivada por el cambio normativo uruguayo que
fija el salario mínimo en $49.855 UYU y establece que sólo el sueldo
fijo cuenta para alcanzarlo. El esquema anterior ($10.000 fijo + 2,35%
× venta neta + 3% × cobranza) deja de cumplir.

**Esquema acordado (v1.2):**

- **Sueldo fijo** = $49.855/mes.
- **Comisión mensual** sobre excedente, con tres tramos por pilar:
  - Venta neta: 0% hasta $600k, 2,35% entre $600k–$1,5M, 5% sobre $1,5M.
  - Cobranza: 0% hasta $700k, 3% entre $700k–$1,5M, 4% sobre $1,5M.
- **Bono trimestral por pilar** (evaluación independiente de venta y
  cobranza, sumados):
  - Cat A (3 meses con umbral pleno): 10% × com_venta_trim + 15% ×
    com_cobranza_trim.
  - Cat B (avg ≥ 50% umbral y ≥1 mes pleno): 5% + 8%.
  - Sin categoría: 0.
- **Licencia por vacaciones**: por ley UY, mes de licencia se computa
  con promedio de los otros 2 meses (afecta clasificación y bono).

**Estado:** propuesta acordada, **NO implementada en `commissions.py`**
al cierre de esta sesión. Pendiente confirmación final de Mariano
después de la conversación con los vendedores que arrancó 2026-05-20.

**Variantes evaluadas y descartadas:**

- **v2** (cobranza "3% sobre todo el monto", no sobre excedente):
  +$67k/mes para Suprabond, $21k/mes/vendedor — demasiado caro.
- **E1/E2** (escalones discretos de $100k, mismo monto por escalón):
  perdían $7-10k/mes vs v1 por la "lumpiness" en los bordes.
- **STEPPED cumulative** (tabla por bracket de $100k incluyendo
  brackets bajos): +$25,5k/mes, costo extra entero en los brackets
  bajos.
- **v1.1** (v1 + microcomisión 0,3%/0,5% bajo umbral): +$15,9k/mes,
  reparto plano de $5.300/vendedor. Descartada en favor de v1.2 que
  premia la consistencia y desempeño proporcional.
- **v1.2 con umbrales aumentados $100k** (700/800 en lugar de 600/700):
  ahorraba ~$17k/mes empresa pero recortaba ~$5k/mes a los vendedores.
  Se mantuvo la calibración 600/700 para alinear con "al mismo volumen
  mismo total".

**Especificación completa:** ver `_learning/formula_compensacion_v1.2.md`
con la fórmula, fórmulas en pseudocódigo, constantes para implementar,
reglas operativas y guía de implementación.

**Regla firme antes de implementar:** correr el smoke test invariante
(`_exploracion-api-contabilium/smoke_comisiones_refexterna.py`)
**antes** de tocar `commissions.py` o `comisiones_data.py`. Plata
real, no se rompe — ver memoria `feedback_comisiones_invariante`.

**Costo extra estimado para Suprabond** (top-3 vendedores activos):
+$24.500/mes empresa, ~$294k/año. Mejora promedio para vendedores:
+10% a +13% en compensación mensual.

**Confirmado por:** Mariano, sesión 2026-05-21.
