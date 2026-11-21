"""Normalisation of provider response metadata (Phase 13.3).

OpenAI-compatible providers disagree about usage keys and finish reasons. This
module keeps the **raw** provider value untouched and derives a small, explicit
normalised view:

* a missing usage field stays ``None`` and ``available`` becomes ``False`` — no
  field is ever filled with 0 and no total is invented;
* ``prompt_tokens`` / ``completion_tokens`` also accept the ``input_tokens`` /
  ``output_tokens`` spelling some gateways use (rename only, never derived);
* finish reasons map to a small business-neutral category while the raw string is
  preserved, because vendor-specific reasons are not a universal status.
"""
from typing import Mapping

FINISH_STOP = "STOP"
FINISH_LENGTH = "LENGTH"
FINISH_CONTENT_FILTER = "CONTENT_FILTER"
FINISH_TOOL_CALLS = "TOOL_CALLS"
FINISH_OTHER = "OTHER"
FINISH_UNAVAILABLE = "UNAVAILABLE"
FINISH_CATEGORIES = (FINISH_STOP, FINISH_LENGTH, FINISH_CONTENT_FILTER,
                     FINISH_TOOL_CALLS, FINISH_OTHER, FINISH_UNAVAILABLE)

_FINISH_BY_RAW = {"stop": FINISH_STOP, "length": FINISH_LENGTH,
                  "content_filter": FINISH_CONTENT_FILTER,
                  "tool_calls": FINISH_TOOL_CALLS, "function_call": FINISH_TOOL_CALLS}


def finish_reason(raw) -> dict:
    """``{"raw": <provider value or None>, "category": <normalised category>}``."""
    if raw is None:
        return {"raw": None, "category": FINISH_UNAVAILABLE}
    if not isinstance(raw, str) or not raw.strip():
        # A non-string provider value is preserved as text, never dropped silently.
        return {"raw": str(raw)[:80], "category": FINISH_UNAVAILABLE}
    text = raw.strip()
    return {"raw": text[:80], "category": _FINISH_BY_RAW.get(text.lower(), FINISH_OTHER)}


def _number(usage, *keys):
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def usage_summary(usage) -> dict:
    """Provider-reported token usage, never synthesised.

    Returns ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` (each
    ``int | None``) plus ``available``. Missing values are ``None``; no total is
    computed from the parts.
    """
    empty = {"available": False, "prompt_tokens": None, "completion_tokens": None,
             "total_tokens": None}
    if not isinstance(usage, Mapping):
        return empty
    prompt = _number(usage, "prompt_tokens", "input_tokens")
    completion = _number(usage, "completion_tokens", "output_tokens")
    total = _number(usage, "total_tokens")
    if prompt is None and completion is None and total is None:
        return empty
    return {"available": True, "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total}
