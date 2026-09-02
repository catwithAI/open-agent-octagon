"""End-to-end app flow test using Streamlit's AppTest harness.

Drives frontend.py the same way a browser would: edits the conversation text
area, sets the parallel count, clicks SUBMIT, and asserts expandable response
elements render for every request.  Runs fully offline on the mock provider.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamlit.testing.v1 import AppTest  # noqa: E402

import mock_llm  # noqa: E402

mock_llm.TOKEN_DELAY_SECONDS = 0.002  # keep the harness fast

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = str(ROOT / "src" / "frontend.py")


def _submit(at, text, count):
    """Fill the first text area and the parallel-count field, then click SUBMIT."""
    at.text_area[0].set_value(text)
    at.number_input[0].set_value(count)
    at.run()
    at.button[1].click().run()  # button[0] is '＋ Add message', [1] is SUBMIT


def test_full_submit_flow():
    at = AppTest.from_file(FRONTEND, default_timeout=30)
    at.run()

    # The app must boot with exactly one editable message row.
    assert len(at.text_area) == 1
    assert len(at.selectbox) == 1

    _submit(at, "Tell me three different summaries of concurrency.", 3)

    # Expanders labelled "Request N" should appear, one per parallel request.
    labels = [e.label for e in at.expander]
    assert "Request 1" in labels and "Request 2" in labels and "Request 3" in labels

    # The submission blocks until the streams complete, so the expanders must
    # already hold the full streamed replies.
    total = sum(len(m.value) for e in at.expander for m in e.markdown)
    assert total > 50, f"expected streamed content inside expanders, got {total} chars"


def test_add_message_grows_the_list():
    at = AppTest.from_file(FRONTEND, default_timeout=30)
    at.run()
    at.button[0].click().run()  # '＋ Add message'
    assert len(at.text_area) == 2
    assert len(at.selectbox) == 2


def test_zero_count_shows_info():
    at = AppTest.from_file(FRONTEND, default_timeout=30)
    at.run()
    _submit(at, "hi", 0)
    # No expanders should be created for count == 0.
    assert len(at.expander) == 0


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(tests)
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    run_all()