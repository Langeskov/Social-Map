"""Unified search: real FTS5 index, workspace isolation and rebuildability."""
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import text
from app.config.settings import AppConfig
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.storage.models import SearchDocumentModel
from app.storage.search_projection import SearchProjectionStore, match_expression, tokenize

NOW = datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "search-data")
    runtime = initialize_database(config)
    try:
        services = create_application_services(config, runtime.sessions)
        workspace = services.initial_workspace
        other = services.workspaces.create("Other")
        yield SimpleEnv(config, runtime, services, workspace, other)
    finally:
        runtime.close()


class SimpleEnv:
    def __init__(self, config, runtime, services, workspace, other):
        self.config, self.runtime, self.services = config, runtime, services
        self.workspace, self.other = workspace, other

    @property
    def ws(self):
        return self.workspace.id

    @property
    def foreign(self):
        return self.other.id

    def search(self, query, **kwargs):
        return self.services.search.search(self.ws, query, **kwargs)

    def types(self, results):
        return [item.entity_type for item in results]

    def ids(self, results, entity_type=None):
        return [item.entity_id for item in results
                if entity_type is None or item.entity_type == entity_type]


def seed_people(env):
    person = env.services.people.create(env.ws, "张三", real_name="Zhang San", occupation="摄影师")
    env.services.people.add_alias(env.ws, person.id, "小三", alias_type="NICKNAME")
    other = env.services.people.create(env.ws, "李四")
    return person, other


def seed_event(env, title="摄影活动", participants=(), **changes):
    values = dict(record_kind="FACT", occurred_on=date(2026, 9, 30), participants=participants)
    values.update(changes)
    return env.services.events.create_event(env.ws, title, "活动", **values)


def seed_evidence(env, event, title="聊天记录", **changes):
    values = dict(content_text="原始聊天文字：明天一起拍照", source_type="CHAT")
    values.update(changes)
    return env.services.evidence.create_evidence(env.ws, event.id, "TEXT", title, **values)


def test_person_acknowledged_fields_and_alias_search(env):
    person, other = seed_people(env)
    assert env.ids(env.search("张三"), "PERSON") == [person.id]
    assert env.ids(env.search("Zhang San"), "PERSON") == [person.id]
    assert env.ids(env.search("小三"), "ALIAS") != []
    alias_hit = next(item for item in env.search("小三") if item.entity_type == "ALIAS")
    assert alias_hit.metadata["person_id"] == person.id
    assert alias_hit.workspace_id == env.ws
    assert env.search("李四")[0].entity_id == other.id
    # Only declared fields are searchable: occupation is not an indexed person field.
    assert env.search("摄影师") == []


def test_event_and_evidence_search_fields(env):
    person, _ = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    evidence = seed_evidence(env, event, annotation="活动前一天的记录", source_label="微信群")
    assert env.ids(env.search("摄影活动"), "EVENT") == [event.id]
    assert env.ids(env.search("活动"), "EVENT") == [event.id]
    assert env.ids(env.search("明天一起拍照"), "EVIDENCE") == [evidence.id]
    assert env.ids(env.search("活动前一天的记录"), "EVIDENCE") == [evidence.id]
    assert env.ids(env.search("微信群"), "EVIDENCE") == [evidence.id]
    hit = next(item for item in env.search("明天一起拍照") if item.entity_type == "EVIDENCE")
    assert hit.metadata["event_id"] == event.id
    assert "明天一起拍照" in hit.snippet
    link = env.services.evidence.create_evidence(env.ws, event.id, "LINK", "相关网页", source_type="WEB",
                                                 source_reference="https://example.invalid/thread")
    assert env.ids(env.search("example.invalid"), "EVIDENCE") == [link.id]


def test_workspace_isolation(env):
    person, _ = seed_people(env)
    foreign_person = env.services.people.create(env.foreign, "张三")
    foreign_event = env.services.events.create_event(env.foreign, "摄影活动", "活动", occurred_on=date(2026, 9, 30))
    env.services.evidence.create_evidence(env.foreign, foreign_event.id, "TEXT", "聊天记录",
                                          content_text="原始聊天文字", source_type="CHAT")
    results = env.search("张三")
    assert [item.workspace_id for item in results] == [env.ws]
    assert person.id in env.ids(results, "PERSON") and foreign_person.id not in env.ids(results, "PERSON")
    assert foreign_event.id not in env.ids(env.search("摄影活动"), "EVENT")
    assert foreign_event.id not in env.ids(env.search("原始聊天文字"), "EVIDENCE")
    with pytest.raises(NotFoundError):
        env.services.search.search(uuid4(), "张三")


def test_soft_delete_exclusion_and_include_deleted(env):
    person, other = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    evidence = seed_evidence(env, event)
    env.services.people.soft_delete(env.ws, other.id)
    env.services.people.soft_delete(env.ws, person.id)
    env.services.events.soft_delete_event(env.ws, event.id)
    assert env.search("李四") == []
    assert env.search("张三") == []
    assert env.search("小三") == []            # aliases inherit the archived person
    assert env.search("摄影活动") == []
    assert env.search("原始聊天文字") == []     # evidence of a deleted event follows it
    assert env.ids(env.search("李四", include_deleted=True), "PERSON") == [other.id]
    assert env.ids(env.search("张三", include_deleted=True), "PERSON") == [person.id]
    assert env.ids(env.search("摄影活动", include_deleted=True), "EVENT") == [event.id]
    assert env.ids(env.search("原始聊天文字", include_deleted=True), "EVIDENCE") == [evidence.id]
    # Restoring makes everything visible again without touching the projection by hand.
    env.services.events.restore_event(env.ws, event.id)
    assert env.ids(env.search("摄影活动"), "EVENT") == [event.id]
    assert env.ids(env.search("原始聊天文字"), "EVIDENCE") == [evidence.id]
    assert env.search("张三", include_deleted=True)[0].metadata["deleted"] is True


def test_evidence_soft_delete_is_searchable_until_deleted(env):
    person, _ = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    evidence = seed_evidence(env, event)
    env.services.evidence.soft_delete_evidence(env.ws, evidence.id)
    assert env.search("明天一起拍照") == []
    assert env.ids(env.search("明天一起拍照", include_deleted=True), "EVIDENCE") == [evidence.id]
    env.services.evidence.restore_evidence(env.ws, evidence.id)
    assert env.ids(env.search("明天一起拍照"), "EVIDENCE") == [evidence.id]


@pytest.mark.parametrize("query", ["*", '"', "AND", "OR", "NOT", "AND OR NOT", "a-b", "-", "^", "(", ")",
                                   "()", ":", "摄影 AND 活动", '张三" OR "李四', "NEAR(", "活动*"])
def test_special_characters_and_operators_never_break(env, query):
    person, _ = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    seed_evidence(env, event)
    results = env.search(query)          # must not raise for ordinary user text
    assert isinstance(results, list)


def test_operator_words_are_treated_as_literal_text(env):
    person, _ = seed_people(env)
    named = env.services.people.create(env.ws, "AND 会议")
    hits = env.search("AND 会议")
    assert named.id in env.ids(hits, "PERSON")
    assert all("AND" in item.title or "AND" in (item.snippet or "") for item in hits
               if item.entity_type == "PERSON")


def test_empty_and_whitespace_queries(env):
    seed_people(env)
    assert env.search("") == []
    assert env.search("   ") == []
    assert env.search("*") == []
    with pytest.raises(DomainError):
        env.services.search.search(env.ws, None)
    with pytest.raises(DomainError):
        env.services.search.search(env.ws, "张三", entity_types=["UNKNOWN"])


def test_fact_edits_update_the_projection(env):
    person, _ = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    evidence = seed_evidence(env, event)
    env.services.people.update(env.ws, person.id, display_name="张三丰")
    assert env.search("张三丰")
    env.services.events.update_event(env.ws, event.id, title="改名后的活动")
    assert env.search("改名后的活动") and not env.search("摄影活动")
    env.services.evidence.update_metadata(env.ws, evidence.id, title="改名后的证据", annotation="新注释关键词")
    assert env.search("改名后的证据") and env.search("新注释关键词")
    # Original material is still searchable after a metadata-only edit.
    assert env.search("明天一起拍照")
    alias = env.services.people.add_alias(env.ws, person.id, "新别称")
    assert env.ids(env.search("新别称"), "ALIAS") == [alias.id]
    env.services.people.remove_alias(env.ws, person.id, alias.id)
    assert env.search("新别称") == []


def test_rebuild_index_recovers_from_dropped_or_stale_projection(env):
    person, other = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    seed_evidence(env, event)
    expected_people = {person.id, other.id}
    with transaction(env.runtime.sessions, write=True) as session:
        rows = SearchProjectionStore(session).count(env.ws)
        assert rows >= 4
        SearchProjectionStore(session).drop_index()
        assert SearchProjectionStore(session).count() == 0
    # Facts survive a dropped projection, and search recreates it on demand.
    assert env.services.people.list(env.ws)
    assert env.ids(env.search("张三"), "PERSON") == [person.id]
    with transaction(env.runtime.sessions, write=True) as session:
        assert SearchProjectionStore(session).count(env.ws) >= 4
    rebuilt = env.services.search.rebuild_index(env.ws)
    assert rebuilt >= 4
    assert env.ids(env.search("张三"), "PERSON") == [person.id]
    assert env.ids(env.search("摄影活动"), "EVENT") == [event.id]
    assert env.search("明天一起拍照")
    with transaction(env.runtime.sessions) as session:
        assert SearchProjectionStore(session).count(env.ws) >= 4
    assert {item.entity_id for item in env.search("张三") if item.entity_type == "PERSON"} == {person.id}
    # A rebuild for one workspace leaves the other workspace's projection alone.
    foreign_person = env.services.people.create(env.foreign, "张三")
    env.services.search.rebuild_index(env.foreign)
    assert env.ids(env.search("张三"), "PERSON") == [person.id]
    assert env.search("张三", include_deleted=True)
    assert foreign_person.id not in env.ids(env.search("张三", include_deleted=True), "PERSON")


def test_rebuild_all_workspaces_and_counts(env):
    person, _ = seed_people(env)
    env.services.people.create(env.foreign, "外部人物")
    total = env.services.search.rebuild_index()
    assert total >= 3
    assert env.services.search.document_count(env.ws) >= 3          # person + alias + person
    assert env.services.search.document_count(env.foreign) == 1
    assert env.services.search.index_available() is True
    assert env.ids(env.search("张三"), "PERSON") == [person.id]


def test_limit_and_entity_type_filters(env):
    person, other = seed_people(env)
    event = seed_event(env, participants=(person.id,))
    seed_evidence(env, event)
    assert len(env.search("张", limit=1)) == 1
    assert env.types(env.search("张三", entity_types=["PERSON"])) == ["PERSON"]
    assert set(env.types(env.search("活动", entity_types=["EVENT"]))) <= {"EVENT"}
    assert env.search("张三", entity_types=["EVENT"]) == []
    with pytest.raises(DomainError):
        env.services.search.search(env.ws, "张三", entity_types=[])


def test_tokenizer_and_expression_builder_are_safe():
    assert tokenize("共同参加摄影活动") == "共 同 参 加 摄 影 活 动"
    assert tokenize("Meeting notes 2026") == "Meeting notes 2026"
    assert tokenize(None) == ""
    assert match_expression("摄影") == '"摄 影"'
    assert match_expression("photo") == '"photo"*'
    assert match_expression("摄影 photo") == '"摄 影" AND "photo"*'
    assert match_expression("") is None
    assert match_expression("***") is None
    assert match_expression('a" OR "b') == '"a"* AND "OR"* AND "b"*'


def test_projection_rows_are_never_authoritative(env):
    person, _ = seed_people(env)
    with transaction(env.runtime.sessions) as session:
        session.execute(SearchDocumentModel.__table__.delete())
    # A wiped projection cannot lose facts: the person is still there.
    assert env.services.people.get(env.ws, person.id).display_name == "张三"
    assert env.search("张三")
    with transaction(env.runtime.sessions) as session:
        count = session.execute(text("SELECT count(*) FROM search_document_fts")).scalar()
        assert count >= 1
