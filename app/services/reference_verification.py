"""Deterministic reference verification (Phase 13.2 / Source Grounding).

One question only: is a citation *legitimate*? — does the cited entity exist, in
this workspace, as part of this run's recorded sources, and was it actually
exposed in the Context Entity Manifest (and with which exposure level)?
Whether the cited material semantically supports the sentence is **not** decided
here; that stays a human judgement.

The same statuses drive `MemoryService.candidate_references`, Save-as-Memory
review and the offline evaluation runner, so no caller can invent a different
"accepted" rule.
"""
import json
from uuid import UUID

from app.domain.entities.reference_validation import (CITABLE_TYPES,
                                                      DELETED_SOURCE,
                                                      INVALID_FORMAT,
                                                      MARKER_PATTERN,
                                                      NOT_IN_RUN,
                                                      SOURCE_NOT_EXPOSED,
                                                      TYPE_MISMATCH,
                                                      UNKNOWN_SOURCE,
                                                      VALID_FULL,
                                                      VALID_LIMITED,
                                                      WORKSPACE_MISMATCH,
                                                      EntryReferenceCheck,
                                                      ValidatedReference)
from app.services.entity_manifest import (CONTENT_INCLUDED, EVENT_DETAIL,
                                          EVENT_REFERENCE, IDENTITY_ONLY,
                                          METADATA_ONLY, PARTICIPANT_CONTEXT,
                                          PERSON, PROFILE, RELATIONSHIP,
                                          RELATIONSHIP_FACT)

#: Which exposure level makes a citation "full" versus "limited".
_STATUS_BY_EXPOSURE = {
    (PERSON, PROFILE): VALID_FULL,
    (PERSON, IDENTITY_ONLY): VALID_LIMITED,
    (PERSON, PARTICIPANT_CONTEXT): VALID_LIMITED,
    (RELATIONSHIP, RELATIONSHIP_FACT): VALID_FULL,
    ("EVENT", EVENT_DETAIL): VALID_FULL,
    ("EVENT", EVENT_REFERENCE): VALID_LIMITED,
    ("EVIDENCE", CONTENT_INCLUDED): VALID_FULL,
    ("EVIDENCE", METADATA_ONLY): VALID_LIMITED,
}


def _exposure_status(entity_type: str, exposures) -> str:
    if not exposures:
        return VALID_LIMITED
    statuses = {_STATUS_BY_EXPOSURE.get((entity_type, exposure), VALID_LIMITED)
                for exposure in exposures}
    return VALID_FULL if VALID_FULL in statuses else VALID_LIMITED


class ReferenceVerifier:
    """Verifies citations against one run's recorded sources and manifest."""

    def __init__(self, repositories, *, workspace_id, manifest=None, recorded=None):
        self._repositories = repositories
        self._workspace_id = workspace_id
        self._manifest = manifest
        self._recorded = {}
        if recorded is not None:
            self._recorded = {kind: frozenset(recorded.get(kind, ()))
                              for kind in CITABLE_TYPES}
        self._cache = {}

    # ------------------------------------------------------------------ factory
    @classmethod
    def for_run(cls, repositories, run, sources):
        """Build a verifier for a stored run (manifest + recorded AnalysisSource)."""
        recorded = {"PERSON": {str(item.target_id) for item in sources.people},
                    "RELATIONSHIP": {str(item.target_id) for item in sources.relationships},
                    "EVENT": {str(item.target_id) for item in sources.events},
                    "EVIDENCE": {str(item.target_id) for item in sources.evidence}}
        manifest = None
        if run.context_snapshot:
            try:
                from app.services.context import snapshot_from_mapping
                snapshot = snapshot_from_mapping(json.loads(run.context_snapshot))
                manifest = (snapshot.entity_manifest
                            if snapshot.entity_manifest.entries else None)
            except (ValueError, KeyError, TypeError):
                manifest = None
        return cls(repositories, workspace_id=run.workspace_id, manifest=manifest,
                   recorded=recorded)

    # -------------------------------------------------------------------- verify
    def verify(self, entity_type: str, entity_id, raw: str = "") -> ValidatedReference:
        """Classify one citation (a known entity type plus its UUID)."""
        normalized = (entity_type or "").strip().upper()
        if normalized not in CITABLE_TYPES:
            return ValidatedReference(
                raw=raw or f"{entity_type}:{entity_id}", entity_type=normalized or "UNKNOWN",
                entity_id=None, status=TYPE_MISMATCH,
                reason="该实体类型不可被引用（只有 PERSON/RELATIONSHIP/EVENT/EVIDENCE 可引用）")
        if not isinstance(entity_id, UUID):
            return ValidatedReference(raw=raw, entity_type=normalized, entity_id=None,
                                      status=INVALID_FORMAT, reason="引用格式无效：不是合法 UUID")
        key = (normalized, str(entity_id))
        if key not in self._cache:
            self._cache[key] = self._verify_uuid(normalized, entity_id, raw)
        cached = self._cache[key]
        return cached if cached.raw == raw else ValidatedReference(
            raw=raw, entity_type=cached.entity_type, entity_id=cached.entity_id,
            status=cached.status, reason=cached.reason, exposure=cached.exposure)

    def _verify_uuid(self, entity_type: str, entity_id: UUID, raw: str) -> ValidatedReference:
        def result(status, reason, exposure=()):
            return ValidatedReference(raw=raw, entity_type=entity_type, entity_id=entity_id,
                                      status=status, reason=reason, exposure=exposure)

        target = self._repositories.lookup.find(entity_type, entity_id)
        if target is None:
            for other in CITABLE_TYPES:
                if other == entity_type:
                    continue
                if self._repositories.lookup.find(other, entity_id) is not None:
                    return result(TYPE_MISMATCH,
                                  f"引用类型错误：该 UUID 实际属于 {other}")
            return result(UNKNOWN_SOURCE, "对象不存在（UUID 未在任何 Workspace 中找到）")
        if target.workspace_id != self._workspace_id:
            return result(WORKSPACE_MISMATCH, "对象属于其他 Workspace")
        if target.deleted_at is not None:
            return result(DELETED_SOURCE, "来源已被删除")
        if self._manifest is not None:
            entry = self._manifest.get(entity_type, entity_id)
            if entry is None:
                return result(SOURCE_NOT_EXPOSED,
                              "实体不在本次 Context Entity Manifest 中（未在上下文曝光）")
            status = _exposure_status(entity_type, entry.exposures)
            if status == VALID_FULL:
                reason = "在本次上下文中完整曝光：" + ("、".join(entry.exposures) or "—")
            else:
                reason = ("在本次上下文中有限曝光：" + ("、".join(entry.exposures) or "—")
                          + "（结论只能引用其所示信息）")
            return result(status, reason, entry.exposures)
        # Legacy snapshot without manifest: the old source rule only proves
        # membership, so it is never upgraded to VALID_FULL.
        if str(entity_id) not in self._recorded.get(entity_type, frozenset()):
            return result(NOT_IN_RUN, "对象不属于本次分析的来源集合（旧快照规则）")
        return result(VALID_LIMITED,
                      "旧快照无 Context Entity Manifest：仅确认来源归属，未确认曝光等级")

    # ----------------------------------------------------------------- scanning
    def verify_markers(self, text: str) -> list:
        """Verify every ``[TYPE:<uuid>]`` citation found in raw answer text."""
        results, seen = [], set()
        for match in MARKER_PATTERN.finditer(text or ""):
            raw, entity_type, identifier = match.group(0), match.group(1), match.group(2)
            if raw in seen:
                continue
            seen.add(raw)
            try:
                entity_id = UUID(identifier)
            except ValueError:
                results.append(ValidatedReference(raw=raw, entity_type=entity_type,
                                                  entity_id=None, status=INVALID_FORMAT,
                                                  reason="引用格式无效：不是合法 UUID"))
                continue
            results.append(self.verify(entity_type, entity_id, raw=raw))
        return results

    def verify_parsed_result(self, parsed) -> tuple:
        """Verify every structural reference of a parsed answer, per entry."""
        from app.domain.entities.analysis_result import SECTION_KEYS
        checks = []
        for section in SECTION_KEYS:
            for index, entry in enumerate(parsed.section(section)):
                for reference in entry.references:
                    checks.append(EntryReferenceCheck(
                        section=section, entry_index=index,
                        reference=self.verify(reference.entity_type, reference.entity_id,
                                              raw=f"{reference.entity_type}:{reference.entity_id}")))
        return tuple(checks)
