"""Shared real-SQLite fixture for the Phase 7 Timeline/Dashboard projections.

Both projection test modules build their world here through the public services
(two workspaces, people, Events, Relationships with append-only state history
and Evidence), so the assertions stay about projection semantics instead of
setup details. Every test module keeps its own pytest fixture with try/finally
``runtime.close()``; this module only knows how to build and observe the world.

The composition root provides the clock-free workspace/person/media services;
the three fact-writing services and both projections are built here with one
injected frozen clock, so relationship states, Event instants, Evidence
timestamps and the Dashboard's 7/30 day windows are all deterministic.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.dashboard import DashboardService
from app.services.database import initialize_database
from app.services.evidence import EvidenceService
from app.services.event import EventService
from app.services.relationship import RelationshipService
from app.services.timeline import TimelineService

NOW = datetime(2026, 10, 4, 9, 30, 0, 123456, tzinfo=timezone.utc)
DAY = date(2026, 10, 4)


@dataclass
class ProjectionEnv:
    config: AppConfig
    runtime: object
    workspaces: object
    people: object
    media: object
    events: object
    relationships: object
    evidence: object
    clock: SimpleNamespace
    ws: UUID
    foreign: UUID
    a: object
    b: object
    c: object
    timeline: TimelineService
    dashboard: DashboardService

    def advance(self, **delta):
        """Move the injected clock so consecutive writes get distinct timestamps."""
        self.clock.now = self.clock.now + timedelta(**delta)
        return self.clock.now


def build_env(tmp_path, name="projection-data") -> ProjectionEnv:
    config = AppConfig(data_dir=tmp_path / name)
    runtime = initialize_database(config)
    try:
        clock = SimpleNamespace(now=NOW)
        sessions = runtime.sessions
        services = create_application_services(config, sessions)
        workspace = services.workspaces.create(f"{name} workspace")
        foreign = services.workspaces.create(f"{name} other workspace")
        a = services.people.create(workspace.id, "Ada")
        b = services.people.create(workspace.id, "Bo")
        c = services.people.create(foreign.id, "Cy")
        return ProjectionEnv(config=config, runtime=runtime,
            workspaces=services.workspaces, people=services.people, media=services.media,
            events=EventService(sessions, clock=lambda: clock.now),
            relationships=RelationshipService(sessions, clock=lambda: clock.now),
            evidence=EvidenceService(sessions, clock=lambda: clock.now),
            clock=clock, ws=workspace.id, foreign=foreign.id, a=a, b=b, c=c,
            timeline=TimelineService(sessions, clock=lambda: clock.now),
            dashboard=DashboardService(sessions, clock=lambda: clock.now))
    except Exception:
        runtime.close()
        raise


def make_event(env, **changes):
    """Create an Event in the fixture workspace; defaults to a DATE Event."""
    values = dict(title="共同活动", event_type="Meeting", occurred_on=DAY, participants=())
    values.update(changes)
    return env.events.create_event(env.ws, **values)


def make_relationship(env, kind="Friend", *, a=None, b=None, state="ACTIVE", effective_at=None, note=None):
    return env.relationships.create(env.ws, (a or env.a).id, (b or env.b).id, kind,
        initial_state=state, effective_at=effective_at, note=note)


def make_history(env, relationship, state, *, effective_at=None, note=None):
    return env.relationships.add_history(env.ws, relationship.id, state,
        effective_at=effective_at, note=note)


def make_evidence(env, event, **changes):
    values = dict(kind="TEXT", title="聊天记录", content_text="原始聊天文字", source_type="CHAT")
    values.update(changes)
    return env.evidence.create_evidence(env.ws, event.id, **values)


@contextmanager
def count_selects(env):
    """Collect every SELECT issued inside the block, to pin "no N+1" behaviour."""
    from sqlalchemy import event as sa_event

    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sa_event.listen(env.runtime.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        sa_event.remove(env.runtime.engine, "before_cursor_execute", capture)


def database_state(env):
    """Every table and every row count: a projection must leave this unchanged."""
    from sqlalchemy import func, inspect, select

    from app.storage.database import Base
    with env.runtime.sessions() as session, session.begin():
        tables = tuple(sorted(inspect(session.get_bind()).get_table_names()))
        rows = tuple((table.name, session.scalar(select(func.count()).select_from(table)))
                     for table in Base.metadata.sorted_tables)
    return tables, rows
