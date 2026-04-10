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
