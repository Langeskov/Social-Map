"""Evidence facts only: original material is create-once, metadata is editable.

No analysis, summary, inference, OCR or automatic extraction happens here. The
service never converts evidence into Events, Relationships or history.
"""
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID
from app.domain.entities import Evidence, EvidenceMedia, Event, Media
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid
from app.services.evidence_repositories import sql_evidence_repositories
from app.services.operations import write_operation
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

# "The caller did not supply a known attachment snapshot" and "any role".
ANY_ATTACHMENTS = object()
ANY_ROLE = object()

MEDIA_KINDS = {"IMAGE", "FILE"}


@dataclass(frozen=True)
class MediaInput:
    media_id: UUID
    role: str | None = None

    def __post_init__(self):
        validate_uuid(self.media_id, "media_id")
        role = self.role
        if role is not None:
            if not isinstance(role, str):
                raise DomainError("role must be a string")
            role = role.strip() or None
            validate_text(role, "role", max_length=100, optional=True)
            object.__setattr__(self, "role", role)


@dataclass(frozen=True)
class MediaLinkView:
    link: EvidenceMedia
    media: Media


@dataclass(frozen=True)
class EvidenceDetail:
    evidence: Evidence
    event: Event
    media: tuple[MediaLinkView, ...]


class EvidenceService:
    KINDS = ("TEXT", "IMAGE", "FILE", "LINK", "MANUAL", "OTHER")
    SOURCE_TYPES = ("CHAT", "EMAIL", "DOCUMENT", "IMAGE", "FILE", "WEB", "MANUAL", "OTHER")
    METADATA = {"title", "annotation"}

    def __init__(self, sessions, repositories=sql_evidence_repositories, *, clock=utc_now):
        self._sessions, self._repositories, self._clock = sessions, repositories, clock

    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    def _changed_at(self, old):
        return max(self._now(), old.updated_at + timedelta(microseconds=1))

    def _event(self, repositories, workspace_id, event_id, *, allow_deleted=False):
        validate_uuid(event_id, "event_id")
        event = repositories.events.get(workspace_id, event_id, include_deleted=True)
        if event is None:
            raise WorkspaceMismatchError("Event 不存在或不属于当前 Workspace。")
        if event.deleted_at is not None and not allow_deleted:
            raise DomainError("已删除的 Event 不能新增或修改证据；请先恢复该事件。")
        return event

    def _get(self, repositories, workspace_id, id, *, include_deleted=False):
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(id, "evidence_id")
        workspace_required(repositories, workspace_id)
        evidence = repositories.evidence.get(workspace_id, id, include_deleted=include_deleted)
        if evidence is None:
            raise NotFoundError("证据不属于当前 Workspace、已删除或不存在。")
        return evidence

    def _media_inputs(self, media):
        if media is None or isinstance(media, (str, bytes)):
            raise DomainError("media 必须是 UUID 或 MediaInput 的序列。")
        try:
            items = tuple(MediaInput(item) if isinstance(item, UUID) else item for item in media)
        except TypeError as exc:
            raise DomainError("media 必须是 UUID 或 MediaInput 的序列。") from exc
        if any(not isinstance(item, MediaInput) for item in items):
            raise DomainError("media 仅接受 UUID 或 MediaInput。")
        keys = [(item.media_id, item.role) for item in items]
        if len(set(keys)) != len(keys):
            raise ConflictError("同一个 Media 与 role 在同一 Evidence 中只能出现一次。")
        return items

    @staticmethod
    def _check_kind_rules(kind, content_text, source_reference, items, title):
        # Business rules live here, not in SQLite CHECK constraints.
        if kind == "TEXT" and not (content_text or "").strip():
            raise DomainError("TEXT 证据必须提供 content_text。")
        if kind in MEDIA_KINDS and not items:
            raise DomainError(f"{kind} 证据必须至少附加一个 Media。")
        if kind == "LINK" and not (source_reference or "").strip():
            raise DomainError("LINK 证据必须提供 source_reference。")
        if kind == "OTHER" and not (title or "").strip():
            raise DomainError("OTHER 证据必须提供 title。")

    def _detail(self, repositories, evidence):
        event = repositories.events.get(evidence.workspace_id, evidence.event_id, include_deleted=True)
        if event is None:
            raise NotFoundError("证据对应的 Event 缺失。")
        media = []
        for link in repositories.evidence_media.list_by_evidence(evidence.workspace_id, evidence.id):
            found = repositories.media.get(evidence.workspace_id, link.media_id, include_deleted=True)
            if found is None:
                raise NotFoundError("证据附件对应的 Media 缺失。")
            media.append(MediaLinkView(link, found))
        return EvidenceDetail(evidence, event, tuple(media))

    @staticmethod
    def attachment_ids(detail: EvidenceDetail) -> frozenset:
        """Snapshot helper for UI callers that must detect stale attachment views."""
        return frozenset(view.media.id for view in detail.media)

    def _guard_snapshot(self, repositories, workspace_id, evidence_id, expected):
        if expected is ANY_ATTACHMENTS:
            return
        current = frozenset(link.media_id for link in
                            repositories.evidence_media.list_by_evidence(workspace_id, evidence_id))
        if frozenset(expected or ()) != current:
            raise ConflictError("证据附件已被其他操作改变，请刷新后重试；本次未修改附件。")

    def create_evidence(self, workspace_id, event_id, kind, title, *, source_type="MANUAL",
                        content_text=None, annotation=None, source_label=None, source_reference=None,
                        captured_at=None, media=()):
        items = self._media_inputs(media)
        self._check_kind_rules(kind, content_text, source_reference, items, title)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            validate_uuid(workspace_id, "workspace_id")
            workspace_required(repositories, workspace_id)
            self._event(repositories, workspace_id, event_id)
            for item in items:
                if repositories.media.get(workspace_id, item.media_id) is None:
                    raise WorkspaceMismatchError("Media 不存在、已删除或不属于当前 Workspace。")
            now = self._now()
            entity = Evidence(workspace_id=workspace_id, event_id=event_id, kind=kind, title=title,
                              source_type=source_type, content_text=content_text, annotation=annotation,
                              source_label=source_label, source_reference=source_reference,
                              captured_at=captured_at, created_at=now, updated_at=now)
            saved = repositories.evidence.save(entity)
            for item in items:
                repositories.evidence_media.add(EvidenceMedia(workspace_id=workspace_id, evidence_id=saved.id,
                    media_id=item.media_id, role=item.role, created_at=now))
            return saved

    def update_metadata(self, workspace_id, evidence_id, **changes):
        if set(changes) - self.METADATA:
            raise DomainError("原始证据内容不可修改；只允许修改 title 与 annotation。")
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, evidence_id)
            # Re-validate through the Domain entity so text rules stay in one place.
            entity = replace(old, **changes, updated_at=self._changed_at(old))
            return repositories.evidence.update_metadata(entity)

    def get(self, workspace_id, evidence_id, *, include_deleted=False):
        with transaction(self._sessions) as session:
            return self._get(self._repositories(session), workspace_id, evidence_id,
                             include_deleted=include_deleted)

    def detail(self, workspace_id, evidence_id, *, include_deleted=False):
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            return self._detail(repositories, self._get(repositories, workspace_id, evidence_id,
                                                         include_deleted=include_deleted))

    def list_evidence(self, workspace_id, event_id, *, include_deleted=False, kind=None):
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            event = self._event(repositories, workspace_id, event_id, allow_deleted=True)
            # A deleted Event hides its evidence unless the caller audits it.
            if event.deleted_at is not None and not include_deleted:
                return []
            return [self._detail(repositories, item) for item in repositories.evidence.list_by_event(
                workspace_id, event_id, include_deleted=include_deleted, kind=kind)]

    def list_by_event(self, workspace_id, event_id, *, include_deleted=False, kind=None):
        return self.list_evidence(workspace_id, event_id, include_deleted=include_deleted, kind=kind)

    def list_by_workspace(self, workspace_id, *, include_deleted=False, kind=None):
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return [self._detail(repositories, item) for item in repositories.evidence.list_by_workspace(
                workspace_id, include_deleted=include_deleted, kind=kind)]

    def list_attachable_media(self, workspace_id, *, search=""):
        validate_uuid(workspace_id, "workspace_id")
        validate_text(search, "search", nonempty=False)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            media = repositories.media.list(workspace_id)
        needle = search.strip().lower()
        if not needle:
            return media
        return [item for item in media if needle in item.mime_type.lower() or needle in item.storage_path.lower()]

    def soft_delete_evidence(self, workspace_id, evidence_id):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, evidence_id, include_deleted=True)
            if old.deleted_at is not None:
                return old
            return repositories.evidence.soft_delete(workspace_id, evidence_id, self._changed_at(old))

    def restore_evidence(self, workspace_id, evidence_id):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, evidence_id, include_deleted=True)
            if old.deleted_at is None:
                return old
            # Recovery only clears deleted_at: Event, media bindings and media bytes
            # are untouched, and no relationship or history is written.
            return repositories.evidence.restore(workspace_id, evidence_id, self._changed_at(old))

    def add_media(self, workspace_id, evidence_id, media_id, role=None, *, expected_attachment_ids=ANY_ATTACHMENTS):
        item = MediaInput(media_id, role)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, evidence_id)
            # Attachment changes are link-only operations: like Phase 5 history
            # links, they stay available while the owning Event is soft deleted.
            if repositories.media.get(workspace_id, item.media_id) is None:
                raise WorkspaceMismatchError("Media 不存在、已删除或不属于当前 Workspace。")
            self._guard_snapshot(repositories, workspace_id, evidence_id, expected_attachment_ids)
            existing = repositories.evidence_media.list_by_evidence(workspace_id, evidence_id)
            if any(link.media_id == item.media_id and link.role == item.role for link in existing):
                raise ConflictError("该 Media 已经附加到此证据。")
            now = self._changed_at(old)
            link = repositories.evidence_media.add(EvidenceMedia(workspace_id=workspace_id,
                evidence_id=evidence_id, media_id=item.media_id, role=item.role, created_at=now))
            repositories.evidence.update_metadata(replace(old, updated_at=now))
            return link

    def remove_media(self, workspace_id, evidence_id, media_id, *, role=ANY_ROLE,
                     expected_attachment_ids=ANY_ATTACHMENTS):
        validate_uuid(media_id, "media_id")
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = self._get(repositories, workspace_id, evidence_id)
            self._guard_snapshot(repositories, workspace_id, evidence_id, expected_attachment_ids)
            links = repositories.evidence_media.list_by_evidence(workspace_id, evidence_id)
            matching = [link for link in links
                        if link.media_id == media_id and (role is ANY_ROLE or link.role == role)]
            if not matching:
                raise NotFoundError("该 Media 不是当前证据的附件。")
            matching_ids = {link.id for link in matching}
            remaining = [link for link in links if link.id not in matching_ids]
            if old.kind in MEDIA_KINDS and not remaining:
                raise ConflictError("IMAGE／FILE 证据必须至少保留一个附件；"
                                    "若附件确实有误，请软删除该证据后重新创建。")
            repositories.evidence_media.remove(workspace_id, evidence_id, media_id,
                                               None if role is ANY_ROLE else role)
            repositories.evidence.update_metadata(replace(old, updated_at=self._changed_at(old)))
