"""Explicit user-recorded Events. No automatic relationship creation or state changes."""
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID
from app.domain.entities import Event, EventParticipant, Person, Relationship, RelationshipHistory
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid
from app.services.event_repositories import sql_event_repositories
from app.services.operations import write_operation
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction


@dataclass(frozen=True)
class ParticipantInput:
    person_id: UUID
    role: str | None = None
    note: str | None = None

    def __post_init__(self):
        validate_uuid(self.person_id, "person_id")
        validate_text(self.role, "role", max_length=200, optional=True, nonempty=False)
        validate_text(self.note, "note", optional=True, nonempty=False)


@dataclass(frozen=True)
class ParticipantView:
    participant: EventParticipant
    person: Person


@dataclass(frozen=True)
class HistoryLink:
    history: RelationshipHistory
    relationship: Relationship


# Explicit "the caller did not supply a known previous link" sentinel. It is
# deliberately not None: None means "the history is expected to be unlinked".
ANY_EVENT = object()


@dataclass(frozen=True)
class EventDetail:
    event: Event
    participants: tuple[ParticipantView, ...]
    histories: tuple[HistoryLink, ...]


class EventService:
    EDITABLE = {"title", "event_type", "record_kind", "occurred_on", "started_at", "ended_at", "location", "description"}

    def __init__(self, sessions, repositories=sql_event_repositories, *, clock=utc_now):
        self._sessions, self._repositories, self._clock = sessions, repositories, clock

    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    def _changed_at(self, old):
        return max(self._now(), old.updated_at + timedelta(microseconds=1))

    def _get(self, repositories, workspace_id, id, *, include_deleted=False):
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(id, "event_id")
        workspace_required(repositories, workspace_id)
        event = repositories.events.get(workspace_id, id, include_deleted=include_deleted)
        if event is None:
            raise NotFoundError("Event 不属于当前 Workspace、已删除或不存在。")
        return event

    def _inputs(self, participants):
        if isinstance(participants, (str, bytes)) or participants is None:
            raise DomainError("participants 必须是 UUID 或 ParticipantInput 的序列。")
        try:
            result = tuple(ParticipantInput(item) if isinstance(item, UUID) else item for item in participants)
        except TypeError as exc:
            raise DomainError("participants 必须是 UUID 或 ParticipantInput 的序列。") from exc
        if any(not isinstance(item, ParticipantInput) for item in result):
            raise DomainError("participants 仅接受 UUID 或 ParticipantInput。")
        if len({item.person_id for item in result}) != len(result):
            raise ConflictError("同一人物在一个 Event 中只能参与一次。")
        return result

    def _validate_people(self, repositories, workspace_id, inputs, existing_ids=frozenset()):
        for item in inputs:
            person = repositories.people.get(workspace_id, item.person_id, include_deleted=True)
            if person is None:
                raise WorkspaceMismatchError("Participant 必须属于 Event 的 Workspace。")
            if person.deleted_at is not None and item.person_id not in existing_ids:
                raise DomainError("Archived 人物不能新增为 Participant；已有历史参与记录可以保留。")

    def _detail(self, repositories, event):
        participants = []
        for participant in repositories.participants.list_by_event(event.workspace_id, event.id):
            person = repositories.people.get(event.workspace_id, participant.person_id, include_deleted=True)
            if person is None:
                raise NotFoundError("历史 Participant 的人物记录缺失。")
            participants.append(ParticipantView(participant, person))
        histories = []
        for history in repositories.histories.list_by_event(event.workspace_id, event.id):
            relationship = repositories.relationships.get(event.workspace_id, history.relationship_id, include_deleted=True)
            if relationship is None:
                raise NotFoundError("History 对应的 Relationship 缺失。")
            histories.append(HistoryLink(history, relationship))
        return EventDetail(event, tuple(participants), tuple(histories))

    def create_event(self, workspace_id, title, event_type, record_kind="FACT", *, occurred_on=None,
                     started_at=None, ended_at=None, location=None, description=None, participants=()):
        inputs = self._inputs(participants)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            validate_uuid(workspace_id, "workspace_id")
            workspace_required(repositories, workspace_id)
            now = self._now()
            entity = Event(workspace_id=workspace_id, title=title, event_type=event_type, record_kind=record_kind,
                occurred_on=occurred_on, started_at=started_at, ended_at=ended_at, location=location,
                description=description, created_at=now, updated_at=now)
            self._validate_people(repositories, workspace_id, inputs)
            saved = repositories.events.save(entity)
            for item in inputs:
                repositories.participants.add(EventParticipant(workspace_id=workspace_id, event_id=entity.id,
                    person_id=item.person_id, role=item.role, note=item.note, created_at=now))
            return saved

    def update_event(self, workspace_id, id, **changes):
        if set(changes) - (self.EDITABLE | {"participants"}):
            raise DomainError("不能修改 Event 身份、Workspace 或生命周期字段。")
        supplied = "participants" in changes
        inputs = self._inputs(changes.pop("participants")) if supplied else None
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, id)
            entity = replace(old, **changes, updated_at=self._changed_at(old))
            current = {item.person_id: item for item in repositories.participants.list_by_event(workspace_id, id)}
            if inputs is not None:
                self._validate_people(repositories, workspace_id, inputs, frozenset(current))
            saved = repositories.events.update(entity)
            if inputs is not None:
                wanted = {item.person_id for item in inputs}
                for person_id in current.keys() - wanted:
                    repositories.participants.remove(workspace_id, id, person_id)
                for item in inputs:
                    if item.person_id in current:
                        previous = current[item.person_id]
                        # Keep original participation ID/created_at even for an archived person.
                        if (previous.role, previous.note) != (item.role, item.note):
                            repositories.participants.update_metadata(replace(previous, role=item.role, note=item.note))
                    else:
                        repositories.participants.add(EventParticipant(workspace_id=workspace_id, event_id=id,
                            person_id=item.person_id, role=item.role, note=item.note, created_at=entity.updated_at))
            return saved

    def get(self, workspace_id, id, *, include_deleted=False):
        with transaction(self._sessions) as session:
            return self._get(self._repositories(session), workspace_id, id, include_deleted=include_deleted)

    def detail(self, workspace_id, id, *, include_deleted=False):
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            return self._detail(repositories, self._get(repositories, workspace_id, id, include_deleted=include_deleted))

    def list_events(self, workspace_id, search="", *, include_deleted=False):
        validate_uuid(workspace_id, "workspace_id")
        validate_text(search, "search", nonempty=False)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return [self._detail(repositories, event) for event in
                repositories.events.list_by_workspace(workspace_id, search.strip(), include_deleted=include_deleted)]

    def list_events_by_person(self, workspace_id, person_id, *, include_deleted=False):
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(person_id, "person_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            if repositories.people.get(workspace_id, person_id, include_deleted=True) is None:
                raise NotFoundError("人物不属于当前 Workspace。")
            return [self._detail(repositories, event) for event in
                repositories.events.list_by_person(workspace_id, person_id, include_deleted=include_deleted)]

    def soft_delete_event(self, workspace_id, id):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, id, include_deleted=True)
            if old.deleted_at is not None:
                return old
            return repositories.events.soft_delete(workspace_id, id, self._changed_at(old))

    def restore_event(self, workspace_id, id):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, id, include_deleted=True)
            if old.deleted_at is None:
                return old
            # Recovery clears deleted_at; neither participants nor relationship states are rewritten.
            return repositories.events.restore(workspace_id, id, self._changed_at(old))

    def add_participant(self, workspace_id, event_id, person_id, role=None, note=None):
        item = ParticipantInput(person_id, role, note)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, event_id)
            self._validate_people(repositories, workspace_id, (item,))
            if any(entry.person_id == person_id for entry in repositories.participants.list_by_event(workspace_id, event_id)):
                raise ConflictError("该人物已经是 Event 的 Participant。")
            now = self._changed_at(old)
            participant = repositories.participants.add(EventParticipant(workspace_id=workspace_id, event_id=event_id,
                person_id=person_id, role=role, note=note, created_at=now))
            repositories.events.update(replace(old, updated_at=now))
            return participant

    def remove_participant(self, workspace_id, event_id, person_id):
        validate_uuid(person_id, "person_id")
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, event_id)
            current = repositories.participants.list_by_event(workspace_id, event_id)
            if not any(item.person_id == person_id for item in current):
                raise NotFoundError("人物不是当前 Event 的 Participant。")
            repositories.participants.remove(workspace_id, event_id, person_id)
            repositories.events.update(replace(old, updated_at=self._changed_at(old)))

    def attach_event_to_history(self, workspace_id, history_id, event_id, *, expected_event_id=ANY_EVENT):
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(history_id, "history_id")
        if event_id is not None:
            validate_uuid(event_id, "event_id")
        if expected_event_id is not ANY_EVENT and expected_event_id is not None:
            validate_uuid(expected_event_id, "expected_event_id")
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            history = repositories.histories.get(workspace_id, history_id)
            if history is None:
                raise WorkspaceMismatchError("History 不存在或不属于当前 Workspace。")
            if expected_event_id is not ANY_EVENT and history.event_id != expected_event_id:
                raise ConflictError("History 的 Event 关联已改变，请刷新详情后再操作；本次未修改关联。")
            relationship = repositories.relationships.get(workspace_id, history.relationship_id, include_deleted=True)
            if relationship is None:
                raise NotFoundError("History 对应的 Relationship 不存在。")
            if event_id is not None:
                # Retained deleted Events are valid historical references, not active events.
                if repositories.events.get(workspace_id, event_id, include_deleted=True) is None:
                    raise WorkspaceMismatchError("Event 不存在或不属于 History 的 Workspace。")
            return repositories.histories.attach_event(workspace_id, history_id, event_id)

    def attach_relationship_history(self, workspace_id, history_id, event_id, *, expected_event_id=ANY_EVENT):
        return self.attach_event_to_history(workspace_id, history_id, event_id,
                                           expected_event_id=expected_event_id)

    def list_attachable_histories(self, workspace_id):
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return [HistoryLink(history, relationship) for relationship in
                repositories.relationships.list_by_workspace(workspace_id, include_deleted=True) for history in
                repositories.histories.list_by_relationship(workspace_id, relationship.id)]
