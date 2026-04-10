"""
auth.py — Login con contraseña única para el dashboard.

Una sola password compartida, sin usuarios individuales, sin recuperación.
La password vive en st.secrets["app_password"]:
  - Localmente en .streamlit/secrets.toml
  - En producción en Streamlit Cloud → Settings → Secrets

Para cambiar la password en producción: editar el secret en el dashboard
de Streamlit Cloud y la app se redeploya sola.

Diferencia con el proyecto anterior (Liquidación de Comisiones):
- Aquel tenía un diccionario de usuarios con sus passwords (multi-user).
- Este tiene una sola password compartida (es lo que pide el manual:
  "contraseña única compartida").
"""

import hmac

import streamlit as st


def _verify(stored_password: str, entered_password: str) -> bool:
    """Comparación constante en el tiempo (anti timing attacks)."""
    return hmac.compare_digest(stored_password, entered_password)


def check_password() -> bool:
    """
    Devuelve True si el usuario está autenticado en esta sesión.

    Si no, pinta el formulario de login y devuelve False. El llamador
    debe `st.stop()` cuando reciba False, así no se renderiza nada del
    resto del app.
    """
    if st.session_state.get("authenticated", False):
        return True

    # Pantalla de login centrada
    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Gestión de Vendedores</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Dashboard semanal del Jefe de Ventas de GSU. "
            "Acceso restringido a personal autorizado de Suprabond."
        )

        with st.form("login_form", clear_on_submit=False):
            password = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)

        if submit:
            stored = st.secrets.get("app_password")
            if stored is None:
                st.error(
                    "El administrador del sitio aún no configuró la "
                    "contraseña en Streamlit Cloud. Avisar a Mariano."
                )
                return False

            if _verify(stored, password):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")

    return False


def logout_button() -> None:
    """
    Renderiza un botón de logout en la sidebar. Llamar después de
    `check_password() == True`.
    """
    if not st.session_state.get("authenticated"):
        return
    with st.sidebar:
        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.pop("authenticated", None)
            st.rerun()
