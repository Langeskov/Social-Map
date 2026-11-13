"""Application-layer unit of work; repositories receive only the yielded Session."""
from contextlib import contextmanager
from collections.abc import Iterator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from app.storage.database import DatabaseError, log_database_error


@contextmanager
def transaction(factory: sessionmaker, *, write: bool = False) -> Iterator[Session]:
    """One operation, one Session, one transaction, deterministic close.

    Compose repository calls inside this scope. Repository code must not call
    commit/rollback or obtain its own Session/Engine. Do not pass the Session,
    live ORM entities or lazy loaders to UI/background threads.
    """
    try:
        with factory() as session:
            session.info["ris_write"] = write
            with session.begin():
                if write:
                    session.connection(execution_options={"ris_write": True})
                yield session
    except SQLAlchemyError as exc:
        log_database_error("service transaction (rolled back)", exc)
        raise DatabaseError("数据库操作失败，当前操作已回滚。请查看错误日志中的技术错误代码。") from exc
    except Exception as exc:
        log_database_error("service operation (rolled back)", exc)
        raise
