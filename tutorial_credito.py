"""
tutorial_credito.py — Contenido del tutorial del Scoring Crediticio.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click en el
botón "Tutorial". Está pensado para quien opera la herramienta: explica qué
hace cada sección, qué significa cada número y en qué NO hay que confiar.
Sin tecnicismos innecesarios.

Si hay que actualizar el contenido, editar acá sin tocar `credito_app.py`.
El registro de cambios va aparte, en `cambios_credito.json`.
"""

import streamlit as st


def render() -> None:
    """Renderiza el tutorial completo dentro del modal."""

    st.markdown(
        """
### ¿Qué hace esta app?

Es una **calculadora de crédito comercial**. Agarra toda la historia de
facturas y cobranzas de Suprabond en Contabilium y, cliente por cliente,
responde tres preguntas:

1. **¿Cuánto plazo le puedo dar?** — 30, 45, 60 o 90 días.
2. **¿Hasta cuánta plata le puedo tener prestada?** — el límite.
3. **¿Cuánto le tengo que cobrar por ese plazo?** — la tasa.

La idea de fondo: si podés ofrecer más plazo con cheque diferido, entran
clientes que hoy no compran porque no les da el flujo. Pero el plazo es plata
prestada, y hay que saber a quién.

Cinco secciones, cada una para una cosa:

| Sección | Para qué sirve |
|---|---|
| **Resumen** | La foto de toda la cartera: cuánto te deben, cuánto está fuera de plazo y cómo se reparte por banda |
| **Clientes** | El buscador. La propuesta para cada cliente y su ficha completa con todas sus facturas |
| **Oportunidad** | A quiénes les podés ofrecer más plazo del que tienen, y cuánto capital te va a costar |
| **Riesgo** | Quiénes tienen hoy más plazo del que su comportamiento justifica, y quiénes concentran la mora |
| **Calidad de datos** | Qué se pudo bajar y qué no, y en qué estado está cada factura |

**La app no escribe nada en Contabilium.** Solo lee: no emite, no modifica ni
borra. No se puede romper nada desde acá.
"""
    )

    st.divider()
    st.markdown(
        """
### Dónde ver qué cambió

El botón **🆕 Novedades**, arriba a la derecha al lado del Tutorial, abre el
registro de actualizaciones: qué se agregó o se arregló y cuándo. En el
encabezado también figura la fecha de la última actualización.
"""
    )

    st.divider()
    st.markdown(
        """
### El semáforo

Cada cliente cae en una banda, y la banda es la que decide todo:

- 🟢 **A y B** — pagan bien. Se les puede estirar el plazo.
- 🟡 **C y D** — pagan con demoras. Plazo corto y ojo con el monto.
- 🔴 **E** — hoy no. O deben algo viejo y grande, o su historia es mala.
- ⚪ **S/D** — sin datos suficientes. **No quiere decir malo**: quiere decir
  que no sabemos. Hacen falta al menos 3 facturas cobradas para opinar, y lo
  decide una persona.

Son muchos los ⚪ S/D, pero pesan poco: son los clientes chicos y los
esporádicos.
"""
    )

    st.divider()
    st.markdown(
        """
### Cómo se arma el puntaje

Siete pilares que suman 100 puntos. Cada cliente saca una parte de cada uno.

| Pilar | Puntos | Qué mira |
|---|---|---|
| **Exceso de DSO** | 25 | Cuántos días de venta tuya tiene prestados **por encima** de lo pactado |
| **Atraso promedio** | 20 | Cuánto tarda en pagar después del vencimiento, pesando más las facturas grandes |
| **Peor caso** | 10 | El atraso de sus peores pagos, no el promedio |
| **Puntualidad** | 10 | Qué proporción de sus facturas paga casi a término |
| **Antigüedad** | 15 | Hace cuánto es cliente y si compra todos los meses |
| **Volumen** | 10 | Cuánto compra por año y si viene creciendo |
| **Situación hoy** | 10 | Qué parte de lo que debe hoy está vencido |

En la ficha de cada cliente, dentro de **Clientes**, ves exactamente cuántos
puntos sacó en cada pilar. Si un cliente te sorprende, ahí está el porqué.
"""
    )

    st.divider()
    st.markdown(
        """
### Qué es el DSO y por qué es el que más pesa

**DSO = cuántos días de tu propia venta tiene el cliente sin pagarte.** Si te
compra \\$100.000 por mes y te debe \\$200.000, tiene 60 días de venta tuya en
la mano.

Pesa más que el atraso por factura por una razón concreta: **Contabilium no
aplica los pagos a la factura más vieja.** Cierra facturas nuevas y deja las
viejas abiertas. Entonces las facturas que se cierran parecen puntuales aunque
la deuda total nunca baje.

El caso real que lo mostró: el cliente más grande de la cartera figura pagando
**2,7 días antes del vencimiento** y al mismo tiempo tiene **153 días de DSO**
con plazo pactado de 60, con facturas enteras impagas desde enero de 2025. El
atraso por factura decía "impecable". El DSO dijo la verdad.

**Regla práctica:** si el atraso promedio y el DSO de un cliente dicen cosas
muy distintas, esa diferencia **es** el hallazgo. Andá a mirarle las facturas.
"""
    )

    st.divider()
    st.markdown(
        """
### De dónde sale el plazo

Sale directo de la banda. Por defecto: **A → 90 días, B → 60, C → 45, D → 30,
E → nada**. Esos números los cambiás en la barra lateral.

La columna **"plazo actual"** es el plazo más largo que ese cliente ya venía
teniendo. Compará contra **"plazo sugerido"**:

- Sugerido **mayor** que el actual → hay margen para ofrecerle más. Están
  juntos en **Oportunidad**.
- Sugerido **menor** → hoy tiene más plazo del que su comportamiento
  justifica. Están juntos en **Riesgo**.
"""
    )

    st.divider()
    st.markdown(
        """
### De dónde sale el límite

No es un número inventado. Es la **exposición natural del plazo**:

> Si un cliente te compra \\$100.000 por mes y le das 60 días, cuando la cosa
> está en régimen te va a estar debiendo dos meses de compra: \\$200.000. Ese
> es el tamaño real del problema.

A eso se le aplica un factor según la banda (a los mejores un poco más, a los
peores un poco menos) y se lo recorta con un **tope de meses de compra**, para
no tener demasiada plata en un solo nombre aunque pague bárbaro.

**"Margen disponible"** = el límite menos lo que ya debe. Es lo que realmente
le podés seguir vendiendo a crédito hoy.

El límite está **con IVA**, porque es lo que el cliente efectivamente tiene que
pagar, que es lo que le estás prestando.
"""
    )

    st.divider()
    st.markdown(
        """
### De dónde sale la tasa

    tasa = costo de fondos + spread + prima de riesgo de la banda

- **Costo de fondos**: lo que te cuesta tener esa plata prestada, o lo que
  rendiría en otro lado. **Este número lo tenés que poner vos.**
- **Spread**: lo que querés ganarle al negocio de financiar.
- **Prima de riesgo**: el recargo por banda. No se inventa: sale de cuántos
  días de más se atrasa cada banda respecto de la mejor.

La columna que le sirve al vendedor es **"recargo %"**: cuánto más caro sale el
producto por los días **extra** sobre los 30 que ya se dan sin cargo. Si dice
3,45%, el mensaje es literalmente *"a 90 días te sale 3,45% más caro"*.
"""
    )

    st.divider()
    st.markdown(
        """
### Los controles de la barra lateral

Todo lo que está ahí es **criterio de negocio**, no una ley. Está afuera del
código justamente para que lo puedas discutir y mover.

**Ventana de historia (12 / 18 / 24 meses)** — cuánta historia se baja. Más
meses es más contexto, pero el pull tarda más.

**Plazos por banda** — cuántos días le tocan a cada banda. Si querés ser más
conservador, bajá los de A y B.

**💰 Costo de fondos anual** — **el parámetro que importa.** Viene en 14% y es
un supuesto, no un dato de Suprabond. De acá sale toda la tasa y todo el
recargo. Antes de cotizarle a un cliente, poné el número real.

**Spread objetivo** — cuánto querés ganar por encima de tu costo. Es la
decisión de si financiar es un servicio a costo o un negocio.

**Tope: meses de compra** — el techo duro. Ningún cliente puede tener prestados
más de X meses de su propia compra, por bien que pague. Es la defensa contra
concentrar riesgo en un solo nombre.

**Veto: piso de deuda vencida y días** — un cliente con deuda vieja **y
grande** se va a rojo sin importar el puntaje. "Grande" es el mayor entre el
piso que pongas y medio mes de su compra. Bajarlo mucho es contraproducente:
con el piso en \\$1 quedaba vetado el 44% de la cartera por restos de centavos.

**Descontar colas de rendición** — dejalo tildado salvo que sepas lo que estás
haciendo. Cuando la nota de crédito del 10% de la rendición no se imputa, la
factura queda con un resto abierto para siempre. Contar eso como mora manda a
rojo a clientes que pagan **antes** del vencimiento. Destildalo solo si querés
ver la deuda cruda del ERP.
"""
    )

    st.divider()
    st.markdown(
        """
### En qué NO confiar

**Esto no es una probabilidad de que no te paguen.** El score dice quién paga
mejor y quién peor. No dice "este cliente tiene 3% de chance de no pagar
nunca". Para decir eso haría falta una lista de clientes que efectivamente no
pagaron, y en la cartera prácticamente no hay. Sin esa lista no hay contra qué
calibrar. **No uses el puntaje como si fuera un porcentaje de riesgo.**

**No ve nada fuera de Suprabond.** Sin Clearing de Informes y sin la Central de
Riesgos del BCU, un cliente puede estar impecable con nosotros y en default con
medio mercado. Por lo mismo **no sirve para clientes nuevos**: sin historia con
nosotros no hay nada que medir, y caen en ⚪ S/D.

**No ve cheques rechazados.** Si a un cliente le rebotó un cheque, en
Contabilium no queda de una forma que la app pueda leer. Si tenés esa
información, **pesa más que el score**.

**Faltan algunos recibos.** Cerca del 0,8%, porque Contabilium devuelve como
máximo 50 recibos por día y hay días con más. Está reportado en **Calidad de
datos**. No mueve la aguja, pero está dicho.

**El límite y el plazo son una sugerencia.** Es una calculadora, no una
autorización. La decisión final, sobre todo en montos grandes, sigue siendo de
alguien que conoce al cliente.
"""
    )

    st.divider()
    st.markdown(
        """
### Dudas comunes

**Un cliente que sé que paga bien me aparece en 🔴 E. ¿Por qué?**
Casi siempre es deuda vieja y grande arrastrada. Abrí su ficha en **Clientes**
y mirá la columna "estado" de sus facturas: si hay facturas `abierta` de hace
más de un año, el veto es correcto aunque el resto de su historia sea buena.
Si son `parcial` con restos chicos, revisá que esté tildado "Descontar colas de
rendición".

**La primera carga tarda muchísimo.**
Es esperable: 10 a 15 minutos. El endpoint de cobranzas de Contabilium no deja
paginar y hay que pedirle día por día. Después queda guardado 24 horas. Si
necesitás forzarlo, está el botón "Recargar datos".

**Cambié el costo de fondos y no cambió nada.**
Mueve la **tasa** y el **recargo %**, no el puntaje ni las bandas. El puntaje
mide comportamiento; la tasa es precio.

**¿Y si quiero darle más plazo a alguien que la app no recomienda?**
Podés, es tu decisión. Lo que la app te da es el costo de esa decisión escrito
en números: cuántos días se atrasa ese cliente y cuánta plata tuya va a tener.
"""
    )
