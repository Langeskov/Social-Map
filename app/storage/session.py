"""Short-lived session factory with enforced owning-thread checks."""
from threading import get_ident
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

class ThreadBoundSession(Session):
    """No global Session: the instance belongs to its creating thread."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._owner_thread = get_ident()

    def check_thread(self):
        if get_ident() != self._owner_thread:
            raise RuntimeError("Session 不得跨线程使用；请为后台任务创建独立 Session。")

    def add(self, instance, *, _warn=True):
        self.check_thread()
        return super().add(instance, _warn=_warn)

    def add_all(self, instances):
        self.check_thread()
        return super().add_all(instances)

    def delete(self, instance):
        self.check_thread()
        return super().delete(instance)

    def get(self, *args, **kwargs):
        self.check_thread()
        return super().get(*args, **kwargs)

    def merge(self, *args, **kwargs):
        self.check_thread()
        return super().merge(*args, **kwargs)

    def connection(self, *args, **kwargs):
        self.check_thread()
        return super().connection(*args, **kwargs)

    def begin(self, nested=False):
        self.check_thread()
        return super().begin(nested=nested)

    def commit(self):
        self.check_thread()
        return super().commit()

    def rollback(self):
        self.check_thread()
        return super().rollback()

    def flush(self, objects=None):
        self.check_thread()
        return super().flush(objects)

    def close(self):
        self.check_thread()
        return super().close()

@event.listens_for(ThreadBoundSession, "do_orm_execute")
def check_execution(state):
    state.session.check_thread()


def create_session_factory(engine: Engine) -> sessionmaker[ThreadBoundSession]:
    # close_resets_only=False makes a completed operation's Session unusable.
    return sessionmaker(bind=engine, class_=ThreadBoundSession, expire_on_commit=False,
                        close_resets_only=False)
