"""SQL dialect adaptation: Domain always sees aware UTC datetimes."""
from datetime import datetime, timezone
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("持久化时间必须带时区")
        utc = value.astimezone(timezone.utc)
        return utc.replace(tzinfo=None) if dialect.name == "sqlite" else utc

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
