"""Backend: dispatches the conversation to the LLM provider N times in parallel.

Every request runs in its own worker thread.  Tokens stream straight from the
provider (real OpenAI SSE, or the offline mock) into a ``StreamSlot`` via
:func:`on_token`, so the frontend can render them as they arrive.

Provider selection:
  * If ``USE_REAL_OPENAI`` is truthy **and** ``OPENAI_API_KEY`` is present, the
    real OpenAI ``chat.completions`` streaming API is used.
  * Otherwise the deterministic offline mock (``mock_llm.py``) is used, so the
    whole app works with zero credentials and zero cost (see REPORT.md).
"""

from __future__ import annotations

import os
import threading
from typing import Callable, List, Optional

from stream_handler import StreamManager, StreamSlot

# Streaming sentinel emitted by the token generators when the reply ends.
STOP = "<|mock_stop|>"


def use_real_api() -> bool:
    """Whether the real OpenAI API should be used instead of the mock."""
    return os.getenv("USE_REAL_OPENAI", "").lower() in {"1", "true", "yes"} and bool(
        os.getenv("OPENAI_API_KEY")
    )


def _real_openai_tokens(model: str, messages: List[dict]) -> "iter":
    """Stream answer tokens from the real OpenAI API (chat completions SSE)."""
    from openai import OpenAI  # imported lazily so the app runs offline

    client = OpenAI()
    stream = client.chat.completions.create(model=model, messages=messages, stream=True)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _mock_openai_tokens(messages: List[dict], request_index: int) -> "iter":
    """Stream answer tokens from the offline deterministic mock."""
    from mock_llm import stream_tokens

    yield from stream_tokens(messages, request_index=request_index)


def _produce(
    slot: StreamSlot,
    messages: List[dict],
    request_index: int,
    model: str,
    on_token: Optional[Callable[[StreamSlot, str], None]],
) -> None:
    """Worker body: pull tokens from the provider and feed them to the slot."""
    generator = _real_openai_tokens(model, messages) if use_real_api() else _mock_openai_tokens(
        messages, request_index
    )
    try:
        for token in generator:
            if token == STOP:  # end-of-stream sentinel from the mock
                break
            slot.append(token)
            if on_token is not None:
                on_token(slot, token)
    except Exception as exc:  # surface provider failures in the UI slot
        slot.fail(f"Request {request_index + 1} failed: {exc}")
    finally:
        slot.finish()


def launch_requests(
    messages: List[dict],
    count: int,
    model: str = "gpt-4o-mini",
    on_token: Optional[Callable[[StreamSlot, str], None]] = None,
) -> StreamManager:
    """Launch *count* parallel requests for *messages*.

    Returns a :class:`StreamManager` that is populated immediately by worker
    threads; the caller polls it and redraws the UI until everything finishes.

    Arguments:
        messages: OpenAI-style ``[{"role": ..., "content": ...}, ...]`` list
            representing the full conversation.
        count: how many times to run the request in parallel (0..100).
        model: OpenAI model name, only consulted when the real API is used.
        on_token: optional callback invoked once per streamed token.
    """
    manager = StreamManager(total=count)
    threads = [
        threading.Thread(
            target=_produce,
            args=(slot, messages, slot.request_index, model, on_token),
            daemon=True,
            name=f"request-{slot.request_index + 1}",
        )
        for slot in manager.slots
    ]
    for t in threads:
        t.start()
    return manager