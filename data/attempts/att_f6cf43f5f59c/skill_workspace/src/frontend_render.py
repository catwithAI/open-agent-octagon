"""Rendering of the streamed responses.

On SUBMIT the backend returns a :class:`StreamManager` with one slot per
parallel request.  This module draws an expandable element per request
("Request 1", "Request 2", …), then polls the slots ~20×/second so the text
inside each expander grows token-by-token, in real time.
"""

from __future__ import annotations

import html
import time

import streamlit as st

from stream_handler import StreamManager

_POLL_SECONDS = 0.05  # redraw interval → real-time token streaming


def _format_token_text(raw: str) -> str:
    """Render raw streamed text as safe, preformatted markdown."""
    return html.escape(raw).replace("\n", "\n\n")


def render_responses(manager: StreamManager) -> None:
    """Draw one expandable card per request and stream tokens into them."""
    st.markdown("### Responses")

    holders = []
    for i, slot in enumerate(manager.slots, start=1):
        with st.expander(f"Request {i}", expanded=(i == 1)):
            placeholder = st.empty()
            holders.append(placeholder)

    progress = st.progress(0.0, text="Waiting for responses…")

    # Poll loop: redraw every slot as its worker thread streams more tokens.
    while not manager.all_finished or any(h is not None for h in holders):
        for i, (slot, holder) in enumerate(zip(manager.slots, holders)):
            if slot.error:
                holder.error(slot.error)
                holders[i] = None
            elif slot.text:
                holder.markdown(_format_token_text(slot.text))
        progress.progress(
            manager.finished_count() / max(manager.total, 1),
            text=manager.summary(),
        )
        if manager.all_finished:
            break
        time.sleep(_POLL_SECONDS)

    progress.progress(1.0, text="All responses complete.")
    for i, (slot, holder) in enumerate(zip(manager.slots, holders)):
        if slot.error:
            holder.error(slot.error)
        elif slot.text:
            holder.markdown(_format_token_text(slot.text))
    st.success(manager.summary())