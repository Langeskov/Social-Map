"""PersonMedia rules inside the caller's Service transaction."""
from uuid import UUID
from app.domain.entities import PersonMedia
from app.domain.errors import DomainError
from app.services.rules import person_required, media_required, require_image


def attach_media(repositories, workspace_id: UUID, person_id: UUID, media_id: UUID, role: str) -> PersonMedia:
    person_required(repositories, workspace_id, person_id)
    media = media_required(repositories, workspace_id, media_id)
    require_image(media)
    if role not in {"AVATAR", "GALLERY"}:
        raise DomainError("图片引用角色必须为 AVATAR 或 GALLERY。")
    existing = repositories.person_media.list(workspace_id, person_id)
    matching = next((link for link in existing if link.role == role and link.media_id == media_id), None)
    if matching:
        return matching
    if role == "AVATAR":
        repositories.person_media.remove_for_person(workspace_id, person_id, "AVATAR")
        sort_order = 0
    else:
        sort_order = max((link.sort_order for link in existing if link.role == "GALLERY"), default=-1) + 1
    return repositories.person_media.save(PersonMedia(workspace_id=workspace_id, person_id=person_id,
                                                     media_id=media_id, role=role, sort_order=sort_order))
