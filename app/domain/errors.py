"""Public errors raised at the pure domain boundary."""


class DomainError(ValueError):
    """Invalid domain data or a violated domain rule."""


class NotFoundError(DomainError):
    """A requested domain object does not exist."""


class WorkspaceMismatchError(DomainError):
    """Objects belong to different workspaces."""


class ConflictError(DomainError):
    """An operation conflicts with existing domain data."""


class MediaValidationError(DomainError):
    """Media metadata or its storage path is invalid."""
