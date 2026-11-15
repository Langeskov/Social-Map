"""Unified search over the fact layer, served by the rebuildable FTS5 projection.

FTS5 never appears outside this service and its storage adapter: the service
returns immutable DTOs, always scoped to one workspace, and can rebuild the whole
index from person / person_alias / event / evidence at any time.
"""
from dataclasses import dataclass, field
from uuid import UUID
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import utc_now
from app.services.unit_of_work import transaction
from app.storage.models import WorkspaceModel
from app.storage.search_projection import (ENTITY_TYPES, SearchProjectionStore,
                                           collect_documents, match_expression)

SNIPPET_RADIUS = 40


@dataclass(frozen=True)
class SearchResult:
    """Projection result; carries ids and display text, never an ORM instance."""

    id: int
    entity_type: str
    entity_id: UUID
    workspace_id: UUID
    title: str
    snippet: str
    rank: float
    metadata: dict = field(default_factory=dict)


def build_snippet(body: str | None, query: str, *, fallback: str = "") -> str:
    """Locate the query inside the original text so CJK stays readable."""
    text = (body or "").strip()
    if not text:
        return fallback
    needle = "".join(part for part in query.split() if part)
    for candidate in (needle, query.strip()):
        if candidate and candidate in text:
            start = max(0, text.index(candidate) - SNIPPET_RADIUS)
            end = min(len(text), text.index(candidate) + len(candidate) + SNIPPET_RADIUS)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return f"{prefix}{text[start:end]}{suffix}"
    return text[:SNIPPET_RADIUS * 2] + ("…" if len(text) > SNIPPET_RADIUS * 2 else "")


class SearchService:
    def __init__(self, sessions, *, clock=utc_now):
        self._sessions, self._clock = sessions, clock

    def _workspace(self, session, workspace_id):
        workspace = session.get(WorkspaceModel, workspace_id)
        if workspace is None or workspace.deleted_at is not None:
            raise NotFoundError("Workspace 不存在或已归档。")
        return workspace

    def index_available(self) -> bool:
        with transaction(self._sessions) as session:
            return SearchProjectionStore(session).index_exists()

    def document_count(self, workspace_id: UUID | None = None) -> int:
        with transaction(self._sessions) as session:
            return SearchProjectionStore(session).count(workspace_id)

    def search(self, workspace_id: UUID, query: str, *, entity_types=None,
               include_deleted: bool = False, limit: int = 25) -> list[SearchResult]:
        if not isinstance(query, str):
            raise DomainError("搜索词必须是字符串。")
        if entity_types is not None:
            requested = tuple(entity_types)
            if not requested or set(requested) - set(ENTITY_TYPES):
                raise DomainError("entity_types 只能包含 PERSON、ALIAS、EVENT、EVIDENCE。")
            entity_types = requested
        expression = match_expression(query)
        with transaction(self._sessions) as session:
            self._workspace(session, workspace_id)
            if expression is None:
                return []
            store = SearchProjectionStore(session)
            if not store.index_exists() or store.count(workspace_id) == 0:
                # The projection is derived data: rebuild it rather than pretending
                # the workspace has no matches. A genuinely empty workspace simply
                # rebuilds to zero rows.
                self._rebuild(session, workspace_id)
            hits = store.match(workspace_id, expression, entity_types=entity_types,
                               include_deleted=include_deleted, limit=limit)
            return [SearchResult(hit.id, hit.entity_type, hit.entity_id, hit.workspace_id, hit.title,
                                 build_snippet(hit.body, query, fallback=hit.title), hit.rank,
                                 self._metadata(session, hit)) for hit in hits]

    def _metadata(self, session, hit) -> dict:
        metadata = {"occurred_at": hit.occurred_at, "deleted": hit.deleted_at is not None}
        if hit.entity_type in {"ALIAS", "EVIDENCE"} and hit.parent_id is not None:
            metadata["parent_id"] = hit.parent_id
        if hit.entity_type == "EVIDENCE":
            metadata["event_id"] = hit.parent_id
        if hit.entity_type == "ALIAS":
            metadata["person_id"] = hit.parent_id
        if hit.entity_type == "EVENT":
            metadata["event_id"] = hit.entity_id
        if hit.entity_type == "PERSON":
            metadata["person_id"] = hit.entity_id
        return metadata

    def _rebuild(self, session, workspace_id: UUID | None) -> int:
        store = SearchProjectionStore(session)
        store.clear(workspace_id)
        documents = collect_documents(session, workspace_id)
        for document in documents:
            store.upsert(document)
        return len(documents)

    def rebuild_index(self, workspace_id: UUID | None = None) -> int:
        """Drop and re-derive the projection; fact tables are never written."""
        from app.services.operations import write_operation
        with write_operation(self._sessions) as session:
            if workspace_id is not None:
                self._workspace(session, workspace_id)
            return self._rebuild(session, workspace_id)
