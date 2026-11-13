"""Row ↔ Domain mapping, owned by the storage layer.

Every SQL adapter needs the same three operations, and before Phase 10 four modules
carried their own copy (with two importers reaching into the *relationship* module
for a helper that was never relationship-specific). They live here now:

* `to_values`  - frozen dataclass → column mapping for INSERT;
* `to_domain`  - ORM row → domain entity (field names must match the entity);
* `apply_update` - copy the entity's fields onto a loaded row.

Nothing here knows about transactions, sessions or business rules.
"""
from dataclasses import fields


def to_values(entity) -> dict:
    """Copy scalar dataclass fields; never copy ORM state or relationships."""
    return {field.name: getattr(entity, field.name) for field in fields(entity)}


def to_domain(domain_type, model):
    """Build a domain entity from an ORM row (or `None`)."""
    if model is None:
        return None
    return domain_type(**{field.name: getattr(model, field.name) for field in fields(domain_type)})


def apply_update(model, entity) -> None:
    """Assign every scalar field of `entity` onto `model`."""
    for name, value in to_values(entity).items():
        setattr(model, name, value)
