"""Independent process persistence and real desktop startup acceptance on temporary data."""
import json
import os
from pathlib import Path
import subprocess
import sys
from app.config.settings import PROJECT_ROOT

WRITER = '''
import json, sys
from pathlib import Path
from datetime import date
from app.config.settings import AppConfig
from app.services.database import initialize_database
from app.services.application import create_application_services
runtime = initialize_database(AppConfig(data_dir=Path(sys.argv[1])))
try:
    services = create_application_services(AppConfig(data_dir=Path(sys.argv[1])), runtime.sessions)
    ws = services.initial_workspace.id
    a = services.people.create(ws, "Temporary fixture A")
    b = services.people.create(ws, "Temporary fixture B")
    relationship = services.relationships.create(ws, a.id, b.id, "Friend")
    history = services.relationships.history(ws, relationship.id)[0]
    event = services.events.create_event(ws, "Temporary acceptance record", "Meeting", "OBSERVATION",
        occurred_on=date(2026,9,30), participants=(a.id,b.id), description="User-recorded observation")
    services.events.attach_event_to_history(ws,history.id,event.id)
    services.people.soft_delete(ws,b.id)
    print(json.dumps(dict(ws=str(ws), event=str(event.id), a=str(a.id), b=str(b.id),
        history=str(history.id), relationship=str(relationship.id))))
finally:
    runtime.close()
'''

READER = '''
import json, sys
from pathlib import Path
from uuid import UUID
from app.config.settings import AppConfig
from app.services.database import initialize_database
from app.services.event import EventService
from app.services.relationship import RelationshipService
ids = json.loads(sys.argv[2])
ws, event_id = UUID(ids['ws']), UUID(ids['event'])
runtime = initialize_database(AppConfig(data_dir=Path(sys.argv[1])))
try:
    service = EventService(runtime.sessions)
    detail = service.detail(ws,event_id)
    assert detail.event.record_kind == "OBSERVATION"
    assert detail.event.occurred_on.isoformat() == "2026-09-30"
    assert len(detail.participants) == 2
    assert {str(p.person.id) for p in detail.participants} == {ids['a'],ids['b']}
    assert next(p for p in detail.participants if str(p.person.id)==ids['b']).person.deleted_at is not None
    assert len(detail.histories) == 1 and str(detail.histories[0].history.id)==ids['history']
    assert detail.histories[0].history.event_id == event_id
    relationship = RelationshipService(runtime.sessions).current(ws,UUID(ids['relationship']),include_deleted=True)
    assert relationship.current_state == "ACTIVE"
    assert len(service.list_events_by_person(ws,UUID(ids['b']))) == 1
    print("event/participants/history/state persisted")
finally:
    runtime.close()
'''


def run(args, cwd, environment):
    result = subprocess.run([sys.executable, "-s", *args], cwd=cwd, env=environment,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_event_links_survive_process_restarts_and_two_gui_startups(tmp_path):
    data = tmp_path / "process-fixture-data"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    ids = json.loads(run(["-c", WRITER, str(data)], tmp_path, environment))
    config = tmp_path / "runtime.toml"
    config.write_text('[storage]\ndata_dir="process-fixture-data"\n', encoding="utf-8")
    for _ in range(2):
        run(["-m", "app.main", "--config", str(config), "--smoke-test"], tmp_path, environment)
        output = run(["-c", READER, str(data), json.dumps(ids)], tmp_path, environment)
        assert "event/participants/history/state persisted" in output
    assert (data / "database.sqlite3").exists()
