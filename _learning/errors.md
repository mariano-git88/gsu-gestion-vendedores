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
