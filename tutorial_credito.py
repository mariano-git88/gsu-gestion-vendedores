"""
tutorial_credito.py — Manual completo (ELI5) del Scoring Crediticio + Novedades.

Se pintan en dos `st.dialog` distintos, desde los botones "📖 Tutorial" y
"🆕 Novedades" de la sidebar de `credito_app.py`.

El tutorial está pensado como manual integral: alguien que lo lee una vez tiene
que entender TODAS las funciones y poder explicárselas a otra persona que nunca
vio la herramienta. Por eso está dividido en secciones y cada cosa se explica
con qué es, para qué sirve y qué hacer con eso.

`render_novedades()` es el changelog: entradas con fecha, lo más nuevo arriba.
Cuando se toca algo que el usuario puede notar, va una entrada acá.
"""

import streamlit as st


def render() -> None:
    st.caption(
        "Manual completo. Leelo una vez y vas a poder usar —y explicar— toda "
        "la herramienta. Está dividido en secciones 👇"
    )

    (t_que, t_score, t_decisiones, t_tabs, t_params, t_ojo, t_faq) = st.tabs([
        "1. Qué es", "2. El score", "3. Las 3 decisiones", "4. Las pestañas",
        "5. Los parámetros", "6. En qué NO confiar", "7. Dudas comunes",
    ])

    # =================================================================
    with t_que:
        st.markdown(
            """
            ### ¿Qué es esta herramienta? 🏦

            Es una **calculadora de crédito comercial**. Agarra toda la
            historia de facturas y de cobranzas de Suprabond en Contabilium y,
            cliente por cliente, responde tres preguntas:

            1. **¿Cuánto plazo le puedo dar?** (30, 45, 60 o 90 días)
            2. **¿Hasta cuánta plata le puedo tener prestada?** (el límite)
            3. **¿Cuánto le tengo que cobrar por ese plazo?** (la tasa)

            La idea de fondo del negocio: si podés ofrecer más plazo con cheque
            diferido, entran clientes que hoy no te compran porque no les da el
            flujo. Pero el plazo es plata prestada, y hay que saber a quién.

            ### Cómo llega a eso, en criollo

            Para cada factura mira **cuándo venció** y **cuándo se pagó de
            verdad**. Con eso arma el prontuario de cada cliente: si paga antes
            o después, si es consistente o impredecible, hace cuánto que te
            compra, cuánto te compra y cuánta plata tuya tiene hoy en la mano.

            Todo eso se resume en un **puntaje de 0 a 100** y en una **banda**
            (A, B, C, D, E o S/D). De la banda salen las tres respuestas.

            ### El semáforo

            - 🟢 **A y B** — pagan bien. Se les puede estirar el plazo.
            - 🟡 **C y D** — pagan con demoras. Plazo corto y ojo con el monto.
            - 🔴 **E** — hoy no. O deben algo viejo y grande, o su historia es
              mala.
            - ⚪ **S/D** — sin datos suficientes. **No quiere decir malo**:
              quiere decir que no sabemos, y lo decide una persona.

            ### De dónde salen los datos

            Todo de **Contabilium**. La herramienta **no escribe nada**: solo
            lee. No podés romper nada tocando acá.
            """
        )

    # =================================================================
    with t_score:
        st.markdown(
            """
            ### Cómo se arma el puntaje

            Son **siete pilares** que suman 100 puntos. Cada cliente saca una
            parte de cada uno.

            | Pilar | Puntos | Qué mira |
            |---|---|---|
            | **Exceso de DSO** | 25 | Cuántos días de venta tuya tiene prestados **por encima** de lo que se pactó |
            | **Atraso promedio** | 20 | Cuántos días tarda en pagar después del vencimiento, pesando más las facturas grandes |
            | **Peor caso** | 10 | El atraso de sus peores pagos, no el promedio |
            | **Puntualidad** | 10 | Qué proporción de sus facturas paga casi a término |
            | **Antigüedad** | 15 | Hace cuánto que es cliente y si te compra todos los meses |
            | **Volumen** | 10 | Cuánto te compra por año y si viene creciendo |
            | **Situación hoy** | 10 | Qué parte de lo que te debe hoy está vencido |

            En la ficha de cada cliente (pestaña **Clientes**) vas a ver
            exactamente cuántos puntos sacó en cada pilar. Si un cliente te
            sorprende, ahí está el porqué.

            ### ¿Qué es el DSO y por qué pesa tanto?

            **DSO = cuántos días de tu propia venta tiene el cliente sin
            pagarte.** Si te compra $100.000 por mes y te debe $200.000, tiene
            60 días de venta tuya en la mano.

            Pesa más que el atraso por factura por una razón concreta:
            **Contabilium no aplica los pagos a la factura más vieja.** Cierra
            facturas nuevas y deja las viejas abiertas. Entonces las facturas
            que se cierran parecen puntuales aunque la deuda total nunca baje.

            El caso real que lo mostró: el cliente más grande de la cartera
            figura pagando **2,7 días antes del vencimiento** y al mismo tiempo
            tiene **153 días de DSO** con plazo pactado de 60, con facturas
            enteras impagas desde enero de 2025. El atraso por factura decía
            "impecable". El DSO dijo la verdad.

            **Regla práctica:** si el atraso promedio y el DSO de un cliente
            dicen cosas muy distintas, esa diferencia es el hallazgo. Andá a
            mirarle las facturas.
            """
        )

    # =================================================================
    with t_decisiones:
        st.markdown(
            """
            ### 1. El plazo

            Sale directo de la banda. Por defecto: **A → 90 días, B → 60,
            C → 45, D → 30, E → nada**. Esos números los podés cambiar en la
            barra lateral.

            La columna **"plazo actual"** es el plazo más largo que ese cliente
            ya venía teniendo. Comparala con **"plazo sugerido"**:
            - Sugerido **mayor** que el actual → hay margen para ofrecerle más.
            - Sugerido **menor** → hoy tiene más plazo del que su
              comportamiento justifica. Está en la pestaña **Riesgo**.

            ### 2. El límite

            No es un número inventado. Es la **exposición natural del plazo**:

            > Si un cliente te compra $100.000 por mes y le das 60 días de
            > plazo, cuando el sistema está en régimen te va a estar debiendo
            > dos meses de compra: $200.000. Ese es el tamaño del problema.

            A eso se le aplica un factor según la banda (a los mejores un poco
            más, a los peores un poco menos) y se lo recorta con un **tope de
            meses de compra**, para no tener demasiada plata en un solo nombre
            aunque pague bárbaro.

            **"Margen disponible"** = el límite menos lo que ya debe. Es lo que
            realmente le podés seguir vendiendo a crédito hoy.

            ### 3. La tasa

            `tasa = costo de fondos + spread + prima de riesgo de la banda`

            - **Costo de fondos**: lo que te cuesta a vos tener esa plata
              prestada (o lo que rendiría en otro lado). **Este número lo
              tenés que poner vos** — ver la pestaña 5.
            - **Spread**: lo que querés ganarle al negocio de financiar.
            - **Prima de riesgo**: el recargo por banda.

            La columna que le sirve al vendedor es **"recargo %"**: cuánto más
            caro sale el producto por los días **extra** sobre los 30 que ya se
            dan sin cargo. Si dice 3,45%, el mensaje es literalmente
            *"a 90 días te sale 3,45% más caro"*.
            """
        )

    # =================================================================
    with t_tabs:
        st.markdown(
            """
            ### 📊 Resumen
            La foto de toda la cartera. Arriba los cuatro números grandes:
            cuánta plata te deben, el DSO general, **cuánto capital está
            prestado por encima del plazo pactado** (o sea, financiación que
            estás regalando) y cuántos clientes tienen score.

            Abajo, la cartera partida por banda y una tabla que mide **cuánto
            cuesta la mora de cada banda** en días y en tasa.

            ### 👥 Clientes
            El buscador. Filtrás por banda, por facturación mínima o por
            nombre/RUT, y ves la propuesta completa de cada uno.

            Abajo elegís un cliente y se abre su **ficha**: el desglose de su
            puntaje pilar por pilar y **todas sus facturas** con el estado de
            cada una. Esta es la pantalla para preparar una conversación con un
            cliente puntual.

            ### 📈 Oportunidad
            A quiénes les podés ofrecer más plazo del que tienen. Te dice
            cuántos son, **cuánto capital extra te va a inmovilizar** y cuánto
            interés dejaría si lo usan todo.

            El gráfico ubica a cada cliente por DSO y por volumen: cuanto más a
            la derecha, más días de venta tiene prestados. La línea punteada
            marca los 30 días.

            ### ⚠️ Riesgo
            Dos listas. La primera: clientes que **hoy tienen más plazo del que
            su score justifica**. La segunda: los que concentran la mora,
            ordenados por capital inmovilizado — esa es la plata que ya estás
            financiando sin cobrar nada.

            ### 🔍 Calidad de datos
            Qué se pudo bajar y qué faltó, en qué estado está cada factura, y
            cuánto saldo se está tratando como **cola de rendición** en vez de
            deuda. Si algún número te resulta raro, mirá acá primero.

            ### 📐 Cómo se calcula
            La versión corta de este tutorial, siempre a mano dentro de la app.
            """
        )

    # =================================================================
    with t_params:
        st.markdown(
            """
            Todo lo que está en la barra lateral es **criterio de negocio**, no
            una ley. Está afuera del código justamente para que lo puedas
            discutir y mover.

            ### Ventana de historia (12 / 18 / 24 meses)
            Cuánta historia se baja. Más meses = más contexto, pero el pull
            tarda más. **La primera carga del día tarda 10-15 minutos** porque
            el endpoint de cobranzas de Contabilium no permite paginar y hay
            que pedirlo día por día. Después queda guardado 24 horas.

            ### Plazos por banda
            Cuántos días le corresponden a cada banda. Si querés ser más
            conservador, bajá los de A y B.

            ### 💰 Costo de fondos anual — **el parámetro que importa**
            Viene en **14% y es un supuesto, no un dato de Suprabond**. De acá
            sale toda la tasa y todo el recargo. **Antes de usar estos números
            para cotizarle a un cliente, poné el número real.**

            ### Spread objetivo
            Cuánto querés ganar por encima de tu costo. Es la decisión de si
            financiar es un servicio a costo o un negocio.

            ### Tope: meses de compra
            El techo duro. Ningún cliente puede tener prestados más de X meses
            de su propia compra, por bien que pague. Es la defensa contra
            concentrar riesgo en un nombre.

            ### Veto: piso de deuda vencida y días
            Un cliente con deuda vieja **y grande** se va a rojo sin importar
            el puntaje. "Grande" es el mayor entre el piso que pongas acá y
            medio mes de su compra. Bajarlo mucho es contraproducente: con el
            piso en $1 quedaba vetado el 44% de la cartera por restos de
            centavos.

            ### Descontar colas de rendición
            **Dejalo tildado salvo que sepas lo que estás haciendo.** Cuando la
            nota de crédito del 10% de la rendición no se imputa, la factura
            queda con un resto abierto para siempre. Contar eso como mora manda
            a rojo a clientes que pagan **antes** del vencimiento. Destildalo
            solo si querés ver la deuda cruda del ERP.
            """
        )

    # =================================================================
    with t_ojo:
        st.markdown(
            """
            ### Esto NO es una probabilidad de default

            El score dice **quién paga mejor y quién peor**. No dice "este
            cliente tiene 3% de chance de no pagarte nunca".

            Para decir eso haría falta una lista de clientes que efectivamente
            no pagaron, y en la cartera de Suprabond prácticamente no hay. Sin
            esa lista no hay contra qué calibrar. Es una limitación real, no un
            detalle técnico: **no uses el puntaje como si fuera un porcentaje
            de riesgo.**

            ### No ve nada fuera de Suprabond

            Sin Clearing de Informes y sin la Central de Riesgos del BCU, un
            cliente puede estar impecable con nosotros y en default con medio
            mercado. La herramienta no tiene forma de saberlo.

            Por la misma razón **no sirve para clientes nuevos**: sin historia
            con nosotros, no hay nada que medir. Esos caen en ⚪ S/D y los
            decide una persona.

            ### Tampoco ve cheques rechazados

            Si a un cliente le rebotó un cheque, en Contabilium no queda
            registrado de una forma que la herramienta pueda leer. Si tenés esa
            información en la cabeza o en una planilla, **pesa más que el
            score**.

            ### Faltan algunos recibos

            Alrededor del 0,8% de los recibos no se puede bajar, porque el
            endpoint de Contabilium devuelve como máximo 50 por día y hay días
            con más. Está reportado en la pestaña **Calidad de datos**. No
            mueve la aguja, pero está dicho.

            ### El límite y el plazo son una sugerencia

            Es una calculadora, no una autorización. La decisión final,
            especialmente en montos grandes, sigue siendo de una persona que
            conoce al cliente.
            """
        )

    # =================================================================
    with t_faq:
        st.markdown(
            """
            **Un cliente que sé que paga bien me aparece en 🔴 E. ¿Por qué?**
            Casi siempre es deuda vieja y grande arrastrada. Abrí su ficha en
            **Clientes** y mirá la columna "estado" de sus facturas: si hay
            facturas `abierta` de hace más de un año, el veto es correcto
            aunque el resto de su historia sea buena. Si son `parcial` con
            restos chicos, revisá que esté tildado "Descontar colas de
            rendición".

            **¿Por qué hay tantos clientes en ⚪ S/D?**
            Porque hace falta un mínimo de 3 facturas cobradas para opinar. Son
            muchos clientes pero poca facturación: son los chicos y los
            esporádicos.

            **La primera carga tarda muchísimo.**
            Es esperable: 10-15 minutos. El endpoint de cobranzas no permite
            paginar y hay que pedirle día por día. Después queda cacheado 24
            horas. Si necesitás forzar la actualización, está el botón
            "Recargar datos".

            **Cambié el costo de fondos y no cambió nada.**
            El costo de fondos mueve la **tasa** y el **recargo %**, no el
            puntaje ni las bandas. El puntaje mide comportamiento; la tasa es
            precio.

            **¿Puedo romper algo desde acá?**
            No. La herramienta solo lee de Contabilium. No emite, no modifica
            ni borra nada.

            **¿El límite es con IVA o sin IVA?**
            **Con IVA.** Es lo que el cliente efectivamente tiene que pagar,
            que es lo que le prestás.

            **¿Y si quiero ofrecerle más plazo a alguien que la herramienta no
            recomienda?**
            Podés. Es tu decisión. Lo que la herramienta te da es el costo de
            esa decisión escrito en números: cuántos días se atrasa ese cliente
            y cuánta plata tuya va a tener.
            """
        )


def render_novedades() -> None:
    """Changelog visible para el usuario. Lo más nuevo arriba."""
    st.caption(
        "Qué fue cambiando en la herramienta, de lo más nuevo a lo más viejo."
    )

    st.markdown(
        """
        ### 14 de agosto de 2026 — Primera versión 🎉

        Arranca el **Scoring Crediticio**. Puntúa a toda la cartera por
        comportamiento de pago y traduce el puntaje en **plazo, límite y tasa**
        por cliente.

        **Lo que trae:**
        - Score de 0 a 100 con siete pilares, y el desglose visible en la ficha
          de cada cliente.
        - Bandas A / B / C / D / E / S/D con semáforo.
        - Plazo sugerido, límite de crédito, margen disponible y el recargo %
          que hay que cobrar por el plazo extra.
        - Seis pestañas: Resumen, Clientes, Oportunidad, Riesgo, Calidad de
          datos y Cómo se calcula.
        - Todos los criterios de política editables desde la barra lateral.

        **Lo que apareció al construirla, y conviene saber:**

        - **La cartera tiene 86 días de DSO contra un plazo típico de 30.** De
          $11,8M que te deben, **$7,5M están por encima del plazo pactado**:
          financiación que ya estás dando gratis, antes de ampliar nada.
        - **El campo de vencimiento de Contabilium no sirve.** Trae siempre
          emisión + 30 días aunque la condición sea 60 o 90. La herramienta
          calcula el vencimiento a partir de la condición de venta.
        - **Los pagos no se imputan a la factura más vieja**, así que el atraso
          por factura queda optimista. Por eso el DSO pesa más que el atraso.
        - **Las colas de rendición no son deuda**: hay 125 facturas viejas con
          un resto de exactamente 10%, que es la nota de crédito que nunca se
          imputó. Se descuentan por defecto y se puede apagar.
        - **191 clientes ya pagan con cheque**, así que el cheque diferido no
          es territorio nuevo para ellos.

        **Pendiente:** el **costo de fondos** viene en 14% y es un supuesto.
        Poner el número real antes de cotizarle a un cliente.
        """
    )
