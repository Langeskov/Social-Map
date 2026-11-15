"""Public application failure type; Qt need not import persistence adapters."""
from app.storage.database import DatabaseError

__all__ = ["DatabaseError"]
