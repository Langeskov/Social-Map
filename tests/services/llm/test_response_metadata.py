"""Response metadata normalisation (Phase 13.3 / 9.5, 9.6).

Usage is never invented: missing fields stay ``None`` and ``available`` is False —
no 0, no summed total. Finish reasons keep the raw provider string and gain only a
small normalised category; a non-stop vendor value is not treated as success.
"""
from app.services.llm.response_metadata import (FINISH_CONTENT_FILTER, FINISH_LENGTH,
                                                FINISH_OTHER, FINISH_STOP,
                                                FINISH_TOOL_CALLS, FINISH_UNAVAILABLE,
                                                finish_reason, usage_summary)


# ----------------------------------------------------------------- finish reason
def test_finish_reason_categories():
    assert finish_reason("stop") == {"raw": "stop", "category": FINISH_STOP}
    assert finish_reason("length")["category"] == FINISH_LENGTH
    assert finish_reason("content_filter")["category"] == FINISH_CONTENT_FILTER
    assert finish_reason("tool_calls")["category"] == FINISH_TOOL_CALLS
    assert finish_reason("function_call")["category"] == FINISH_TOOL_CALLS
    # Case/whitespace tolerant, raw value preserved.
    assert finish_reason("  STOP ") == {"raw": "STOP", "category": FINISH_STOP}


def test_unknown_and_missing_finish_reasons_are_not_success():
    assert finish_reason("safety") == {"raw": "safety", "category": FINISH_OTHER}
    assert finish_reason(None) == {"raw": None, "category": FINISH_UNAVAILABLE}
    assert finish_reason("")["category"] == FINISH_UNAVAILABLE
    assert finish_reason("   ")["category"] == FINISH_UNAVAILABLE
    # A non-string provider value is preserved as text, never silently dropped.
    assert finish_reason(42) == {"raw": "42", "category": FINISH_UNAVAILABLE}


def test_finish_reason_raw_text_is_bounded():
    raw = "x" * 500
    result = finish_reason(raw)
    assert result["category"] == FINISH_OTHER
    assert len(result["raw"]) == 80


# ----------------------------------------------------------------------- usage
def test_full_usage_is_recorded():
    usage = usage_summary({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert usage == {"available": True, "prompt_tokens": 10, "completion_tokens": 5,
                     "total_tokens": 15}


def test_missing_usage_is_unavailable_not_zero():
    for value in (None, {}, [], "n/a"):
        usage = usage_summary(value)
        assert usage == {"available": False, "prompt_tokens": None, "completion_tokens": None,
                         "total_tokens": None}


def test_partial_usage_keeps_missing_fields_none():
    usage = usage_summary({"completion_tokens": 7})
    assert usage["available"] is True
    assert usage["prompt_tokens"] is None and usage["total_tokens"] is None
    assert usage["completion_tokens"] == 7


def test_input_output_spelling_is_normalised_not_derived():
    usage = usage_summary({"input_tokens": 8, "output_tokens": 3, "total_tokens": 11})
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (8, 3, 11)
    # No total is synthesised from the parts.
    derived = usage_summary({"prompt_tokens": 8, "completion_tokens": 3})
    assert derived["total_tokens"] is None
    assert derived["available"] is True


def test_invalid_numbers_are_ignored_not_coerced():
    usage = usage_summary({"prompt_tokens": -1, "completion_tokens": True,
                           "total_tokens": "15", "input_tokens": 4})
    assert usage["prompt_tokens"] == 4          # valid input_tokens wins after invalid keys
    assert usage["completion_tokens"] is None
    assert usage["total_tokens"] is None
    assert usage["available"] is True
