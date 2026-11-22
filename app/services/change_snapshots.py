"""Whitelisted snapshot builders shared by the services that capture changes.

Each builder loads the aggregate's collections (aliases/images/participants/media)
inside the caller's transaction and returns the canonical whitelist snapshot for
the serializer. Services that already hold the collection can pass it in to avoid
a second query.
"""
from app.services.change_serialization import ChangeSnapshotSerializer

SERIALIZER = ChangeSnapshotSerializer()


def workspace_state(workspace) -> dict:
    return SERIALIZER.snapshot("WORKSPACE", workspace)


def person_state(repositories, workspace_id, person, *, aliases=None, images=None) -> dict:
    if aliases is None:
        aliases = repositories.aliases.list(workspace_id, person.id)
    if images is None:
        images = repositories.person_media.list(workspace_id, person.id)
    return SERIALIZER.snapshot("PERSON", person, aliases=aliases, images=images)


def event_state(repositories, workspace_id, event, *, participants=None) -> dict:
    if participants is None:
        participants = repositories.participants.list_by_event(workspace_id, event.id)
    return SERIALIZER.snapshot("EVENT", event, participants=participants)


def evidence_state(repositories, workspace_id, evidence, *, media=None) -> dict:
    if media is None:
        media = repositories.evidence_media.list_by_evidence(workspace_id, evidence.id)
    return SERIALIZER.snapshot("EVIDENCE", evidence, media=media)


def relationship_state(relationship) -> dict:
    return SERIALIZER.snapshot("RELATIONSHIP", relationship)


def history_state(history) -> dict:
    return SERIALIZER.snapshot("RELATIONSHIP_HISTORY", history)


def media_state(media) -> dict:
    return SERIALIZER.snapshot("MEDIA", media)


def memory_state(memory) -> dict:
    return SERIALIZER.snapshot("MEMORY", memory)
