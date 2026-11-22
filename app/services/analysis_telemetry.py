"""Run telemetry (Phase 13.3): request identity, response metadata and hashes.

Storage: the existing ``AnalysisRun.scope`` JSON under the versioned
``telemetry`` key — **no schema or migration change** (the field-gap analysis is
recorded in docs/phase13-verification.md, §Phase 13.3). Historical runs without
telemetry read as "unrecorded" and are never backfilled or rewritten.

Hash canonicalisation (identification only — never a signature or audit proof):

* canonical JSON: ``ensure_ascii=False, sort_keys=True, separators=(",", ":")``;
* UTF-8 bytes are hashed with SHA-256 (hex);
* ``None`` and ``""`` remain distinct;
* volatile values are excluded: ``analysis_run_id`` and ``generated_at``;
* arrays keep their order (never sorted).
"""
import hashlib
import json

from app.domain.entities.analysis_result import SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION
from app.services.llm.provider import ChatRequest
from app.services.llm.response_metadata import finish_reason, usage_summary
from app.services.prompt import ANALYSIS_GOAL, OUTPUT_TEMPLATE, SYSTEM_PROMPT

#: Version of the telemetry payload itself (bump on incompatible layout changes).
TELEMETRY_VERSION = "1"
#: Version of the static prompt template; bump when the prompt text meaningfully
#: changes (13.1 JSON output contract = v2).
PROMPT_TEMPLATE_VERSION = "2"
#: Key inside the run's scope JSON; unknown to older readers, ignored by them.
SCOPE_KEY = "telemetry"


# ------------------------------------------------------------------ canonical
def canonical_json(value) -> str:
    """Deterministic JSON used for every hash in this module."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _short(value, limit: int = 200):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


# -------------------------------------------------------------- hash builders
def prompt_template_hash() -> str:
    """SHA-256 of the static prompt contract (system + output + goals)."""
    return sha256_hex(canonical_json({"system": SYSTEM_PROMPT, "output": OUTPUT_TEMPLATE,
                                      "goals": ANALYSIS_GOAL}))


def context_snapshot_hash(snapshot) -> str:
    """SHA-256 of what the model was given, excluding volatile run-local values."""
    scope = {key: value for key, value in (snapshot.scope or {}).items()
             if key != "analysis_run_id"}
    projection = {
        "target_type": snapshot.target_type,
        "target_id": str(snapshot.target_id) if snapshot.target_id else None,
        "analysis_type": snapshot.analysis_type,
        "scope": scope,
        "sections": [{"key": section.key, "title": section.title, "kind": section.kind,
                      "note": section.note, "lines": list(section.lines)}
                     for section in snapshot.sections],
        "rendered_context": snapshot.rendered_context,
    }
    return sha256_hex(canonical_json(projection))


def request_payload_hash(request: ChatRequest) -> str:
    """SHA-256 of the final message payload (model + messages + parameters)."""
    return sha256_hex(canonical_json(request.payload()))


# ------------------------------------------------------------- telemetry build
def _identity(*, provider_name, requested_model) -> dict:
    return {"version": TELEMETRY_VERSION,
            "request": {"provider_name": _short(provider_name),
                        "requested_model": _short(requested_model),
                        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                        "prompt_hash": prompt_template_hash()}}


def build_partial_request_telemetry(*, provider_name, requested_model) -> dict:
    """Request identity available even when the context/request could not be built."""
    return _identity(provider_name=provider_name, requested_model=requested_model)


def build_request_telemetry(request: ChatRequest, *, provider_name, requested_model,
                            snapshot) -> dict:
    """Full request identity: prompt/context/request hashes + input estimate."""
    telemetry = _identity(provider_name=provider_name, requested_model=requested_model)
    telemetry["request"].update({
        "context_snapshot_hash": context_snapshot_hash(snapshot),
        "request_hash": request_payload_hash(request),
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        # Deliberately separate from provider-reported usage (Phase 13.3 / 9.5).
        "estimated_context_tokens": snapshot.estimated_tokens,
    })
    return telemetry


def _execution(*, status, duration_ms, attempts: int | None, error_class=None) -> dict:
    # ``attempts`` is the real number of provider calls; None means the caller
    # cannot prove it (e.g. cancellation) and is stored as null, never as 0.
    retry_count = None if attempts is None else max(0, int(attempts) - 1)
    block = {"status": status, "duration_ms": max(0, int(duration_ms)),
             "retry_count": retry_count}
    if error_class:
        block["error_class"] = error_class
    return block


def complete_telemetry(telemetry: dict, response, *, attempts: int, duration_ms: int,
                       status: str = "SUCCEEDED") -> dict:
    """Merge provider response metadata into the run telemetry."""
    merged = {**telemetry, "request": dict(telemetry.get("request") or {})}
    merged["response"] = {"returned_model": _short(response.model),
                          "finish_reason": finish_reason(response.finish_reason),
                          "usage": usage_summary(response.usage),
                          "provider_request_id": _short(response.raw_id)}
    merged["execution"] = _execution(status=status, duration_ms=duration_ms,
                                     attempts=attempts)
    return merged


def fail_telemetry(telemetry: dict | None, *, status: str, duration_ms: int,
                   error_class: str, attempts: int | None) -> dict:
    """Execution metadata for a failed/cancelled run (no fabricated response).

    ``attempts`` is the actual provider-call count when known (``1`` for a
    non-retryable first failure, ``N`` for exhausted retries); ``None`` when the
    path cannot prove it, which is stored as ``retry_count: null`` (unavailable)
    instead of a misleading ``0``.
    """
    merged = dict(telemetry or {"version": TELEMETRY_VERSION})
    merged["execution"] = _execution(status=status, duration_ms=duration_ms,
                                     attempts=attempts,
                                     error_class=_short(error_class, 80))
    return merged


# ------------------------------------------------------------------ read side
def scope_with_telemetry(scope: dict, telemetry: dict | None) -> dict:
    """Return the scope JSON value carrying the (versioned) telemetry block."""
    updated = dict(scope)
    if telemetry is not None:
        updated[SCOPE_KEY] = telemetry
    return updated


def telemetry_from_scope(scope) -> dict | None:
    """Parse a stored telemetry block; ``None`` for old/foreign records."""
    if not isinstance(scope, dict):
        return None
    telemetry = scope.get(SCOPE_KEY)
    if not isinstance(telemetry, dict):
        return None
    if not isinstance(telemetry.get("version"), str) or not telemetry["version"].strip():
        return None
    return telemetry
