"""Named service operation scopes; reservation policy is centralized here."""
from contextlib import contextmanager
from app.services.unit_of_work import transaction

@contextmanager
def write_operation(factory):
    with transaction(factory, write=True) as session:
        yield session
