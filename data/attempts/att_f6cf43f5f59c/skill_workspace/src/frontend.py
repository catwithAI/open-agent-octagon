"""Streamlit app entry point.

Run with:  streamlit run src/frontend.py

Layout:
  * an editable, unbounded conversation list   (`message_list.py`)
  * a numeric field limited to 0..100          (this module)
  * a SUBMIT button that fires N parallel      (`backend.py`)
    LLM requests, rendered live                (`frontend_render.py`)
"""

from __future__ import annotations

import streamlit as st

import message_list
from backend import launch_requests, use_real_api
from frontend_render import render_responses

# --------------------------------------------------------------------------
# Page scaffolding
# --------------------------------------------------------------------------
st.set_page_config(page_title="OpenAI Response Comparator", page_icon="⚖️", layout="wide")
_css_loaded = False


def _inject_styling() -> None:
    """Load Tailwind from the CDN + the local stylesheet.

    Streamlit's static ``static/`` is not used on purpose: the UI is styled
    via the Tailwind CDN (per the spec) with graceful fallback when offline.
    """
    global _css_loaded
    if _css_loaded:
        return
    _css_loaded = True
    st.html(
        """
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
        /* Local overrides live in src/styles.css; keep a small amount here so
           the page looks decent even before the network stylesheet arrives. */
        .app-shell { font-family: ui-sans-serif, system-ui, sans-serif; }
        </style>
        """
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    _inject_styling()

    st.title("⚖️ OpenAI Response Comparator")
    st.caption(
        "Send the same conversation to the model N times in parallel and watch "
        "the streamed answers diverge — with a zero-cost offline mock by default."
    )

    # 1. Editable conversation ---------------------------------------------
    st.subheader("Conversation")
    message_list.render()

    # 2. Parallelism control ------------------------------------------------
    st.subheader("Parallel requests")
    col_num, col_info = st.columns([2, 4])
    with col_num:
        count = st.number_input(
            "Number of parallel requests (0–100)",
            min_value=0,
            max_value=100,
            value=3,
            step=1,
            key="parallel_count",
        )
    with col_info:
        provider = "OpenAI API" if use_real_api() else "offline mock (no API key needed)"
        st.info(f"Provider: **{provider}**. Responses stream token-by-token.")

    # 3. Submit ---------------------------------------------------------------
    if st.button("SUBMIT", type="primary", key="submit_button", use_container_width=True):
        messages = message_list.to_api_messages()
        if not messages:
            st.warning("Add at least one non-empty message before submitting.")
        elif count == 0:
            st.info("Count is 0 — nothing to send. Set it to 1–100 for a run.")
        else:
            manager = launch_requests(messages, count=int(count))
            st.session_state["last_manager"] = manager

    if st.session_state.get("last_manager") is not None:
        render_responses(st.session_state["last_manager"])


if __name__ == "__main__":
    main()