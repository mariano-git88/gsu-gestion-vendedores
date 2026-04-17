# Errores — Gestión de Vendedores GSU

Append-only. Cada entrada documenta un error real (mío o de Mariano) que
vale la pena no repetir, con el contexto de cómo apareció y cómo lo
detectamos a tiempo.

---

## 2026-04-10 — Case-sensitivity de carpetas: WSL/OneDrive vs Linux

**Qué pasó:** Mariano creó una carpeta para los assets desde el Explorer
de Windows, llamándola `Assets` (con A mayúscula). Cuando inspeccioné
el disco desde WSL, todo parecía OK porque WSL en `/mnt/c/...` es
**case-insensitive** (hereda la convención de Windows). Pero git, que
sí registra el case real, la trackeaba como `assets` (lowercase).

Si yo hubiera escrito en `theme.py` el path `Path(__file__).parent /
"Assets" / "logo.png"` (mayúscula) y hubiéramos hecho push, hubiera
**funcionado en local pero roto en producción**. Streamlit Cloud corre
en Linux real, donde el case sí importa, y el archivo `Assets/logo.png`
literalmente no existe — solo `assets/logo.png`.

**Cómo lo detecté:** después de escribir el código con `"Assets"`, antes
de avisarle a Mariano que pushee, corrí `git status` y vi que git mostraba
la carpeta como `assets/` lowercase. El mismo `git ls-files` confirmó.
Comparé contra mi código y vi el mismatch.

**Cómo lo arreglé:** cambié el path en `theme.py` a `"assets"` (lowercase)
para que coincida con lo que git tiene tracked. Confirmé con
`Path.exists()` que sigue resolviendo OK localmente (porque WSL es
case-insensitive de todas formas).

**Lección operativa:**
1. **Antes de pushear cualquier path con asunción de case, correr
   `git status` y `git ls-files`** para ver el case real. No confiar
   en `ls` desde WSL.
2. **Linux es case-sensitive, Windows/WSL en /mnt/c no.** Cualquier
   archivo o carpeta que se referencie por path en código tiene que
   estar en lowercase o coincidir EXACTO con el case que git registró.
3. **Si Mariano dice "creé la carpeta X"**, antes de usar ese nombre
   en código, verificar el case real con git, no asumir lo que él dijo.

**Aplicación general:** vale para cualquier asset, fixture, módulo Python,
template HTML, etc. El riesgo es alto y silencioso (rompe solo en deploy).

---

## 2026-04-10 — Dependencias ocultas: pandas Styler.background_gradient requiere matplotlib

**Qué pasó:** al implementar la vista `views/analisis.py` (sub-tanda B
de la tab "Análisis"), usé `Styler.background_gradient(cmap='RdYlGn')`
para colorear la matriz de penetración por sub-rubro y `cmap='Greys'`
para el heatmap cliente×sub-rubro. Localmente funcionaba perfecto.

**Smoke test que lo dejó pasar:** corrí `streamlit run --headless` y
verificó HTTP 200, sin errores. PERO el headless run **solo verifica
que el app arranque y sirva la pantalla de login**. Las vistas que viven
adentro de tabs no se renderizan hasta que el usuario sube las planillas
y navega a la tab. Por eso el ImportError de matplotlib NUNCA se disparó
en mi smoke test — solo apareció en producción cuando Mariano cargó las
planillas reales y entró a la tab "Análisis".

**Por qué funcionaba local:** mi entorno local tenía matplotlib instalado
por casualidad (probablemente como dependencia transitiva de algún otro
paquete que tengo instalado en mi venv personal). Streamlit Cloud, en
cambio, solo instala lo que está explícitamente en `requirements.txt`.
matplotlib NO estaba ahí. → ImportError silencioso hasta el primer uso.

**El error en producción:** múltiples lugares de la vista Análisis
mostraban una traza interminable terminando en
`pandas/io/formats/style.py:_background_gradient → import matplotlib →
ImportError`.

**Cómo lo arreglé:** reemplacé las dos llamadas a `background_gradient`
por helpers locales (`_color_for_pct` y `_make_grey_scale`) que calculan
los colores manualmente con interpolación lineal en stdlib + pandas.
Cero dependencia de matplotlib. La alternativa de agregar `matplotlib`
a `requirements.txt` se descartó: 30+ MB de dependencia para usar 2
funciones de gradiente es desproporcionado, y matplotlib trae sus
propias dependencias transitivas que pueden traer otros problemas en
deploys futuros.

**Lecciones operativas:**

1. **`streamlit run --headless` NO valida vistas que viven dentro de
   tabs.** Solo valida que el módulo se importe y la pantalla inicial
   sirva. Para validar el contenido de las tabs hay que **ejecutar
   manualmente el código de las views con datos sintéticos**, hasta el
   punto de generar el output (HTML del Styler, render del componente,
   etc.). El smoke test "arranca el app" da una falsa sensación de
   seguridad.

2. **`pandas.Styler` tiene varias dependencias OPCIONALES** que pandas
   solo importa cuando se las invoca:
   - `matplotlib` para `background_gradient()`, `bar()`
   - `jinja2` para `to_html()` y casi todo el rendering
   - Posiblemente más en versiones futuras
   Si vamos a usar Styler, conviene **listar explícitamente jinja2 en
   requirements.txt** (Streamlit ya lo incluye, pero por si acaso) y
   **evitar las funciones que requieren matplotlib** salvo que matplotlib
   ya sea parte legítima del proyecto.

3. **Funciones seguras de Styler que NO requieren matplotlib**:
   - `Styler.format()` — formato de strings, OK
   - `Styler.applymap(func)` o `Styler.map(func)` — celda por celda con
     función custom, OK
   - `Styler.apply(func, axis=...)` — fila/columna/tabla con función
     custom, OK
   - `Styler.set_properties()` — properties CSS estáticas, OK

4. **Funciones peligrosas que SÍ requieren matplotlib** (evitar o
   listarlas explícitamente como dependencia):
   - `Styler.background_gradient()`
   - `Styler.bar()`
   - `Styler.text_gradient()`

5. **Regla operativa para futuras visualizaciones**: si necesito
   gradientes de color en una tabla, **escribo helpers manuales** con
   interpolación lineal en RGB en lugar de usar las funciones built-in
   de pandas. Más código pero deploy garantizado y sin sorpresas.

**Aplicación general:** cualquier librería con "optional dependencies"
puede romper en deploy si solo se prueba en un entorno donde esas
dependencias ya están casualmente instaladas. **Pinear explícitamente
las dependencias** en requirements.txt y **probar en un entorno limpio**
(idealmente un Docker o un venv recién creado) elimina el riesgo.

---

## 2026-04-10 — Styler.applymap removido en pandas 3.x (segundo round del mismo bug)

**Qué pasó:** después de arreglar el problema de matplotlib (entrada
anterior) reemplazando `background_gradient` por styling manual, usé
`Styler.applymap(...)` para aplicar las funciones de color celda por
celda. Funcionaba sintácticamente y mi entorno local no protestó. Pero
al desplegar a Streamlit Cloud (pandas 3.x), la tab "Análisis" volvió a
romper con `AttributeError: 'Styler' object has no attribute 'applymap'`.

**Causa:** `Styler.applymap` fue **renombrado** a `Styler.map` en
pandas 2.1 (donde quedó como deprecated alias). En **pandas 3.x lo
eliminaron del todo**. Streamlit Cloud está corriendo Python 3.14 +
pandas 3.x, mientras que mi entorno local tiene pandas 2.x donde
`applymap` todavía existe (con DeprecationWarning silencioso).

**Cómo lo arreglé:** reemplacé `pivot.style.applymap(...)` y
`heat.style.applymap(...)` por `pivot.style.map(...)` y
`heat.style.map(...)`. Cambio puramente cosmético, misma funcionalidad,
soportado en pandas 2.1+.

**Lección operativa adicional:**

1. **Cuando dudo entre dos nombres de API en pandas, elegir el más
   reciente**. Pandas tiene un montón de pares "viejo nombre / nuevo
   nombre" donde el viejo está deprecated y va a desaparecer:
   - `Styler.applymap` → `Styler.map` (desde 2.1)
   - `df.append()` → `pd.concat()` (removido en 2.0)
   - `df.iteritems()` → `df.items()` (removido en 2.0)
   - `df.ix[]` → `df.loc[]` o `df.iloc[]` (removido hace años)

2. **Mi entorno local NO es referencia confiable** para validar que el
   código va a funcionar en producción. Tengo un mix de versiones que
   no necesariamente coincide con Streamlit Cloud. **La única validación
   real es probar en producción** (o en un entorno Docker que replique
   exactamente el de Streamlit Cloud).

3. **El smoke test "streamlit headless + HTTP 200" sigue siendo
   insuficiente** para detectar este tipo de bugs. Solo valida import +
   primera pantalla, no el código que vive dentro de las tabs. Para
   cambios en views/, el único test confiable es ejecutar la función
   `render()` directamente con datos sintéticos y verificar que NO
   levanta excepciones — pero eso requiere una sesión de Streamlit
   activa, lo cual es complicado fuera del runtime real.

4. **Patrón recomendado** para futuros cambios visuales: **commit + push
   + verificación inmediata en producción**. No esperar a "validar todo
   junto" porque cada bug nuevo solo aparece cuando lo provocás. Acumular
   cambios sin probar los multiplica.

**Aplicación general:** APIs deprecated en librerías populares casi
siempre **se eliminan eventualmente**. Si una librería te dice "esto
está deprecated, usá la otra", tomalo en serio: **migrá ahora**, no
cuando la versión nueva rompa tu deploy.

---

## 2026-04-17 — Firma inconsistente entre loaders API

**Qué pasó:** durante Tanda B (loaders API), implementé
`load_productos_api` y `load_combos_api` con un parámetro opcional
`conceptos_items: list[dict] | None = None` para permitir reutilizar
el pull de `/api/conceptos/search` cuando ambos se llaman en
sucesión. Pero `load_clientes_api` se quedó con la firma original,
**sin el parámetro equivalente** `clientes_items`.

Al escribir `_api_sync_maestros` en `app.py` (Tanda E), le pasé
`clientes_items=clientes_items` a `load_clientes_api` — porque
mentalmente ya había asumido simetría con los otros loaders. Al
correr `streamlit run app.py` y tocar "Sincronizar", crasheó con:

```
TypeError: load_clientes_api() got an unexpected keyword argument
'clientes_items'
```

**Cómo lo detectamos:** Mariano lo reportó desde el navegador apenas
tocó el botón Sincronizar en la primera prueba local. El stack
trace apuntaba directo a la línea ofensora, muy fácil de diagnosticar.

**Cómo lo arreglé:** agregué el parámetro `clientes_items` a
`load_clientes_api` para que la firma quede simétrica con los otros
dos loaders. El cambio preservó retrocompatibilidad (default `None`,
hace el pull interno si no se pasa) y eliminó el error sin tocar el
caller.

**Lecciones operativas:**

1. **Cuando un patrón de API (firma, parámetros opcionales,
   convenciones) aparece en dos o más funciones relacionadas, aplicarlo
   a TODAS las funciones hermanas al mismo tiempo.** El "fácil ahorro"
   de no aplicarlo a la que parece que no lo necesita hoy se paga
   cuando mañana sí lo necesita, y el usuario del módulo asume simetría
   que no existe.

2. **Los tests programáticos `if __name__ == "__main__"` del
   api_loader no detectaron este bug** porque el self-test llamaba a
   `load_clientes_api` sin el parámetro. Solo el caller de `app.py`
   lo pasaba. Para futuros cambios de firmas, conviene invocar las
   funciones en el self-test con todas las combinaciones de parámetros
   que el app real usa.

3. **La validación local con `streamlit run` es barata y detecta
   estos errores al instante.** Justifica la molestia de configurar el
   venv + secrets cuando los cambios tocan lógica de carga.

**Aplicación general:** si sos el autor de una familia de funciones
(ej. `load_X_api` para varias entidades), tomate 30 segundos extra
para revisar que las firmas sean simétricas antes de declarar "hecho".

---

## 2026-04-17 — Comparar vs xlsx del mes equivocado: teoría errónea sobre "GSU es subset de Suprabond"

**Qué pasó:** durante Tanda C.3 corrí por primera vez
`comparar_api_vs_xlsx.py` para validar que `load_fc_api` producía los
mismos números que `data_loader.load_fc()` sobre el xlsx de marzo
2026. Los resultados mostraron diferencias **enormes**:

```
Filas:       xlsx=957    api=3158    diff=+2201
Monto total: xlsx=1.4M   api=3.9M    diff=+180%
```

Mi hipótesis inmediata: "la cuenta de Contabilium contiene TODO el
Grupo Suprabond Uruguay, y GSU es solo una unidad de negocio dentro
del grupo. El xlsx ya viene prefiltrado por GSU, la API nos devuelve
todo". Argumenté con evidencia circunstancial (documentos únicos
171 vs 444, observaciones que mencionan "GRUPO SUPRABOND URUGUAY",
etc.) y le pedí a Mariano que identificara cómo distinguir GSU del
resto (PuntoVenta específico, Inventario, tags, etc.).

**Qué era en realidad:** el xlsx `fc_mensual.xlsx` que Mariano tenía
en la carpeta de exploración era de **un mes anterior** (abril
parcial), no de marzo 2026 cerrado. Mariano lo reemplazó por el
correcto y la comparación siguiente cuadró al centavo:
`FAC, NCF, TIK` idénticos, monto total con diff de $0.00.

**Cómo lo detectamos:** Mariano supo inmediatamente al leer mi
hipótesis: "Actualizo el archivo. Probá de nuevo". Tras el update,
los números cuadraron y la "teoría del subset" se cayó sola.

**Lección operativa:**

Antes de construir teorías elaboradas sobre discrepancias de datos,
**verificar con el dueño de los datos que la entrada sea la que
yo creo que es**. Una pregunta simple y directa
("¿este archivo es el mes correcto?") ahorra media hora de
especulación que después se demuestra equivocada.

En particular, cuando los números difieren por un factor redondo
(~3x es sospechoso para una diferencia de mes, porque el mes tiene
~30 días y la ventana del rango puede solaparse distinto), **esa es
la señal #1 de que la entrada no es la esperada**, no de un bug
estructural.

**Aplicación general:** "la entrada está mal" es siempre la primera
hipótesis a testear en un debug de datos, y cuesta casi nada
falsearla/confirmarla (una pregunta al operador o un
`print(df.head())`). Construir teorías de arquitectura antes de
verificar la entrada desperdicia tiempo y desvía la conversación.
