"""File/database orchestration; compensation is deliberately conservative."""
from dataclasses import replace
import logging
from pathlib import Path
from uuid import UUID
from sqlalchemy.exc import IntegrityError
from app.domain.entities import Media, Person
from app.domain.errors import ConflictError, DomainError, MediaValidationError
from app.domain.validation import utc_now
from app.services.repositories import sql_repositories
from app.services.operations import write_operation
from app.services.unit_of_work import transaction
from app.services.rules import workspace_required, person_required, media_required
from app.services.person_links import attach_media
from app.storage.database import DatabaseError

logger = logging.getLogger(__name__)

class MediaService:
    def __init__(self, sessions, store, repositories=sql_repositories):
        self._sessions, self.store, self._repositories = sessions, store, repositories

    def get(self, workspace_id: UUID, media_id: UUID) -> Media:
        with transaction(self._sessions) as session:
            return media_required(self._repositories(session), workspace_id, media_id)

    def import_file(self, workspace_id: UUID, source: Path, *, person_id: UUID | None = None,
                    role: str = "GALLERY", _new_person: Person | None = None) -> Media:
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            if person_id is not None:
                person_required(repositories, workspace_id, person_id)
            if _new_person is not None and _new_person.workspace_id != workspace_id:
                raise DomainError("新人物与导入 Workspace 不一致。")
        staged = self.store.stage(Path(source))
        try:
            if (person_id is not None or _new_person is not None) and (not staged.mime_type.startswith("image/") or staged.width is None):
                raise MediaValidationError("头像和 Gallery 必须使用可解码的图片。")
            with self.store.content_lock(workspace_id, staged.sha256):
                receipt = self.store.publish(workspace_id, staged)
                try:
                    for attempt in range(2):
                        try:
                            return self._persist(workspace_id, staged, receipt, person_id, role, _new_person)
                        except DatabaseError as exc:
                            # Database UNIQUE is final authority even if a writer
                            # outside our lock protocol raced with this operation.
                            if attempt == 0 and isinstance(exc.__cause__, IntegrityError):
                                with transaction(self._sessions) as session:
                                    found = self._repositories(session).media.get_by_hash(workspace_id, staged.sha256, include_deleted=True)
                                if found is not None:
                                    continue
                            raise
                    raise ConflictError("媒体去重冲突，请重试操作。")
                except Exception:
                    self._compensate(workspace_id, staged.sha256, receipt)
                    raise
        finally:
            try:
                self.store.discard(staged)
            except Exception as exc:
                # Never misreport a committed operation as rolled back because
                # staging cleanup failed. Leftovers are not effective media.
                logger.error("Media staging cleanup failed: type=%s", type(exc).__name__)

    def _persist(self, workspace_id, staged, receipt, person_id, role, new_person):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            if new_person is not None:
                if repositories.people.get(workspace_id, new_person.id, include_deleted=True) is not None:
                    raise ConflictError("人物 ID 已存在。")
                repositories.people.save(new_person)
                person_id = new_person.id
            if person_id is not None:
                person_required(repositories, workspace_id, person_id)
            old = repositories.media.get_by_hash(workspace_id, staged.sha256, include_deleted=True)
            if old is not None:
                if old.storage_path != receipt.storage_path or old.size_bytes != staged.size_bytes:
                    raise MediaValidationError("已有媒体元数据与内容寻址文件不一致，未覆盖原文件。")
                media = repositories.media.save(replace(old, deleted_at=None, updated_at=utc_now())) if old.deleted_at else old
            else:
                media = repositories.media.save(Media(workspace_id=workspace_id, sha256=staged.sha256,
                    mime_type=staged.mime_type, size_bytes=staged.size_bytes, width=staged.width,
                    height=staged.height, storage_path=receipt.storage_path))
            if person_id is not None:
                attach_media(repositories, workspace_id, person_id, media.id, role)
            return media

    def _compensate(self, workspace_id, sha256, receipt):
        if not receipt.created_new:
            return
        try:
            # A new Session after rollback/commit failure is essential: the
            # original identity map cannot establish what actually committed.
            with transaction(self._sessions) as session:
                repositories = self._repositories(session)
                existing = repositories.media.get_by_hash(workspace_id, sha256, include_deleted=True)
                referenced = bool(existing and repositories.media.has_references(workspace_id, existing.id, active_only=True))
            self.store.delete_if_unreferenced(receipt, media_exists=existing is not None, references_exist=referenced)
        except Exception as exc:
            logger.error("Media compensation unconfirmed; file retained: type=%s", type(exc).__name__)

    def soft_delete(self, workspace_id: UUID, media_id: UUID) -> None:
        media = self.get(workspace_id, media_id)
        with self.store.content_lock(workspace_id, media.sha256):
            with write_operation(self._sessions) as session:
                repositories = self._repositories(session)
                media = media_required(repositories, workspace_id, media_id)
                if repositories.media.has_references(workspace_id, media_id):
                    raise ConflictError("媒体仍被人物或证据引用，不能删除。")
                now = utc_now()
                repositories.media.save(replace(media, deleted_at=now, updated_at=now))
        # Physical bytes remain immutable and available for reactivation.

    def original_path(self, workspace_id: UUID, media_id: UUID) -> Path:
        media = self.get(workspace_id, media_id)
        if not self.store.verify(media.storage_path, media.sha256):
            raise MediaValidationError("媒体文件丢失或校验失败。")
        return self.store.path_for(media.storage_path)
