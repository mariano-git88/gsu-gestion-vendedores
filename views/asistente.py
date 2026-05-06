"""
views/asistente.py — Tab de chat conversacional con LLM.

Recibe los DataFrames cacheados del dashboard (post-prepare_facturacion)
y la sesión API (para tools que consultan en vivo, como stock). Mantiene
historial de la conversación en st.session_state.

Por simplicidad esta v1 NO tiene streaming. Se puede agregar después
con `client.messages.stream(...)` y `st.write_stream`.
"""

from __future__ import annotations

import streamlit as st

import agent


SESSION_KEY = "asistente_messages"
SESSION_KEY_LOG = "asistente_tool_calls"


def _reset_chat():
    st.session_state[SESSION_KEY] = []
    st.session_state[SESSION_KEY_LOG] = []


def render(df_fc, df_clientes, df_productos, api_session) -> None:
    """Pinta la tab del asistente.

    Args:
      df_fc: DataFrame procesado de facturación (post prepare_facturacion).
        Idealmente el rango más amplio que se haya sincronizado para que
        el asistente pueda responder por períodos pasados.
      df_clientes, df_productos: maestros.
      api_session: ApiSession activa de Contabilium (para tools live como stock).
    """
    st.subheader("🤖 Asistente conversacional")
    st.caption(
        "Preguntale sobre ventas, clientes, stock y vendedores. "
        "Las respuestas usan los datos sincronizados desde la sidebar."
    )

    # API key check.
    api_key = st.secrets.get("anthropic_api_key")
    if not api_key:
        st.error(
            "No está configurada `anthropic_api_key` en secrets. "
            "Agregala en `.streamlit/secrets.toml` (local) o en Streamlit "
            "Cloud → Settings → Secrets."
        )
        return

    # Aviso si no hay datos sincronizados.
    if df_fc is None or df_fc.empty:
        st.info(
            "Antes de preguntar, sincronizá desde la barra lateral. "
            "Sin datos cargados las respuestas van a ser limitadas."
        )

    # Botón de reset.
    cols = st.columns([5, 1])
    with cols[1]:
        if st.button("Limpiar chat", use_container_width=True):
            _reset_chat()
            st.rerun()

    # Inicializar historial.
    if SESSION_KEY not in st.session_state:
        _reset_chat()

    # Ejemplos al inicio.
    if not st.session_state[SESSION_KEY]:
        with st.expander("💡 Ejemplos de preguntas", expanded=True):
            st.markdown(
                """
                - ¿Cuál fue el mes que más se vendió de SILICONAS en 2026?
                - Top 10 clientes del mes pasado.
                - ¿Cuánto stock nos queda del producto con SKU GFX 25?
                - Ranking de vendedores del trimestre.
                - ¿Qué clientes de la cartera de OPMARIO no compraron en los últimos 3 meses?
                """
            )

    # Render del historial. El formato anthropic guarda content como
    # lista de blocks; para mostrar al usuario tomamos solo el texto.
    for msg in st.session_state[SESSION_KEY]:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            # content puede ser string (input usuario) o lista (tool_result).
            if isinstance(content, str):
                with st.chat_message("user"):
                    st.markdown(content)
            # Los tool_results no se muestran al usuario (son ruido).
        elif role == "assistant":
            # content es lista de blocks (text + tool_use).
            if isinstance(content, list):
                texts = []
                for b in content:
                    if hasattr(b, "type") and b.type == "text":
                        texts.append(b.text)
                    elif isinstance(b, dict) and b.get("type") == "text":
                        texts.append(b["text"])
                if texts:
                    with st.chat_message("assistant"):
                        st.markdown("\n\n".join(texts))

    # Input del usuario.
    user_msg = st.chat_input("Hacé tu pregunta...")
    if user_msg:
        # Agregar al historial.
        st.session_state[SESSION_KEY].append({"role": "user", "content": user_msg})
        with st.chat_message("user"):
            st.markdown(user_msg)

        # Llamar al agent.
        ctx = {
            "df_fc": df_fc,
            "df_clientes": df_clientes,
            "df_productos": df_productos,
            "api_session": api_session,
        }
        with st.chat_message("assistant"):
            with st.spinner("Pensando..."):
                try:
                    text, tool_log = agent.responder(
                        st.session_state[SESSION_KEY],
                        ctx,
                        api_key=api_key,
                    )
                    # responder() ya muta messages in-place (agregó assistant + tool rounds).
                    st.session_state[SESSION_KEY_LOG].extend(tool_log)
                    st.markdown(text)
                except Exception as exc:
                    st.error(f"Error del asistente: {exc}")

    # Panel de debug expandible al fondo.
    if st.session_state.get(SESSION_KEY_LOG):
        with st.expander(f"🔍 Tool calls de esta sesión ({len(st.session_state[SESSION_KEY_LOG])})", expanded=False):
            for i, tc in enumerate(st.session_state[SESSION_KEY_LOG], 1):
                st.markdown(f"**{i}. `{tc['tool']}`**")
                st.json(tc["input"])
                st.code(tc["result_preview"], language="json")
                st.markdown("---")
