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
