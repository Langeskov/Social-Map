"""Spawn-importable workers; only scalar paths/IDs cross process boundaries."""
from pathlib import Path
from uuid import UUID


def import_media_worker(data_dir, source, workspace_id, barrier, results):
    """Open our own engine/Session factory; parent alone initializes migrations."""
    from app.config.settings import AppConfig
    from app.services.application import create_application_services
    from app.services.database import DatabaseRuntime
    from app.storage.database import create_sqlite_engine
    from app.storage.session import create_session_factory

    runtime = None
    try:
        config = AppConfig(data_dir=Path(data_dir))
        engine = create_sqlite_engine(config)
        runtime = DatabaseRuntime(engine, create_session_factory(engine))
        services = create_application_services(config, runtime.sessions)
        barrier.wait(timeout=30)
        media = services.media.import_file(UUID(workspace_id), Path(source))
        results.put(("ok", str(media.id), media.storage_path))
    except Exception as exc:
        # Never return exception text, SQL, paths, or person/profile data.
        results.put(("error", type(exc).__name__))
    finally:
        if runtime is not None:
            runtime.close()
