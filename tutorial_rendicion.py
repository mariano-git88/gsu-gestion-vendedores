"""
tutorial_rendicion.py — Contenido del tutorial del módulo de Rendición de
Cobranzas.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click en el
botón "Tutorial" del sidebar de `rendicion_app.py`. Está escrito en tono
ELI5 (explicado bien simple), como los tutoriales de las otras apps GSU,
para que Valeria / operaciones lo usen sin conocimiento técnico.

El contenido vive solo en este módulo; para actualizarlo se edita acá sin
tocar `rendicion_app.py`.
"""

import streamlit as st


def render() -> None:
    """Renderiza el contenido completo del tutorial dentro del modal."""

    # =================================================================
    # Intro
    # =================================================================
    st.markdown(
        """
        ### ¿Para qué sirve esta app? 🧾

        Automatiza las **cobranzas de los vendedores**. En lugar de que
        vos entres a Contabilium factura por factura, la app:

        1. Lee la **planilla de cobranzas** que arman los vendedores.
        2. Busca cada factura en Contabilium y calcula la **Nota de
           Crédito del 10%** (descuento comercial) y cuánto se debería
           cobrar.
        3. Te muestra un **reporte** de qué está OK y qué hay que revisar.
        4. Cuando aprobás, **crea la NC + el recibo e imputa** todo en
           Contabilium (deja el saldo en 0), como lo hacés a mano —
           pero en segundos.
        """
    )

    st.info(
        "Es la misma cuenta y las mismas cobranzas de siempre. La app hace "
        "el trabajo repetitivo; vos seguís teniendo la última palabra en "
        "cada cobranza."
    )

    st.divider()

    # =================================================================
    # La planilla
    # =================================================================
    st.markdown(
        """
        ### Paso 0 — La planilla de los vendedores 📋

        La app espera un Excel con estas columnas (las que arman los
        vendedores):

        - **Fecha**
        - **Nro. Cliente**
        - **Nro Factura** (ej. `A-00034367`)
        - **Cobro Efectivo** — cuánta plata en efectivo
        - **Cobro Cheque** — cuánta plata en cheque
        - **Total Recibo**
        - *(opcional)* **Descuento** — poné `10%` o `No`
        - *(opcional)* **Nº Cheque** — el número del cheque

        **Tips para que salga bien:**
        - Si la cobranza lleva descuento, poné **10%** en la columna
          Descuento. Si es pago total (sin descuento), poné **No**.
        - Si se cobra con **cheque**, cargá el cheque en Contabilium.
          El **número del cheque lo confirmás vos al ejecutar** (con el
          cheque a la vista), así no importa si el vendedor lo tipeó mal.
          Si igual lo anota en la planilla, mejor: te queda de registro.
        """
    )

    st.divider()

    # =================================================================
    # Paso a paso
    # =================================================================
    st.markdown(
        """
        ### Paso a paso en la app 👇

        **1) Subí la planilla.** Arrastrá el Excel al recuadro de carga.

        **2) Tocá «▶️ Analizar».** La app busca las facturas en
        Contabilium y arma el reporte. *(Esto todavía no toca nada: solo
        mira.)*

        **3) Mirá el reporte.** Cada fila queda:
        - **✅ OK** → la cuenta cierra, lista para automatizar.
        - **⚠️ Revisar** → algo no cuadra (ver la columna **Motivo**).

        **4) Aprobá.** En la misma tabla, la columna **Aprobar**:
        - Las **OK** vienen ya tildadas.
        - Revisá las **⚠️** y tildá las que verifiques que están bien.
        - Podés destildar una OK que no quieras automatizar.
        """
    )

    st.divider()

    # =================================================================
    # Ejecución — la parte que escribe
    # =================================================================
    st.markdown(
        """
        ### Paso final — Ejecutar en Contabilium ⚙️

        Esta es la parte que **sí escribe** en Contabilium. Por eso es
        **de a una cobranza** y con confirmación.

        **Antes (solo para las de 10%):** en la barra izquierda abrí
        **«🔐 Conexión a Contabilium»**, poné tu **usuario y contraseña**
        de Contabilium y tocá **Conectar**. Es tu login de siempre; queda
        solo en esta sesión, no se guarda. *(Las de pago total sin
        descuento no lo necesitan.)*

        **5) Bajá a «⚙️ Ejecutar en Contabilium».**

        **6) Elegí una cobranza** del desplegable.

        **7) Mirá la vista previa.** Te muestra la factura, la NC del
        10% y el cobro. Si hay **cheque**, confirmá su **número** ahí (con
        el cheque a la vista). Podés abrir "Ver los datos técnicos" para
        el detalle.

        **8) Escribí `CONFIRMAR`** en el recuadro (en mayúsculas). Recién
        ahí se habilita el botón.

        **9) Tocá «🚀 Ejecutar esta cobranza».** La app crea la NC y el
        recibo, imputa todo, y **verifica sola** que la factura y la NC
        queden en **$0**. Te muestra el **número de recibo**.

        Repetí con la siguiente cobranza.
        """
    )

    st.warning(
        "**Empezá con una factura chica** las primeras veces, para "
        "agarrarle la mano con tranquilidad."
    )

    st.divider()

    # =================================================================
    # Qué NO hace la app / casos a mano
    # =================================================================
    st.markdown(
        """
        ### Cosas que la app deja para hacer a mano ✋

        Algunas cobranzas la app **no las ejecuta sola** (aunque las
        apruebes) y te avisa para que las cargues vos:

        - **Un recibo contra varias facturas** (ej. `A-1 / A-2`): no
          sabe cuánto va a cada una.
        - **Entregas / pagos parciales**: cuando el cliente paga solo
          una parte de lo que debe.
        - Cuando el número que puso el vendedor **no es una factura**
          (ej. cargó un número de Nota de Crédito por error).
        - Facturas **no encontradas** en el rango de fechas.

        En todos estos casos vas a ver el motivo en el reporte.
        """
    )

    st.divider()

    # =================================================================
    # Preguntas frecuentes
    # =================================================================
    st.markdown(
        """
        ### Preguntas frecuentes 🤔

        **¿Subir la planilla ya cambia algo en Contabilium?**
        No. Subir y analizar es solo mirar. Nada se crea hasta que hacés
        el paso 8–9 (CONFIRMAR + Ejecutar).

        **¿Por qué me pide usuario y contraseña de Contabilium?**
        Para las cobranzas con 10%, el recibo se crea en Contabilium con
        tu propio usuario (queda registrado a tu nombre). Lo ponés una vez
        al abrir la app; **no se guarda en ningún lado** (solo mientras la
        app está abierta). Si la sesión vence, la app se reconecta sola.

        **Me da error de usuario/contraseña.**
        Es el mismo login con el que entrás a Contabilium. Revisá que esté
        bien escrito. Si seguís sin poder, avisá a Mariano.

        **No encuentra una factura que sé que existe.**
        Seguramente su fecha quedó fuera del rango de búsqueda. Abrí
        **«📅 Rango de fechas de facturas»** en la barra izquierda y
        ampliá el «desde».

        **Se creó la NC pero falló el recibo.**
        La app te muestra el **número de la NC** que quedó suelta, para
        que la anules a mano en Contabilium.

        **¿Puedo deshacer una ejecución?**
        Desde la app no. Como en Contabilium normal, se revierte a mano
        (anulando la NC y el recibo). Por eso conviene ir de a una y
        revisar la vista previa antes de confirmar.
        """
    )

    st.success(
        "Cualquier duda o algo que no cuadre, hablá con Mariano. Mejor "
        "preguntar antes de ejecutar. 🙌"
    )
