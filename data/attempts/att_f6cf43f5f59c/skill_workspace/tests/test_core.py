"""Unit tests for the non-UI core (mock, streaming, backend, helpers)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _helpers import chat_messages  # noqa: E402
from backend import STOP, launch_requests  # noqa: E402
from mock_llm import MockLLMStream, stream_tokens  # noqa: E402
from stream_handler import StreamManager, StreamSlot  # noqa: E402


def test_chat_messages_filters_blanks():
    rows = [
        {"role": "user", "content": "  hello  "},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": None},
        {"role": "user", "content": "second"},
    ]
    assert chat_messages(rows) == [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "second"},
    ]


def test_mock_stream_yields_tokens_and_stop():
    conv = [{"role": "user", "content": "Explain concurrency."}]
    MockLLMStream.TOKEN_DELAY_SECONDS = 0
    tokens = list(stream_tokens(conv, request_index=0))
    assert tokens[-1] == STOP
    body = "".join(tokens[:-1]).strip()
    assert len(body) > 40  # a substantive, non-trivial reply
    assert "You asked:" in body


def test_mock_replies_vary_per_request_but_are_reproducible():
    conv = [{"role": "user", "content": "Same question, twice."}]
    MockLLMStream.TOKEN_DELAY_SECONDS = 0
    a1 = "".join(stream_tokens(conv, request_index=1))[:-1]
    a2 = "".join(stream_tokens(conv, request_index=2))[:-1]
    a1_again = "".join(stream_tokens(conv, request_index=1))[:-1]
    assert a1 != a2, "different request indices should produce different replies"
    assert a1 == a1_again, "same request index must be deterministic"


def test_parallel_requests_stream_and_finish():
    conv = [{"role": "user", "content": "Hi"}]
    MockLLMStream.TOKEN_DELAY_SECONDS = 0.001

    def on_token(slot, token):
        pass

    manager = launch_requests(conv, count=5, on_token=on_token)
    assert manager.total == 5
    assert len(manager.slots) == 5

    deadline = time.time() + 10
    while not manager.all_finished:
        assert time.time() < deadline, "streams should finish quickly"
        time.sleep(0.02)

    for i, slot in enumerate(manager.slots, start=1):
        assert slot.ok and not slot.error, f"slot {i}: {slot.error}"
        assert len(slot.text.strip()) > 20


def test_stream_manager_defaults():
    m = StreamManager(total=3)
    assert m.active_count == 3 and m.finished_count() == 0
    s = StreamSlot(0)
    s.append("hel")
    s.append("lo")
    assert s.text == "hello"
    assert not s.is_finished
    s.finish()
    assert s.ok
    m2 = StreamManager(total=1, started_slots=[s])
    assert m2.all_finished and m2.summary() == "1/1 requests completed"


def test_failure_surfaces_in_slot():
    s = StreamSlot(0)
    s.fail("boom")
    assert s.error == "boom"
    assert s.is_finished and not s.ok


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failures.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(tests)
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print(f"FAILURES: {failures}")
        raise SystemExit(1)


if __name__ == "__main__":
    run_all()