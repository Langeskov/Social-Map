"""Search projection storage: a rebuildable FTS5 index over the fact layer.

Nothing here is a source of truth. `search_document` and `search_document_fts`
are derived from person / person_alias / event / evidence and can be dropped and
rebuilt at any time without touching a single fact row.

CJK tokenisation: the FTS5 `unicode61` tokenizer would treat a whole Chinese
phrase as one token, so `摄影` could never match `共同参加摄影活动`. Indexed text
therefore separates each CJK character with a space and keeps Latin/digit runs
whole; queries are built from the same tokenisation as quoted phrases, so raw
user input never reaches MATCH and FTS5 operators cannot break a query.
"""
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from uuid import UUID
from sqlalchemy import delete, select, text
import sqlalchemy as sa
from app.domain.entities import Evidence, Event, Person
from app.storage.models import SearchDocumentModel

FTS_TABLE = "search_document_fts"
CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff"
TOKEN_PATTERN = re.compile(rf"[{CJK_CLASS}]+|[^\W_]+", re.UNICODE)
CJK_PATTERN = re.compile(rf"[{CJK_CLASS}]")

ENTITY_TYPES = ("PERSON", "ALIAS", "EVENT", "EVIDENCE")


def tokenize(value: str | None) -> str:
    """Split CJK runs into single characters, keep Latin/digit runs whole."""
    if not value:
        return ""
    tokens = []
    for run in TOKEN_PATTERN.findall(value):
        if CJK_PATTERN.match(run[0]):
            tokens.extend(run)
        else:
            tokens.append(run)
    return " ".join(tokens)


def match_expression(query: str | None) -> str | None:
    """Build a safe FTS5 expression from ordinary user text.

    Returns None when nothing searchable remains (empty query or only symbols),
    so callers can short-circuit instead of running a meaningless MATCH.
    """
    if not query:
        return None
    clauses = []
    for run in TOKEN_PATTERN.findall(query):
        if CJK_PATTERN.match(run[0]):
            clauses.append('"' + " ".join(run) + '"')
        else:
            clauses.append('"' + run + '"*')
    return " AND ".join(clauses) or None


@dataclass(frozen=True)
class SearchDocument:
    """A derived projection row; never built from anything but the fact layer."""

    workspace_id: UUID
    entity_type: str
    entity_id: UUID
    title: str
    updated_at: datetime
    parent_id: UUID | None = None
    body: str | None = None
    occurred_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self):
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown search entity_type: {self.entity_type}")


@dataclass(frozen=True)
class SearchHit:
    id: int
    workspace_id: UUID
    entity_type: str
    entity_id: UUID
    parent_id: UUID | None
    title: str
    body: str | None
    occurred_at: datetime | None
    deleted_at: datetime | None
    rank: float


class SearchProjectionStore:
    """Session-injected projection adapter: reads/writes only projection objects."""

    def __init__(self, session):
        self.session = session

    # ------------------------------------------------------------------ schema
    def index_exists(self) -> bool:
        return self.session.scalar(text(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": FTS_TABLE}) > 0

    def ensure_index(self) -> None:
        """Create the FTS5 object when missing (rebuild resilience, raw fixtures).

        Creating and dropping this virtual table makes SQLite use a temporary file;
        when the host denies its temporary directory SQLite reports SQLITE_CANTOPEN
        ("unable to open database file") instead of a permission error.
        """
        if not self.index_exists():
            self.session.execute(text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(title_tokens, body_tokens)"))

    def drop_index(self) -> None:
        self.ensure_index()
        self.session.execute(text(f"DROP TABLE IF EXISTS {FTS_TABLE}"))
        self.session.execute(delete(SearchDocumentModel))

    # ------------------------------------------------------------------- write
    def upsert(self, document: SearchDocument) -> int:
        self.ensure_index()
        row = self.session.scalar(select(SearchDocumentModel).where(
            SearchDocumentModel.workspace_id == document.workspace_id,
            SearchDocumentModel.entity_type == document.entity_type,
            SearchDocumentModel.entity_id == document.entity_id))
        title_tokens = tokenize(document.title)
        body_tokens = tokenize(document.body)
        if row is None:
            row = SearchDocumentModel(
                workspace_id=document.workspace_id, entity_type=document.entity_type,
                entity_id=document.entity_id, parent_id=document.parent_id, title=document.title,
                body=document.body, title_tokens=title_tokens, body_tokens=body_tokens,
                occurred_at=document.occurred_at, deleted_at=document.deleted_at,
                updated_at=document.updated_at)
            self.session.add(row)
            self.session.flush()
        else:
            row.parent_id, row.title, row.body = document.parent_id, document.title, document.body
            row.title_tokens, row.body_tokens = title_tokens, body_tokens
            row.occurred_at, row.deleted_at, row.updated_at = (
                document.occurred_at, document.deleted_at, document.updated_at)
            self.session.flush()
        # The FTS table stores its own copy keyed by the projection row id.
        self.session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :rowid"), {"rowid": row.id})
        self.session.execute(text(
            f"INSERT INTO {FTS_TABLE}(rowid, title_tokens, body_tokens) VALUES (:rowid, :title, :body)"),
            {"rowid": row.id, "title": title_tokens, "body": body_tokens})
        return row.id

    def remove(self, workspace_id: UUID, entity_type: str, entity_id: UUID) -> None:
        row = self.session.scalar(select(SearchDocumentModel).where(
            SearchDocumentModel.workspace_id == workspace_id,
            SearchDocumentModel.entity_type == entity_type,
            SearchDocumentModel.entity_id == entity_id))
        if row is None:
            return
        self.session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :rowid"), {"rowid": row.id})
        self.session.execute(delete(SearchDocumentModel).where(SearchDocumentModel.id == row.id))

    def clear(self, workspace_id: UUID | None = None) -> None:
        self.ensure_index()
        if workspace_id is None:
            self.session.execute(text(f"DELETE FROM {FTS_TABLE}"))
            self.session.execute(delete(SearchDocumentModel))
            return
        rows = self.session.scalars(select(SearchDocumentModel.id).where(
            SearchDocumentModel.workspace_id == workspace_id)).all()
        for row_id in rows:
            self.session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :rowid"), {"rowid": row_id})
        self.session.execute(delete(SearchDocumentModel).where(
            SearchDocumentModel.workspace_id == workspace_id))

    # -------------------------------------------------------------------- read
    def count(self, workspace_id: UUID | None = None) -> int:
        query = select(SearchDocumentModel.id)
        if workspace_id is not None:
            query = query.where(SearchDocumentModel.workspace_id == workspace_id)
        return len(self.session.scalars(query).all())

    def match(self, workspace_id: UUID, expression: str, *, entity_types=None,
              include_deleted: bool = False, limit: int | None = 25) -> list[SearchHit]:
        # The FTS5 object must be in FROM for `MATCH` and `bm25()` to resolve; it is
        # joined by rowid to the projection row that carries workspace/scope data.
        fts = sa.table(FTS_TABLE, sa.column("rowid", sa.Integer))
        query = (sa.select(SearchDocumentModel, sa.literal_column(f"bm25({FTS_TABLE})").label("rank"))
                 .select_from(SearchDocumentModel.__table__.join(fts, fts.c.rowid == SearchDocumentModel.id))
                 .where(sa.text(f"{FTS_TABLE} MATCH :expression"),
                        SearchDocumentModel.workspace_id == workspace_id))
        if entity_types:
            query = query.where(SearchDocumentModel.entity_type.in_(tuple(entity_types)))
        if not include_deleted:
            query = query.where(SearchDocumentModel.deleted_at.is_(None))
        query = query.order_by(sa.literal_column("rank"), SearchDocumentModel.entity_type,
                               SearchDocumentModel.entity_id)
        if limit is not None:
            query = query.limit(limit)
        rows = self.session.execute(query, {"expression": expression}).all()
        return [SearchHit(row[0].id, row[0].workspace_id, row[0].entity_type, row[0].entity_id,
                          row[0].parent_id, row[0].title, row[0].body, row[0].occurred_at,
                          row[0].deleted_at, float(row[1])) for row in rows]


def person_document(person: Person) -> SearchDocument:
    body = " ".join(part for part in (person.real_name,) if part)
    return SearchDocument(workspace_id=person.workspace_id, entity_type="PERSON", entity_id=person.id,
                          title=person.display_name, body=body or None, occurred_at=person.created_at,
                          deleted_at=person.deleted_at, updated_at=person.updated_at)


def alias_document(alias, person: Person) -> SearchDocument:
    return SearchDocument(workspace_id=alias.workspace_id, entity_type="ALIAS", entity_id=alias.id,
                          parent_id=person.id, title=alias.alias, body=alias.alias_type,
                          occurred_at=alias.created_at, deleted_at=person.deleted_at,
                          updated_at=alias.updated_at)


def event_document(event: Event) -> SearchDocument:
    # A date event keeps its DATE meaning; the sort/display instant is that day's
    # UTC midnight, computed here only for the projection.
    occurred = event.started_at
    if occurred is None and event.occurred_on is not None:
        occurred = datetime.combine(event.occurred_on, time.min, tzinfo=timezone.utc)
    body = " ".join(part for part in (event.event_type, event.description, event.location) if part)
    return SearchDocument(workspace_id=event.workspace_id, entity_type="EVENT", entity_id=event.id,
                          title=event.title, body=body or None, occurred_at=occurred,
                          deleted_at=event.deleted_at, updated_at=event.updated_at)


def evidence_document(evidence: Evidence, event: Event | None) -> SearchDocument:
    body = " ".join(part for part in (evidence.content_text, evidence.annotation,
                                      evidence.source_label, evidence.source_reference) if part)
    # Evidence of a deleted Event is hidden by default too, exactly like its Event.
    hidden_at = evidence.deleted_at or (event.deleted_at if event is not None else None)
    return SearchDocument(workspace_id=evidence.workspace_id, entity_type="EVIDENCE",
                          entity_id=evidence.id, parent_id=evidence.event_id, title=evidence.title,
                          body=body or None, occurred_at=evidence.captured_at or evidence.created_at,
                          deleted_at=hidden_at, updated_at=evidence.updated_at)


# --------------------------------------------------------------------- fact reads
from app.domain.entities import PersonAlias  # noqa: E402  (kept next to its builder)
from app.storage.models import (EventModel, EvidenceModel, PersonAliasModel,  # noqa: E402
                                PersonModel)
from app.storage.mapping import to_domain as snapshot  # noqa: E402


def collect_documents(session, workspace_id: UUID | None = None) -> list[SearchDocument]:
    """Re-derive every projection row from the fact layer (one query per type)."""
    documents: list[SearchDocument] = []
    people_query = select(PersonModel)
    alias_query = select(PersonAliasModel).join(
        PersonModel, (PersonModel.id == PersonAliasModel.person_id)
        & (PersonModel.workspace_id == PersonAliasModel.workspace_id))
    event_query = select(EventModel)
    evidence_query = select(EvidenceModel)
    if workspace_id is not None:
        people_query = people_query.where(PersonModel.workspace_id == workspace_id)
        alias_query = alias_query.where(PersonAliasModel.workspace_id == workspace_id)
        event_query = event_query.where(EventModel.workspace_id == workspace_id)
        evidence_query = evidence_query.where(EvidenceModel.workspace_id == workspace_id)
    people = {model.id: snapshot(Person, model) for model in session.scalars(people_query)}
    documents.extend(person_document(person) for person in people.values())
    for model in session.scalars(alias_query):
        owner = people.get(model.person_id)
        if owner is not None:
            documents.append(alias_document(snapshot(PersonAlias, model), owner))
    events = {model.id: snapshot(Event, model) for model in session.scalars(event_query)}
    documents.extend(event_document(event) for event in events.values())
    for model in session.scalars(evidence_query):
        evidence = snapshot(Evidence, model)
        documents.append(evidence_document(evidence, events.get(evidence.event_id)))
    return documents


# ------------------------------------------------------------------------ sync
def _sync(session, documents, removals=()) -> None:
    store = SearchProjectionStore(session)
    for workspace_id, entity_type, entity_id in removals:
        store.remove(workspace_id, entity_type, entity_id)
    for document in documents:
        store.upsert(document)


def sync_person(session, person: Person) -> None:
    """Refresh one person row plus (via its aliases) the archived state they inherit."""
    _sync(session, [person_document(person)])
    sync_aliases(session, person)


def sync_aliases(session, person: Person) -> None:
    models = session.scalars(select(PersonAliasModel).where(
        PersonAliasModel.workspace_id == person.workspace_id,
        PersonAliasModel.person_id == person.id)).all()
    store = SearchProjectionStore(session)
    for model in models:
        store.upsert(alias_document(snapshot(PersonAlias, model), person))
    return None


def remove_alias(session, workspace_id: UUID, alias_id: UUID) -> None:
    SearchProjectionStore(session).remove(workspace_id, "ALIAS", alias_id)


def sync_alias_row(session, alias: PersonAlias) -> None:
    """Refresh one alias row; it inherits the archived state of its person."""
    person_model = session.get(PersonModel, alias.person_id)
    if person_model is None:
        return
    SearchProjectionStore(session).upsert(alias_document(alias, snapshot(Person, person_model)))


def sync_event(session, event: Event) -> None:
    _sync(session, [event_document(event)])


def sync_evidence(session, evidence: Evidence) -> None:
    event_model = session.get(EventModel, evidence.event_id)
    event = snapshot(Event, event_model) if event_model is not None else None
    _sync(session, [evidence_document(evidence, event)])


def remove_evidence(session, workspace_id: UUID, evidence_id: UUID) -> None:
    SearchProjectionStore(session).remove(workspace_id, "EVIDENCE", evidence_id)

