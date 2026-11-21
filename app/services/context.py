"""ContextBuilder: the only place that decides what a model is allowed to see.

It reads the fact layer directly (never SearchService: search *finds* objects, the
builder *renders a factual context*), and it renders an explicit, reproducible
snapshot. The snapshot is stored on the AnalysisRun, because facts change later and
a past analysis must still show exactly what the model was given.
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import re
import unicodedata
from uuid import UUID

import app.services.entity_manifest as em
from app.domain.entities import Event, Evidence, Person
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.services.entity_manifest import (ContextEntityManifest, ManifestBuilder,
                                          entity_marker, person_exposure_note)
from app.services.rules import workspace_required

LAST_7_DAYS = "LAST_7_DAYS"
LAST_30_DAYS = "LAST_30_DAYS"
LAST_90_DAYS = "LAST_90_DAYS"
ALL_TIME = "ALL"
CUSTOM = "CUSTOM"
TIME_SCOPES = (LAST_7_DAYS, LAST_30_DAYS, LAST_90_DAYS, ALL_TIME, CUSTOM)
SCOPE_DAYS = {LAST_7_DAYS: 7, LAST_30_DAYS: 30, LAST_90_DAYS: 90}

EVENT_KIND = "EVENT"
OBSERVATION_KIND = "OBSERVATION"
FACT = "FACT"
OBSERVATION = "OBSERVATION"

#: Non-CJK text runs at roughly four characters per token in practice; counting
#: two characters per token keeps the estimate conservative (too high) for
#: ASCII-heavy content.
CHARS_PER_TOKEN = 2
#: CJK / full-width text averages ~1.5 characters per token and can reach one
#: token per character, so every such character is counted as a whole token.
#: (The old single-ratio len/2 estimate *undercounted* CJK text by ~25% against
#: this very same 1.5-characters-per-token figure; see docs/phase13-evaluation.md.)
CJK_CHARS_PER_TOKEN = 1
DEFAULT_MAX_CONTEXT_TOKENS = 24000


def _is_wide_char(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def estimate_tokens(text: str) -> int:
    """Conservative offline token estimate; never a provider-reported count.

    Wide (CJK / full-width) characters count as one token each, everything else
    at ``CHARS_PER_TOKEN`` characters per token. The estimate deliberately errs
    high so the context budget guard rarely lets a context through that a real
    tokenizer would count over budget — but it cannot *guarantee* any specific
    tokenizer's count. Only the provider's usage metadata (Phase 13.3) can.
    """
    wide = 0
    narrow = 0
    for ch in text:
        if _is_wide_char(ch):
            wide += 1
        else:
            narrow += 1
    return (wide + CJK_CHARS_PER_TOKEN - 1) // CJK_CHARS_PER_TOKEN \
        + (narrow + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def event_instant(event: Event) -> datetime:
    """Sorting/instant value: `started_at`, or the UTC midnight of a DATE event."""
    if event.started_at is not None:
        return event.started_at
    if event.occurred_on is not None:
        return datetime.combine(event.occurred_on, time.min, tzinfo=timezone.utc)
    raise DomainError("Event 既没有 occurred_on 也没有 started_at。")


@dataclass(frozen=True)
class TimeWindow:
    scope: str
    start: datetime | None
    end: datetime
    as_of: datetime

    def contains(self, instant: datetime) -> bool:
        if instant > self.end:
            return False
        return self.start is None or instant >= self.start

    def to_mapping(self) -> dict:
        return {"scope": self.scope,
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat(),
                "as_of": self.as_of.isoformat()}


def resolve_window(scope: str, *, as_of: datetime, custom_start: datetime | None = None,
                   custom_end: datetime | None = None) -> TimeWindow:
    moment = normalize_datetime(as_of, "as_of")
    if scope not in TIME_SCOPES:
        raise DomainError(f"time_scope 必须是 {', '.join(TIME_SCOPES)} 之一。")
    if scope == CUSTOM:
        if custom_start is None and custom_end is None:
            raise DomainError("CUSTOM 时间范围必须提供 start 或 end。")
        start = normalize_datetime(custom_start, "custom_start", optional=True)
        end = normalize_datetime(custom_end, "custom_end") if custom_end is not None else moment
        if start is not None and start > end:
            raise DomainError("CUSTOM 时间范围的 start 不能晚于 end。")
        return TimeWindow(scope, start, end, moment)
    if scope == ALL_TIME:
        return TimeWindow(scope, None, moment, moment)
    return TimeWindow(scope, moment - timedelta(days=SCOPE_DAYS[scope]), moment, moment)


@dataclass(frozen=True)
class AnalysisRequest:
    workspace_id: UUID
    target_type: str
    target_id: UUID
    analysis_type: str
    as_of: datetime
    time_scope: str = LAST_30_DAYS
    include_relationships: bool = True
    include_history: bool = True
    include_observations: bool = True
    include_evidence: bool = False
    include_memories: bool = False
    selected_event_ids: tuple[UUID, ...] = ()
    selected_evidence_ids: tuple[UUID, ...] = ()
    selected_memory_ids: tuple[UUID, ...] = ()
    custom_start: datetime | None = None
    custom_end: datetime | None = None
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS

    def __post_init__(self):
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        object.__setattr__(self, "custom_start",
                           normalize_datetime(self.custom_start, "custom_start", optional=True))
        object.__setattr__(self, "custom_end",
                           normalize_datetime(self.custom_end, "custom_end", optional=True))
        object.__setattr__(self, "selected_event_ids", tuple(self.selected_event_ids or ()))
        object.__setattr__(self, "selected_evidence_ids", tuple(self.selected_evidence_ids or ()))
        object.__setattr__(self, "selected_memory_ids", tuple(self.selected_memory_ids or ()))
        for identifier in self.selected_event_ids + self.selected_evidence_ids + self.selected_memory_ids:
            validate_uuid(identifier, "selected id")
        if type(self.max_context_tokens) is not int or self.max_context_tokens <= 0:
            raise DomainError("max_context_tokens 必须是正整数。")

    def window(self) -> TimeWindow:
        return resolve_window(self.time_scope, as_of=self.as_of, custom_start=self.custom_start,
                              custom_end=self.custom_end)


@dataclass(frozen=True)
class ContextSection:
    """One labelled block of the rendered context (fact or observation)."""

    key: str
    title: str
    kind: str
    lines: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class ContextSnapshot:
    analysis_run_id: UUID | None
    generated_at: datetime
    scope: dict
    rendered_context: str
    sections: tuple[ContextSection, ...] = ()
    target_type: str = ""
    target_id: UUID | None = None
    analysis_type: str = ""
    entity_manifest: ContextEntityManifest = field(default_factory=ContextEntityManifest)

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.rendered_context)

    def to_mapping(self) -> dict:
        return {
            "analysis_run_id": str(self.analysis_run_id) if self.analysis_run_id else None,
            "generated_at": self.generated_at.isoformat(),
            "target_type": self.target_type,
            "target_id": str(self.target_id) if self.target_id else None,
            "analysis_type": self.analysis_type,
            "scope": self.scope,
            "entity_manifest": self.entity_manifest.to_mapping(),
            "sections": [{"key": section.key, "title": section.title, "kind": section.kind,
                          "note": section.note, "lines": list(section.lines)}
                         for section in self.sections],
            "rendered_context": self.rendered_context,
        }

    def source_ids(self) -> dict:
        """Fact ids referenced, grouped by the fact table they belong to.

        Phase 13.1: derived from the Context Entity Manifest, so every id that may
        be cited is exactly what the model saw. Snapshots stored before the
        manifest existed fall back to the legacy scope lists unchanged.
        """
        if self.entity_manifest.entries:
            return self.entity_manifest.source_ids()
        scope = self.scope
        return {"PERSON": tuple(scope.get("person_ids", ())),
                "RELATIONSHIP": tuple(scope.get("relationship_ids", ())),
                "EVENT": tuple(scope.get("event_ids", ())),
                "EVIDENCE": tuple(scope.get("evidence_ids", ()))}

    @property
    def included_memory_ids(self) -> tuple:
        """Memory ids shown to the model (manifest-driven; legacy fallback)."""
        if self.entity_manifest.entries:
            return self.entity_manifest.memory_ids()
        return tuple(self.scope.get("included_memory_ids", ()))


class ContextBuilder:
    """Renders an explicit, bounded context; it never writes to the fact layer."""

    def __init__(self, repositories, *, clock=utc_now):
        self._repositories = repositories
        self._clock = clock

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _person_line(person: Person) -> str:
        parts = [f"display_name={person.display_name}"]
        for name, label in (("real_name", "real_name"), ("gender", "gender"),
                            ("occupation", "occupation"), ("organization", "organization"),
                            ("location", "location")):
            value = getattr(person, name)
            if value:
                parts.append(f"{label}={value}")
        if person.birthday is not None:
            parts.append(f"birthday={person.birthday.isoformat()}")
        return " · ".join(parts)

    @staticmethod
    def _event_line(event: Event) -> str:
        when = event.occurred_on.isoformat() if event.occurred_on is not None else (
            event.started_at.isoformat() if event.started_at else "—")
        text = f"[{event.record_kind}] {when} · {event.title} · type={event.event_type}"
        if event.location:
            text += f" · location={event.location}"
        if event.description:
            text += f" · description={_collapse(event.description)}"
        return text

    @staticmethod
    def _evidence_metadata_line(evidence: Evidence, media_names=()) -> str:
        parts = [f"[{evidence.kind}] {evidence.title}",
                 f"captured_at={evidence.captured_at.isoformat() if evidence.captured_at else '—'}",
                 f"source={evidence.source_type}"]
        if evidence.source_label:
            parts.append(f"source_label={evidence.source_label}")
        if evidence.source_reference:
            parts.append(f"reference={evidence.source_reference}")
        if media_names:
            parts.append("attachments=" + "; ".join(media_names))
        return " · ".join(parts)

    def _require_workspace(self, repositories, workspace_id):
        validate_uuid(workspace_id, "workspace_id")
        return workspace_required(repositories, workspace_id)

    def _target_events(self, repositories, request, window, participant_ids):
        """One query for the candidate events, then one bulk participant query."""
        events = repositories.events.list_by_workspace(request.workspace_id, include_deleted=False)
        chosen: dict[UUID, Event] = {}
        for event in events:
            instant = event_instant(event)
            if window.contains(instant):
                chosen[event.id] = event
        # Explicit selection always wins, even outside the window.
        for identifier in request.selected_event_ids:
            if identifier in chosen:
                continue
            event = repositories.events.get(request.workspace_id, identifier)
            if event is None:
                raise NotFoundError("选中的 Event 不属于当前 Workspace。")
            chosen[event.id] = event
        if participant_ids is None:
            return sorted(chosen.values(), key=event_instant, reverse=True)
        wanted = set(participant_ids)
        result = []
        for event in sorted(chosen.values(), key=event_instant, reverse=True):
            participants = repositories.participants.list_by_event(request.workspace_id, event.id)
            if wanted and not any(item.person_id in wanted for item in participants):
                continue
            result.append(event)
        return result

    @staticmethod
    def _group(name, lines, kind=FACT, note=None):
        return ContextSection(key=name, title=name, kind=kind, lines=tuple(lines), note=note)

    # --------------------------------------------------------------- memories
    def _memory_section(self, repositories, request, person_ids, manifest):
        """Renders user-confirmed memories and records them in the manifest."""
        found: dict[UUID, object] = {}
        if request.include_memories:
            # Workspace-level memories plus the memories of every person in scope.
            for memory in repositories.memories.list_by_workspace(
                    request.workspace_id, workspace_scoped_only=True, now=request.as_of):
                found[memory.id] = memory
            for value in person_ids:
                for memory in repositories.memories.list_by_workspace(
                        request.workspace_id, person_id=UUID(value), now=request.as_of):
                    found[memory.id] = memory
        for identifier in request.selected_memory_ids:
            memory = repositories.memories.get(request.workspace_id, identifier, include_deleted=False)
            # Explicit selection cannot resurrect a deleted or expired memory.
            if memory is not None and memory.is_current(now=request.as_of):
                found[memory.id] = memory
        if not found:
            return None
        lines = []
        for memory in sorted(found.values(), key=lambda item: (item.kind, item.content)):
            manifest.add(em.MEMORY, memory.id, label=memory.content[:80],
                         role=em.ROLE_WORKSPACE_MEMORY if memory.person_id is None
                         else em.ROLE_PERSON_MEMORY,
                         exposure=em.USER_CONFIRMED_CONTENT)
            period = ""
            if memory.valid_from or memory.valid_until:
                period = (f" · 有效期={memory.valid_from.isoformat() if memory.valid_from else '不限'}"
                          f"→{memory.valid_until.isoformat() if memory.valid_until else '不限'}")
            scope_label = "人物" if memory.person_id else "Workspace"
            lines.append(f"{entity_marker(em.MEMORY, memory.id)} [{memory.kind}] {memory.content}"
                         f" · 范围={scope_label} · 来源={memory.source_type}"
                         f" · 确认于 {memory.confirmed_at.isoformat()}{period}")
        return ContextSection("memories", "用户确认的长期记忆（Memory）", FACT, tuple(lines),
                              note="由用户确认，可能已经过时，不是数据库事实")

    # -------------------------------------------------------------------- build
    def build(self, request: AnalysisRequest, *, analysis_run_id: UUID | None = None) -> ContextSnapshot:
        repositories = self._repositories
        self._require_workspace(repositories, request.workspace_id)
        window = request.window()
        manifest = ManifestBuilder()
        if request.target_type == "PERSON":
            sections, scope = self._person_context(repositories, request, window, manifest)
        elif request.target_type == "RELATIONSHIP":
            sections, scope = self._relationship_context(repositories, request, window, manifest)
        elif request.target_type == "EVENT":
            sections, scope = self._event_context(repositories, request, window, manifest)
        else:
            raise DomainError("ContextBuilder 支持 PERSON、RELATIONSHIP、EVENT。")

        # Memories are opt-in and only the *current* ones are eligible: a deleted or
        # expired memory never reaches a model, even when explicitly selected.
        if request.include_memories or request.selected_memory_ids:
            section = self._memory_section(repositories, request, scope.get("person_ids", ()),
                                           manifest)
            if section is not None:
                sections.append(section)

        scope["time_window"] = window.to_mapping()
        scope["as_of"] = window.as_of.isoformat()
        scope["workspace_id"] = str(request.workspace_id)
        scope["analysis_run_id"] = str(analysis_run_id) if analysis_run_id else None
        scope["include"] = {"relationships": request.include_relationships,
                            "history": request.include_history,
                            "observations": request.include_observations,
                            "evidence": request.include_evidence,
                            "memories": request.include_memories}
        scope["selected_event_ids"] = [str(value) for value in request.selected_event_ids]
        scope["selected_evidence_ids"] = [str(value) for value in request.selected_evidence_ids]
        built_manifest = manifest.build()
        # Legacy copy only; the manifest is the authority for source ids.
        scope["included_memory_ids"] = [str(value) for value in built_manifest.memory_ids()]
        snapshot = ContextSnapshot(analysis_run_id=analysis_run_id, generated_at=self._clock(),
                                   scope=scope, rendered_context="", sections=tuple(sections),
                                   target_type=request.target_type, target_id=request.target_id,
                                   analysis_type=request.analysis_type,
                                   entity_manifest=built_manifest)
        rendered = _render(snapshot)
        snapshot = ContextSnapshot(analysis_run_id=analysis_run_id,
                                   generated_at=snapshot.generated_at, scope=scope,
                                   rendered_context=rendered, sections=tuple(sections),
                                   target_type=request.target_type, target_id=request.target_id,
                                   analysis_type=request.analysis_type,
                                   entity_manifest=built_manifest)
        guard = snapshot.estimated_tokens
        if guard > request.max_context_tokens:
            raise DomainError(
                f"Selected context exceeds model context limit."
                f"（估算 {guard} tokens > 上限 {request.max_context_tokens}）"
                "请缩小时间范围、取消勾选部分内容，或减少选中的事件／证据。")
        return snapshot

    # --------------------------------------------------------------- per target
    def _person_context(self, repositories, request, window, manifest):
        person = repositories.people.get(request.workspace_id, request.target_id)
        if person is None:
            raise NotFoundError("人物不属于当前 Workspace，或已经归档。")
        manifest.add(em.PERSON, person.id, label=person.display_name,
                     role=em.ROLE_TARGET, exposure=em.PROFILE)
        sections = [ContextSection("target", "目标人物", FACT,
                                   (f"{entity_marker(em.PERSON, person.id)} "
                                    f"{self._person_line(person)}",))]
        aliases = repositories.aliases.list(request.workspace_id, person.id)
        if aliases:
            sections.append(ContextSection("aliases", "别称", FACT,
                                           tuple(f"{item.alias}（{item.alias_type}）" for item in aliases)))
        relationship_ids, relationship_lines, history_lines = [], [], []
        if request.include_relationships or request.include_history:
            views = repositories.relationships.list_by_person(request.workspace_id, person.id,
                                                              include_deleted=False)
            for view in views:
                other = repositories.people.get(request.workspace_id,
                                                view.person_b_id if view.person_a_id == person.id
                                                else view.person_a_id, include_deleted=True)
                label = other.display_name if other is not None else "（已归档人物）"
                relationship_ids.append(view.id)
                manifest.add(em.RELATIONSHIP, view.id, label=view.relationship_type,
                             role=em.ROLE_PERSON_RELATIONSHIP, exposure=em.RELATIONSHIP_FACT)
                if other is not None:
                    manifest.add(em.PERSON, other.id, label=other.display_name,
                                 role=em.ROLE_RELATIONSHIP_ENDPOINT,
                                 exposure=em.IDENTITY_ONLY)
                    other_text = (f"{entity_marker(em.PERSON, other.id)} {label}"
                                  f"{person_exposure_note((em.IDENTITY_ONLY,))}")
                else:
                    other_text = label
                relationship_lines.append(
                    f"{entity_marker(em.RELATIONSHIP, view.id)} {view.relationship_type}"
                    f" · {view.directionality} · 对方={other_text}")
            if request.include_history and relationship_ids:
                histories = repositories.histories.list_by_workspace(request.workspace_id)
                for history in histories:
                    if history.relationship_id not in set(relationship_ids):
                        continue
                    if not _history_in_window(history, window):
                        continue
                    if history.event_id is not None:
                        # Only the link is shown here, never the event detail.
                        manifest.add(em.EVENT, history.event_id, role=em.ROLE_LINKED_EVENT,
                                     exposure=em.EVENT_REFERENCE)
                    history_lines.append(_history_line(history, window))
        if relationship_lines:
            sections.append(ContextSection("relationships", "当前关系", FACT, tuple(relationship_lines)))
        if history_lines:
            sections.append(ContextSection("history", "关系历史（effective_at）", FACT,
                                           tuple(sorted(history_lines, key=_marker_stripped))))
        events = self._target_events(repositories, request, window, (person.id,))
        event_sections, ids = self._event_sections(repositories, request, events, manifest)
        sections.extend(event_sections)
        scope = {"target": "PERSON", "person_ids": [str(person.id)],
                 "relationship_ids": [str(value) for value in relationship_ids],
                 "event_ids": ids["event_ids"], "evidence_ids": ids["evidence_ids"]}
        return sections, scope

    def _relationship_context(self, repositories, request, window, manifest):
        relationship = repositories.relationships.get(request.workspace_id, request.target_id,
                                                      include_deleted=False)
        if relationship is None:
            raise NotFoundError("关系不属于当前 Workspace，或已删除。")
        manifest.add(em.RELATIONSHIP, relationship.id, label=relationship.relationship_type,
                     role=em.ROLE_TARGET, exposure=em.RELATIONSHIP_FACT)
        first = repositories.people.get(request.workspace_id, relationship.person_a_id, include_deleted=True)
        second = repositories.people.get(request.workspace_id, relationship.person_b_id, include_deleted=True)
        endpoint_lines = [f"{entity_marker(em.RELATIONSHIP, relationship.id)} "
                          f"{relationship.relationship_type} · {relationship.directionality}"]
        for side, person in (("A", first), ("B", second)):
            if person is None:
                endpoint_lines.append(f"{side}=（缺失）")
                continue
            manifest.add(em.PERSON, person.id, label=person.display_name,
                         role=em.ROLE_RELATIONSHIP_ENDPOINT, exposure=em.IDENTITY_ONLY)
            endpoint_lines.append(
                f"{side}={entity_marker(em.PERSON, person.id)} {person.display_name}"
                f"{person_exposure_note((em.IDENTITY_ONLY,))}")
        sections = [ContextSection("target", "目标关系", FACT, tuple(endpoint_lines))]
        history_lines, event_ids = [], set()
        if request.include_history:
            for history in repositories.histories.list_by_relationship(request.workspace_id,
                                                                       relationship.id):
                if not _history_in_window(history, window):
                    continue
                if history.event_id is not None:
                    event_ids.add(history.event_id)
                    manifest.add(em.EVENT, history.event_id, role=em.ROLE_LINKED_EVENT,
                                 exposure=em.EVENT_REFERENCE)
                history_lines.append(_history_line(history, window))
        if history_lines:
            sections.append(ContextSection("history", "关系历史（effective_at）", FACT,
                                           tuple(sorted(history_lines, key=_marker_stripped))))
        events = self._target_events(repositories, request, window,
                                     (relationship.person_a_id, relationship.person_b_id))
        for identifier in event_ids:
            if all(event.id != identifier for event in events):
                extra = repositories.events.get(request.workspace_id, identifier, include_deleted=True)
                if extra is not None:
                    events.append(extra)
        event_sections, ids = self._event_sections(repositories, request, events, manifest)
        sections.extend(event_sections)
        scope = {"target": "RELATIONSHIP", "relationship_ids": [str(relationship.id)],
                 "person_ids": [str(relationship.person_a_id), str(relationship.person_b_id)],
                 "event_ids": ids["event_ids"], "evidence_ids": ids["evidence_ids"]}
        return sections, scope

    def _event_context(self, repositories, request, window, manifest):
        event = repositories.events.get(request.workspace_id, request.target_id, include_deleted=True)
        if event is None:
            raise NotFoundError("Event 不属于当前 Workspace。")
        manifest.add(em.EVENT, event.id, label=event.title,
                     role=em.ROLE_TARGET, exposure=em.EVENT_DETAIL)
        participants = repositories.participants.list_by_event(request.workspace_id, event.id)
        lines = [f"{entity_marker(em.EVENT, event.id)} {self._event_line(event)}"]
        for item in participants:
            person = repositories.people.get(request.workspace_id, item.person_id, include_deleted=True)
            if person is not None:
                manifest.add(em.PERSON, person.id, label=person.display_name,
                             role=em.ROLE_EVENT_PARTICIPANT, exposure=em.PARTICIPANT_CONTEXT)
                name = (f"{entity_marker(em.PERSON, person.id)} {person.display_name}"
                        f"{person_exposure_note((em.PARTICIPANT_CONTEXT,))}")
            else:
                name = "（已归档人物）"
            lines.append(f"参与人：{name}" + (f" · role={item.role}" if item.role else "")
                         + (f" · note={_collapse(item.note)}" if item.note else ""))
        sections = [ContextSection("target", "目标事件", FACT, tuple(lines))]
        event_sections, ids = self._event_sections(repositories, request, [event], manifest,
                                                   skip_ids={event.id})
        sections.extend(event_sections)
        history_lines = []
        for history in repositories.histories.list_by_event(request.workspace_id, event.id):
            relationship = repositories.relationships.get(
                request.workspace_id, history.relationship_id, include_deleted=True)
            manifest.add(em.RELATIONSHIP, history.relationship_id,
                         label=relationship.relationship_type if relationship is not None else "",
                         role=em.ROLE_EVENT_HISTORY, exposure=em.RELATIONSHIP_FACT)
            history_lines.append(_history_line(
                history, window, prefix=f"{entity_marker(em.RELATIONSHIP, history.relationship_id)} "))
        if history_lines:
            sections.append(ContextSection("linked_history", "与该事件关联的关系历史", FACT,
                                           tuple(sorted(history_lines))))
        scope = {"target": "EVENT", "event_ids": [str(event.id)],
                 "person_ids": [str(item.person_id) for item in participants],
                 "relationship_ids": sorted({str(item.relationship_id) for item in
                                             repositories.histories.list_by_event(request.workspace_id,
                                                                                  event.id)}),
                 "evidence_ids": ids["evidence_ids"]}
        return sections, scope

    def _event_sections(self, repositories, request, events, manifest, *, skip_ids=frozenset()):
        """Render event/observation/evidence sections and return the ids referenced."""
        sections, ids = [], {"event_ids": [], "evidence_ids": []}
        fact_lines, observation_lines = [], []
        evidence_lines, content_lines = [], []
        for event in events:
            if event.id not in skip_ids:
                manifest.add(em.EVENT, event.id, label=event.title,
                             role=em.ROLE_LINKED_EVENT, exposure=em.EVENT_DETAIL)
                (observation_lines if event.record_kind == OBSERVATION else fact_lines).append(
                    f"{entity_marker(em.EVENT, event.id)} {self._event_line(event)}")
            ids["event_ids"].append(str(event.id))
            selected = set(request.selected_evidence_ids)
            for evidence in repositories.evidence.list_by_event(request.workspace_id, event.id,
                                                                include_deleted=False):
                if request.selected_evidence_ids and evidence.id not in selected:
                    continue
                attachment_names = []
                for link in repositories.evidence_media.list_by_evidence(request.workspace_id, evidence.id):
                    model = repositories.media.get(request.workspace_id, link.media_id,
                                                   include_deleted=True)
                    if model is not None:
                        attachment_names.append(f"{model.mime_type}/{model.size_bytes}B")
                evidence_lines.append(
                    f"{entity_marker(em.EVIDENCE, evidence.id)} "
                    f"{self._evidence_metadata_line(evidence, attachment_names)} （事件：{event.title}）")
                ids["evidence_ids"].append(str(evidence.id))
                content_provided = bool(request.include_evidence and
                                        (evidence.content_text or evidence.annotation))
                manifest.add(em.EVIDENCE, evidence.id, label=evidence.title,
                             role=em.ROLE_EVENT_EVIDENCE,
                             exposure=(em.CONTENT_INCLUDED if content_provided
                                       else em.METADATA_ONLY))
                # Raw text (and the user annotation) only when explicitly requested;
                # binaries are never included.
                if request.include_evidence:
                    marker = entity_marker(em.EVIDENCE, evidence.id)
                    if evidence.content_text:
                        content_lines.append(f"{marker} （事件：{event.title}｜{evidence.title}）"
                                             f"{_collapse(evidence.content_text)}")
                    if evidence.annotation:
                        content_lines.append(f"{marker} （用户注释｜{evidence.title}）"
                                             f"{_collapse(evidence.annotation)}")
        if fact_lines:
            sections.append(ContextSection("events", "事件（FACT）", FACT,
                                           tuple(sorted(fact_lines, key=_marker_stripped))))
        if observation_lines and request.include_observations:
            sections.append(ContextSection("observations", "用户记录的观察（OBSERVATION）", OBSERVATION,
                                           tuple(sorted(observation_lines, key=_marker_stripped))))
        if evidence_lines:
            note = "含用户明确选择的原文" if request.include_evidence else "仅元数据（未发送原文）"
            sections.append(ContextSection("evidence", f"证据（{note}）", FACT,
                                           tuple(sorted(evidence_lines, key=_marker_stripped)),
                                           note=note))
        if content_lines:
            sections.append(ContextSection("evidence_content", "证据原文（用户明确选择）", FACT,
                                           tuple(sorted(content_lines, key=_marker_stripped))))
        return sections, ids


def _history_line(history, window, *, prefix="") -> str:
    when = history.effective_at.isoformat()
    marker = "" if history.effective_at <= window.as_of else "（计划中，截至 as_of 尚未生效）"
    text = f"{prefix}{when} · {history.state}{marker}"
    if history.note:
        text += f" · note={_collapse(history.note)}"
    if history.event_id:
        text += f" · event={entity_marker(em.EVENT, history.event_id)}"
    return text


def _history_in_window(history, window) -> bool:
    # Future-effective rows are kept (marked) so the model can distinguish a plan
    # from a state that is already in effect; they never count as "current".
    if window.start is not None and history.effective_at < window.start:
        return False
    return True


def _collapse(text: str | None, limit: int = 400) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


_MARKER_PATTERN = re.compile(
    r"\[(?:PERSON|RELATIONSHIP|EVENT|EVIDENCE|MEMORY):"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\]\s*")


def _marker_stripped(line: str) -> str:
    """Sort key that ignores run-local UUID markers (deterministic across runs)."""
    return _MARKER_PATTERN.sub("", line)


def _render(snapshot: ContextSnapshot) -> str:
    """Deterministic plain-text rendering: the literal text sent to the model."""
    lines = [f"# 事实上下文（target={snapshot.target_type}，analysis={snapshot.analysis_type}）"]
    window = snapshot.scope.get("time_window", {})
    lines.append(f"时间范围：{window.get('scope')} · start={window.get('start') or '（不限）'}"
                 f" · end={window.get('end')} · as_of={window.get('as_of')}")
    include = snapshot.scope.get("include", {})
    lines.append("包含："
                 + "、".join(f"{key}={'是' if value else '否'}" for key, value in include.items()))
    lines.append("以上均为数据库中的记录；OBSERVATION 是用户记录的观察，不是模型推断。")
    for section in snapshot.sections:
        if section.key == "_ids":
            continue
        lines.append("")
        lines.append(f"## {section.title}" + (f"（{section.note}）" if section.note else ""))
        if not section.lines:
            lines.append("（无）")
        for line in section.lines:
            lines.append(f"- {line}")
    return "\n".join(lines)


def snapshot_from_mapping(data: dict) -> ContextSnapshot:
    """Rebuild a stored snapshot (used by the UI and by tests).

    Snapshots written before Phase 13.1 have no entity manifest; they rebuild with
    an empty manifest and keep the legacy scope-based source rules.
    """
    sections = tuple(ContextSection(key=item.get("key", ""), title=item.get("title", ""),
                                    kind=item.get("kind", FACT), note=item.get("note"),
                                    lines=tuple(item.get("lines", ())))
                     for item in data.get("sections", ()))
    manifest = ContextEntityManifest.from_mapping(data.get("entity_manifest"))
    return ContextSnapshot(analysis_run_id=UUID(data["analysis_run_id"]) if data.get("analysis_run_id") else None,
                           generated_at=datetime.fromisoformat(data["generated_at"]),
                           scope=dict(data.get("scope", {})), rendered_context=data.get("rendered_context", ""),
                           sections=sections, target_type=data.get("target_type", ""),
                           target_id=UUID(data["target_id"]) if data.get("target_id") else None,
                           analysis_type=data.get("analysis_type", ""),
                           entity_manifest=manifest or ContextEntityManifest())
