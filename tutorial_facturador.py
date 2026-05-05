"""
tutorial_facturador.py — Contenido del tutorial del módulo de Facturación
Masiva.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click en
el botón "Tutorial" del sidebar de `facturador_app.py`. Está pensado
para que cualquier persona del equipo de Suprabond:

  1. Entienda qué hace el módulo y cuándo usarlo.
  2. Pueda configurar de cero el Google Sheet de log si todavía no
     existe (el setup es one-shot, ~5 minutos).
  3. Sepa qué significa cada sección de la app y cómo emitir un lote.
  4. Sepa qué hacer si algo falla (errores parciales, borradores
     colgados, casos de "ya facturada vía API", etc.).

El contenido vive solo en este módulo. Si hay que actualizarlo, se edita
acá sin tocar `facturador_app.py`.
"""

import streamlit as st


def render() -> None:
    """Renderiza el contenido completo del tutorial dentro del modal."""

    # =================================================================
    # Intro
    # =================================================================
    st.markdown(
        """
        ### ¿Qué hace este módulo?

        **Emite facturas electrónicas masivamente** desde órdenes de
        venta de Contabilium UY. Reemplaza el flujo manual de "entrar
        a cada orden, elegir condición de pago, facturar e imprimir"
        por un solo run que procesa decenas o centenares de órdenes
        en minutos.

        Internamente usa el workflow oficial REST de Contabilium en 3
        pasos: `POST /comprobantes/crear` → `GET /comprobantes/emitirFE`
        → `GET /comprobantes/obtenerPdf/`. Cada factura sale con CAE
        válido y QR DGI — son **legales y fiscales**, idénticas a
        las emitidas desde la UI Web.
        """
    )

    st.divider()

    # =================================================================
    # Setup del Google Sheet — lo importante por primera vez
    # =================================================================
    st.markdown(
        """
        ### Setup por primera vez — Google Sheet de log

        Cada emisión (exitosa o fallida) se loguea en un Google Sheet
        para que tengas un historial auditable independiente de la app.
        Esto es **opcional**: si no lo configurás, el módulo igual
        funciona y podés descargar el reporte CSV después de cada run,
        pero perdés el historial acumulado.

        El Sheet debe ser **distinto del de Comisiones** (separamos
        dominios: comisiones vs facturación). Pasos para configurarlo:
        """
    )

    with st.expander("📋 Paso 1 — Crear el Google Sheet", expanded=False):
        st.markdown(
            """
            1. Abrí Google Drive con tu cuenta de trabajo.
            2. Click en **+ Nuevo** → **Hojas de cálculo de Google**.
            3. Ponele un nombre claro: `gsu-facturacion-log` o
               equivalente.
            4. Copiá el **ID del Sheet** desde la URL:
               ```
               https://docs.google.com/spreadsheets/d/<ID_DEL_SHEET>/edit
               ```
               (el `<ID_DEL_SHEET>` es la parte larga entre `/d/` y `/edit`).

            **No** crees ninguna pestaña ni encabezado a mano — el
            módulo crea automáticamente la pestaña `log_facturacion`
            con el header correcto la primera vez que escribe.
            """
        )

    with st.expander("📋 Paso 2 — Compartir con el Service Account", expanded=False):
        st.markdown(
            """
            El Sheet tiene que ser editable por el Service Account de
            Google Cloud que la app usa para escribir. Si ya tenés
            configurado el Service Account para Comisiones, **es el
            mismo** — reutilizalo.

            1. Abrí el Sheet recién creado.
            2. Click en **Compartir** (botón azul arriba a la derecha).
            3. En "Agregar personas y grupos", pegá el **client_email**
               del Service Account. Lo encontrás en el JSON del SA o
               en Streamlit Cloud → Secrets del módulo Comisiones, en
               el bloque `[gsheets.service_account]` clave `client_email`.
               Termina en `@<proyecto>.iam.gserviceaccount.com`.
            4. Asignar permiso **Editor**.
            5. Destildar "Notificar a las personas" (no es un humano).
            6. Click **Compartir**.

            > Si **no** tenés Service Account todavía (porque no usaste
            > Comisiones aún), seguí los pasos del tutorial de Comisiones
            > para crearlo, o pedile a Mariano el JSON del SA existente.
            > La parte de "habilitar Google Sheets API + Google Drive API"
            > en GCP es obligatoria — falla con `PermissionError` raro
            > sino (lección anotada en `_learning/errors.md`).
            """
        )

    with st.expander("📋 Paso 3 — Configurar secrets (local + Cloud)", expanded=False):
        st.markdown(
            """
            **Local** (para correr `streamlit run facturador_app.py` en
            tu máquina): editar `.streamlit/secrets.toml` y agregar:

            ```toml
            [gsheets_facturacion]
            spreadsheet_id = "PEGAR_AQUI_EL_ID_COPIADO_EN_PASO_1"

            # Modo A — apuntando al .json del SA en disco (recomendado local):
            service_account_json_path = ".gsheets/sa.json"

            # Modo B — embebido (NO se usa en local, sí en Cloud):
            # [gsheets_facturacion.service_account]
            #   type = "service_account"
            #   ... (todas las claves del JSON del SA)
            ```

            **Producción** (Streamlit Cloud → app del facturador →
            Settings → Secrets):

            Streamlit Cloud no tiene filesystem persistente, así que el
            JSON del SA va embebido. Pegá el bloque completo:

            ```toml
            [gsheets_facturacion]
            spreadsheet_id = "PEGAR_AQUI_EL_ID_COPIADO_EN_PASO_1"

            [gsheets_facturacion.service_account]
            type = "service_account"
            project_id = "..."
            private_key_id = "..."
            private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
            client_email = "...@....iam.gserviceaccount.com"
            client_id = "..."
            auth_uri = "https://accounts.google.com/o/oauth2/auth"
            token_uri = "https://oauth2.googleapis.com/token"
            auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
            client_x509_cert_url = "..."
            universe_domain = "googleapis.com"
            ```

            **Tip**: si ya pegaste el bloque `[gsheets.service_account]`
            para el deploy de Comisiones, el contenido es **idéntico**
            (mismo SA). Solo cambia el header del bloque (de `gsheets`
            a `gsheets_facturacion`) y el `spreadsheet_id`. Podés copiar
            y pegar y reemplazar.

            Después del save de Secrets, Streamlit Cloud reinicia el
            app automáticamente.
            """
        )

    with st.expander("📋 Paso 4 — Verificar que escribe", expanded=False):
        st.markdown(
            """
            1. Hacé un run masivo chico (1 sola orden de prueba).
            2. Cuando termine, debería aparecer:
               > ✅ Run completado. 1 órdenes procesadas. Log guardado
               > en Google Sheet (1 filas).
            3. Abrí el Sheet en Drive — debería existir una pestaña
               nueva llamada `log_facturacion` con el header arriba y
               la primera fila con los datos de tu run.

            Si **no aparece la pestaña** o sale warning amarillo "NO
            pude guardar el log en Sheet: ...", revisá:

            - ¿El Service Account está agregado como Editor?
            - ¿El `spreadsheet_id` en secrets es exactamente el que
              copiaste de la URL? (sin `/edit`, sin espacios).
            - ¿Las APIs de Google Sheets **y Drive** están habilitadas
              en el proyecto GCP del Service Account? Necesitás las
              **dos**.
            """
        )

    st.divider()

    # =================================================================
    # Cómo usar el flujo (después del setup)
    # =================================================================
    st.markdown(
        """
        ### Cómo emitir un lote

        El flujo está diseñado para tener **muchas oportunidades de
        revertir** antes de comprometer una emisión fiscal:
        """
    )

    st.markdown(
        """
        1. **Sidebar** — Configurá:
           - **Rango de fechas**: período de creación de las órdenes
             a procesar. Default: día 1 del mes anterior → hoy.
           - **Condición de venta**: el plazo a aplicar a TODO el lote
             (ej. "30 Cuenta Corriente"). Si tenés órdenes con
             condiciones distintas, hacelas en lotes separados.
           - **Punto de venta** y **Depósito**: idem, único por lote.
        2. Click **Buscar pendientes**. Tarda 30-90s la primera vez
           (paginar órdenes + paginar facturas existentes para detectar
           ya-facturadas + GET por orden para detectar línea libre).
        3. **Revisá las 3 secciones**:
           - **Tabla principal**: facturables vía API. Esas son las que
             podés seleccionar.
           - **Expander "Ya facturadas vía API"**: órdenes que ya
             tienen comprobante emitido por este módulo en alguna run
             previa. **No se pueden re-facturar.**
           - **Expander "No facturables: línea libre"**: órdenes con
             ítems sin código de producto. La API REST no las acepta;
             facturalas manualmente desde la UI Web de Contabilium.
        4. **Marcá las órdenes** que querés facturar. Vas viendo el
           contador "N órdenes seleccionadas • Total a facturar".
        5. Tipeá `FACTURAR` en el input de confirmación. El botón
           "Emitir N facturas" se habilita.
        6. Click. Empieza el run **secuencial** (sin concurrencia, lo
           prohíbe Contabilium para emisión electrónica) con progress
           bar. Por cada orden vas viendo el status incremental.
        7. Al terminar: tabla de resultado con CAE, número de factura
           y link al QR DGI por cada exitosa, mensaje de error claro
           por cada fallida. Botón para descargar CSV.
        """
    )

    st.divider()

    # =================================================================
    # Caveats importantes
    # =================================================================
    st.markdown(
        """
        ### Caveats importantes

        - **La factura es legal e irreversible**. Salvo nota de
          crédito, una vez que el módulo recibió CAE de DGI no hay
          forma de "deshacerla". Por eso el gate de tipeo `FACTURAR`
          y el resumen previo.

        - **La orden de venta NO cambia de estado en Contabilium**.
          Esto es un caveat conocido y bloqueante del lado de
          Contabilium (ya pedimos al soporte que lo arregle). Las
          órdenes facturadas **vía API** siguen apareciendo como
          "Pendientes" en la UI Web, aunque su factura legal exista.
          El módulo lo compensa con el filtro anti-doble-facturación
          basado en `RefExterna` (el ID de la orden queda grabado en
          el comprobante). Por eso es importante NO facturar la misma
          orden manualmente desde la UI Web después — el operador
          podría no darse cuenta.

        - **Run secuencial = no rapidísimo**. Throttling UY: 15
          requests cada 10 segundos. Cada orden requiere 3 requests
          (crear + emitir + pdf). Calculá ~2-3 segundos por orden.
          Para 100 órdenes son ~5 minutos. Es lo más rápido que
          Contabilium permite sin bloquearte por IP.

        - **Borradores colgados**: si una orden falla en `emitirFE`
          después de un `crear` exitoso, queda un borrador con número
          `FAC A-00000000` en Contabilium. **El módulo lo limpia
          automáticamente** (DELETE) — no tenés que hacer nada manual.
          Si por alguna razón quedó alguno, se borra desde la UI o
          via el smoke `_exploracion-api-contabilium/smoke_emision_oficial_9.py borrar <id>`.

        - **Errores parciales no abortan el run**: si la orden 5 de 20
          falla, las 6-20 siguen procesándose. Al final ves un reporte
          completo con qué pasó con cada una.

        - **Línea libre = no facturable vía API**. Las órdenes con
          ítems sin `IdConcepto` del catálogo no pasan el handler de
          Contabilium (responde HTTP 500). El módulo las detecta
          antes y las descarta del bucket facturable. Hay que
          facturarlas manualmente o convertir el ítem a un concepto
          del catálogo.
        """
    )

    st.divider()

    # =================================================================
    # Cierre
    # =================================================================
    st.markdown(
        """
        ### ¿Algo no funciona como esperás?

        Reportarlo a Mariano. La memoria del proyecto está en
        `_learning/decisions.md`, `_learning/errors.md` y el manual
        operativo `claude.md.txt` — son los archivos que tiene que
        leer cualquier persona (humano o asistente) que abra el repo
        por primera vez.

        El módulo se construyó en la sesión 13 (2026-05-05); todos
        los hallazgos del discovery están documentados ahí.
        """
    )
