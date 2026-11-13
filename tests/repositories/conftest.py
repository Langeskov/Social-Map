"""Repository integration fixtures always exercise the real Alembic schema."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config.settings import AppConfig
from app.domain.entities import Media, Person, PersonAlias, PersonMedia, Workspace
from app.services.database import initialize_database
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction


@pytest.fixture
def runtime(tmp_path):
    database = initialize_database(AppConfig(data_dir=tmp_path))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def now():
    return datetime(2025, 3, 4, 5, 6, 7, 123456, tzinfo=timezone.utc)


@pytest.fixture
def records(runtime, now):
    workspace = Workspace(id=uuid4(), name="CaseWorkspace", description="description",
                          created_at=now, updated_at=now)
    person = Person(id=uuid4(), workspace_id=workspace.id, display_name="Same Name",
                    real_name="Real Person", gender="unspecified", occupation="Engineer",
                    organization="Org", location="City", notes="notes", created_at=now, updated_at=now)
    alias = PersonAlias(id=uuid4(), workspace_id=workspace.id, person_id=person.id,
                        alias="First Alias", alias_type="NICKNAME", note="alias note",
                        created_at=now, updated_at=now)
    media = Media(id=uuid4(), workspace_id=workspace.id, sha256="a" * 64,
                  mime_type="image/png", size_bytes=42, width=20, height=30,
                  storage_path="media/image.png", created_at=now, updated_at=now)
    link = PersonMedia(id=uuid4(), workspace_id=workspace.id, person_id=person.id,
                       media_id=media.id, role="GALLERY", sort_order=2, created_at=now)
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        for repository, entity in zip(
            (repositories.workspaces, repositories.people, repositories.aliases,
             repositories.media, repositories.person_media),
            (workspace, person, alias, media, link), strict=True,
        ):
            assert repository.save(entity) == entity
    return workspace, person, alias, media, link
