"""Service-side validation of active workspace-scoped Domain values."""
from uuid import UUID
from app.domain.errors import NotFoundError, WorkspaceMismatchError, DomainError
from app.services.repositories import Repositories


def workspace_required(repositories: Repositories, workspace_id: UUID):
    entity = repositories.workspaces.get(workspace_id)
    if entity is None:
        raise NotFoundError("Workspace 不存在或已归档。")
    return entity


def person_required(repositories: Repositories, workspace_id: UUID, person_id: UUID):
    workspace_required(repositories, workspace_id)
    entity = repositories.people.get(workspace_id, person_id)
    if entity is None:
        raise NotFoundError("人物不属于当前 Workspace，或已经归档。")
    return entity


def media_required(repositories: Repositories, workspace_id: UUID, media_id: UUID):
    workspace_required(repositories, workspace_id)
    entity = repositories.media.get(workspace_id, media_id)
    if entity is None:
        raise WorkspaceMismatchError("媒体不属于当前 Workspace，或已删除。")
    return entity


def require_image(media):
    if not media.mime_type.startswith("image/") or media.width is None or media.height is None:
        raise DomainError("头像和 Gallery 只能引用已验证的图片。")
