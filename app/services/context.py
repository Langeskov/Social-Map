"""ContextBuilder: the only place that decides what a model is allowed to see.

It reads the fact layer directly (never SearchService: search *finds* objects, the
builder *renders a factual context*), and it renders an explicit, reproducible
snapshot. The snapshot is stored on the AnalysisRun, because facts change later and
a past analysis must still show exactly what the model was given.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from app.domain.entities import Event, Evidence, Person
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
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

#: Conservative estimate: CJK text is roughly one token per 1.5 characters, so a
#: two-characters-per-token bound never underestimates a mixed context.
CHARS_PER_TOKEN = 2
DEFAULT_MAX_CONTEXT_TOKENS = 24000


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def event_instant(event: Event) -> datetime:
    """Sorting/instant value: `started_at`, or the UTC midnight of a DATE event."""
    if event.started_at is not None:
        return event.started_at
    if event.occurred_on is not None:
        return datetime.combine(event.occurred_on, time.min, tzinfo=timezone.utc)
    raise DomainError("Event 既没有 occurred_on 也没有 started_at。")


def evidence_instant(evidence: Evidence) -> datetime:
    return evidence.captured_at or evidence.created_at


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
            "sections": [{"key": section.key, "title": section.title, "kind": section.kind,
                          "note": section.note, "lines": list(section.lines)}
                         for section in self.sections],
            "rendered_context": self.rendered_context,
        }

    def source_ids(self) -> dict:
        """Fact ids referenced, grouped by the fact table they belong to."""
        scope = self.scope
        return {"PERSON": tuple(scope.get("person_ids", ())),
                "RELATIONSHIP": tuple(scope.get("relationship_ids", ())),
                "EVENT": tuple(scope.get("event_ids", ())),
                "EVIDENCE": tuple(scope.get("evidence_ids", ()))}


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
    def _memory_section(self, repositories, request, person_ids, collected):
        """Renders user-confirmed memories; appends the included ids to `collected`."""
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
            collected.append(str(memory.id))
            period = ""
            if memory.valid_from or memory.valid_until:
                period = (f" · 有效期={memory.valid_from.isoformat() if memory.valid_from else '不限'}"
                          f"→{memory.valid_until.isoformat() if memory.valid_until else '不限'}")
            scope_label = "人物" if memory.person_id else "Workspace"
            lines.append(f"[{memory.kind}] {memory.content} · 范围={scope_label}"
                         f" · 来源={memory.source_type}"
                         f" · 确认于 {memory.confirmed_at.isoformat()}{period}")
        return ContextSection("memories", "用户确认的长期记忆（Memory）", FACT, tuple(lines),
                              note="由用户确认，可能已经过时，不是数据库事实")

    # -------------------------------------------------------------------- build
    def build(self, request: AnalysisRequest, *, analysis_run_id: UUID | None = None) -> ContextSnapshot:
        repositories = self._repositories
        self._require_workspace(repositories, request.workspace_id)
        window = request.window()
        if request.target_type == "PERSON":
            sections, scope = self._person_context(repositories, request, window)
        elif request.target_type == "RELATIONSHIP":
            sections, scope = self._relationship_context(repositories, request, window)
        elif request.target_type == "EVENT":
            sections, scope = self._event_context(repositories, request, window)
        else:
            raise DomainError("ContextBuilder 支持 PERSON、RELATIONSHIP、EVENT。")

        # Memories are opt-in and only the *current* ones are eligible: a deleted or
        # expired memory never reaches a model, even when explicitly selected.
        memory_ids = []
        if request.include_memories or request.selected_memory_ids:
            section = self._memory_section(repositories, request, scope.get("person_ids", ()),
                                           memory_ids)
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
        scope["included_memory_ids"] = [str(value) for value in memory_ids]
        snapshot = ContextSnapshot(analysis_run_id=analysis_run_id, generated_at=self._clock(),
                                   scope=scope, rendered_context="", sections=tuple(sections),
                                   target_type=request.target_type, target_id=request.target_id,
                                   analysis_type=request.analysis_type)
        rendered = _render(snapshot)
        snapshot = ContextSnapshot(analysis_run_id=analysis_run_id,
                                   generated_at=snapshot.generated_at, scope=scope,
                                   rendered_context=rendered, sections=tuple(sections),
                                   target_type=request.target_type, target_id=request.target_id,
                                   analysis_type=request.analysis_type)
        guard = snapshot.estimated_tokens
        if guard > request.max_context_tokens:
            raise DomainError(
                f"Selected context exceeds model context limit."
                f"（估算 {guard} tokens > 上限 {request.max_context_tokens}）"
                "请缩小时间范围、取消勾选部分内容，或减少选中的事件／证据。")
        return snapshot

    # --------------------------------------------------------------- per target
    def _person_context(self, repositories, request, window):
        person = repositories.people.get(request.workspace_id, request.target_id)
        if person is None:
            raise NotFoundError("人物不属于当前 Workspace，或已经归档。")
        sections = [ContextSection("target", "目标人物", FACT, (self._person_line(person),))]
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
                relationship_lines.append(
                    f"{view.relationship_type} · {view.directionality} · 对方={label} · id={view.id}")
            if request.include_history and relationship_ids:
                histories = repositories.histories.list_by_workspace(request.workspace_id)
                for history in histories:
                    if history.relationship_id not in set(relationship_ids):
                        continue
                    if not _history_in_window(history, window):
                        continue
                    history_lines.append(_history_line(history, window))
        if relationship_lines:
            sections.append(ContextSection("relationships", "当前关系", FACT, tuple(relationship_lines)))
        if history_lines:
            sections.append(ContextSection("history", "关系历史（effective_at）", FACT,
                                           tuple(sorted(history_lines))))
        events = self._target_events(repositories, request, window, (person.id,))
        event_sections, ids = self._event_sections(repositories, request, events)
        sections.extend(event_sections)
        scope = {"target": "PERSON", "person_ids": [str(person.id)],
                 "relationship_ids": [str(value) for value in relationship_ids],
                 "event_ids": ids["event_ids"], "evidence_ids": ids["evidence_ids"]}
        return sections, scope

    def _relationship_context(self, repositories, request, window):
        relationship = repositories.relationships.get(request.workspace_id, request.target_id,
                                                      include_deleted=False)
        if relationship is None:
            raise NotFoundError("关系不属于当前 Workspace，或已删除。")
        first = repositories.people.get(request.workspace_id, relationship.person_a_id, include_deleted=True)
        second = repositories.people.get(request.workspace_id, relationship.person_b_id, include_deleted=True)
        sections = [ContextSection("target", "目标关系", FACT,
                                   (f"{relationship.relationship_type} · {relationship.directionality}"
                                    f" · id={relationship.id}",
                                    f"A={first.display_name if first else '（缺失）'}",
                                    f"B={second.display_name if second else '（缺失）'}"))]
        history_lines, event_ids = [], set()
        if request.include_history:
            for history in repositories.histories.list_by_relationship(request.workspace_id,
                                                                       relationship.id):
                if not _history_in_window(history, window):
                    continue
                history_lines.append(_history_line(history, window))
                if history.event_id is not None:
                    event_ids.add(history.event_id)
        if history_lines:
            sections.append(ContextSection("history", "关系历史（effective_at）", FACT,
                                           tuple(sorted(history_lines))))
        events = self._target_events(repositories, request, window,
                                     (relationship.person_a_id, relationship.person_b_id))
        for identifier in event_ids:
            if all(event.id != identifier for event in events):
                extra = repositories.events.get(request.workspace_id, identifier, include_deleted=True)
                if extra is not None:
                    events.append(extra)
        event_sections, ids = self._event_sections(repositories, request, events)
        sections.extend(event_sections)
        scope = {"target": "RELATIONSHIP", "relationship_ids": [str(relationship.id)],
                 "person_ids": [str(relationship.person_a_id), str(relationship.person_b_id)],
                 "event_ids": ids["event_ids"], "evidence_ids": ids["evidence_ids"]}
        return sections, scope

    def _event_context(self, repositories, request, window):
        event = repositories.events.get(request.workspace_id, request.target_id, include_deleted=True)
        if event is None:
            raise NotFoundError("Event 不属于当前 Workspace。")
        participants = repositories.participants.list_by_event(request.workspace_id, event.id)
        lines = [self._event_line(event)]
        for item in participants:
            person = repositories.people.get(request.workspace_id, item.person_id, include_deleted=True)
            name = person.display_name if person is not None else "（已归档人物）"
            lines.append(f"参与人：{name}" + (f" · role={item.role}" if item.role else "")
                         + (f" · note={_collapse(item.note)}" if item.note else ""))
        sections = [ContextSection("target", "目标事件", FACT, tuple(lines))]
        event_sections, ids = self._event_sections(repositories, request, [event],
                                                   skip_ids={event.id})
        sections.extend(event_sections)
        history_lines = []
        for history in repositories.histories.list_by_event(request.workspace_id, event.id):
            history_lines.append(_history_line(history, window))
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

    def _event_sections(self, repositories, request, events, *, skip_ids=frozenset()):
        """Render event/observation/evidence sections and return the ids referenced."""
        sections, ids = [], {"event_ids": [], "evidence_ids": []}
        fact_lines, observation_lines = [], []
        evidence_lines, content_lines = [], []
        for event in events:
            if event.id not in skip_ids:
                (observation_lines if event.record_kind == OBSERVATION else fact_lines).append(
                    self._event_line(event))
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
                    f"{self._evidence_metadata_line(evidence, attachment_names)} （事件：{event.title}）")
                ids["evidence_ids"].append(str(evidence.id))
                # Raw text (and the user annotation) only when explicitly requested;
                # binaries are never included.
                if request.include_evidence:
                    if evidence.content_text:
                        content_lines.append(f"（事件：{event.title}｜{evidence.title}）"
                                             f"{_collapse(evidence.content_text)}")
                    if evidence.annotation:
                        content_lines.append(f"（用户注释｜{evidence.title}）{_collapse(evidence.annotation)}")
        if fact_lines:
            sections.append(ContextSection("events", "事件（FACT）", FACT, tuple(sorted(fact_lines))))
        if observation_lines and request.include_observations:
            sections.append(ContextSection("observations", "用户记录的观察（OBSERVATION）", OBSERVATION,
                                           tuple(sorted(observation_lines))))
        if evidence_lines:
            note = "含用户明确选择的原文" if request.include_evidence else "仅元数据（未发送原文）"
            sections.append(ContextSection("evidence", f"证据（{note}）", FACT,
                                           tuple(sorted(evidence_lines)), note=note))
        if content_lines:
            sections.append(ContextSection("evidence_content", "证据原文（用户明确选择）", FACT,
                                           tuple(sorted(content_lines))))
        return sections, ids


def _history_line(history, window) -> str:
    when = history.effective_at.isoformat()
    marker = "" if history.effective_at <= window.as_of else "（计划中，截至 as_of 尚未生效）"
    text = f"{when} · {history.state}{marker}"
    if history.note:
        text += f" · note={_collapse(history.note)}"
    if history.event_id:
        text += f" · event={history.event_id}"
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
    """Rebuild a stored snapshot (used by the UI and by tests)."""
    sections = tuple(ContextSection(key=item.get("key", ""), title=item.get("title", ""),
                                    kind=item.get("kind", FACT), note=item.get("note"),
                                    lines=tuple(item.get("lines", ())))
                     for item in data.get("sections", ()))
    return ContextSnapshot(analysis_run_id=UUID(data["analysis_run_id"]) if data.get("analysis_run_id") else None,
                           generated_at=datetime.fromisoformat(data["generated_at"]),
                           scope=dict(data.get("scope", {})), rendered_context=data.get("rendered_context", ""),
                           sections=sections, target_type=data.get("target_type", ""),
                           target_id=UUID(data["target_id"]) if data.get("target_id") else None,
                           analysis_type=data.get("analysis_type", ""))
