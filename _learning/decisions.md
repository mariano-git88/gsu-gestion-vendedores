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
