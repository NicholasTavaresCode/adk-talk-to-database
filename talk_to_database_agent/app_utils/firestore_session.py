import logging
import time
import uuid
from typing import Any, Optional

from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.adk.sessions.state import State
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1 import DELETE_FIELD
from google.cloud.firestore_v1 import async_collection, async_document
from google.cloud.firestore_v1.base_query import BaseQuery
from google.cloud.firestore_v1.transforms import Increment

logger = logging.getLogger(__name__)

_BATCH_DELETE_SIZE = 500

# Firestore rejects field names that start AND end with `__` (e.g. `__session_metadata__`).
# We escape such keys using a safe prefix/suffix so they can be stored and round-tripped.
_ESC_PREFIX = "_Z_"


def _encode_key(key: str) -> str:
    """Escape a Firestore-reserved dunder-wrapped field name."""
    if key.startswith("__") and key.endswith("__") and len(key) > 4:
        return _ESC_PREFIX + key[2:-2] + _ESC_PREFIX
    return key


def _decode_key(key: str) -> str:
    """Reverse _encode_key."""
    if key.startswith(_ESC_PREFIX) and key.endswith(_ESC_PREFIX) and len(key) > 6:
        return "__" + key[3:-3] + "__"
    return key


def _encode_state(state: dict[str, Any]) -> dict[str, Any]:
    return {_encode_key(k): v for k, v in state.items()}


def _decode_state(state: dict[str, Any]) -> dict[str, Any]:
    return {_decode_key(k): v for k, v in state.items()}


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if value is not None}


def _apply_persisted_state_delta(
    session_state: dict[str, Any],
    state_delta: Optional[dict[str, Any]],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if not state_delta:
        return updates

    for key, value in state_delta.items():
        if key.startswith(State.TEMP_PREFIX):
            continue

        encoded_key = _encode_key(key)
        if value is None:
            session_state.pop(key, None)
            updates[f"state.{encoded_key}"] = DELETE_FIELD
            continue

        session_state[key] = value
        updates[f"state.{encoded_key}"] = value

    return updates


class FirestoreSessionService(BaseSessionService):
    """ADK session service backed by Google Cloud Firestore (async)."""

    def __init__(
        self,
        project: Optional[str] = None,
        database: str = "(default)",
        collection: str = "adk_sessions",
    ):
        self._db = AsyncClient(project=project, database=database)
        self._collection = collection

    # ── Path helpers ─────────────────────────────────────────────────────

    def _session_ref(
        self, app_name: str, user_id: str, session_id: str
    ) -> async_document.AsyncDocumentReference:
        return (
            self._db.collection(self._collection)
            .document(app_name)
            .collection("users")
            .document(user_id)
            .collection("sessions")
            .document(session_id)
        )

    def _sessions_coll(
        self, app_name: str, user_id: str
    ) -> async_collection.AsyncCollectionReference:
        return (
            self._db.collection(self._collection)
            .document(app_name)
            .collection("users")
            .document(user_id)
            .collection("sessions")
        )

    def _events_coll(
        self, app_name: str, user_id: str, session_id: str
    ) -> async_collection.AsyncCollectionReference:
        return self._session_ref(app_name, user_id, session_id).collection("events")

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _purge_events(
        self, app_name: str, user_id: str, session_id: str
    ) -> int:
        events_ref = self._events_coll(app_name, user_id, session_id)
        total = 0
        while True:
            docs = events_ref.limit(_BATCH_DELETE_SIZE)
            deleted = 0
            async for doc_snapshot in docs.stream():
                await doc_snapshot.reference.delete()
                deleted += 1
            total += deleted
            if deleted < _BATCH_DELETE_SIZE:
                break
        if total:
            logger.info(
                "Purged %d events from %s/%s/%s",
                total, app_name, user_id, session_id,
            )
        return total

    # ── CRUD ─────────────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        if not session_id:
            session_id = uuid.uuid4().hex

        now = time.time()
        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=state or {},
            events=[],
            last_update_time=now,
        )

        await self._session_ref(app_name, user_id, session_id).set({
            "id": session_id,
            "app_name": app_name,
            "user_id": user_id,
            "state": _encode_state(state or {}),
            "last_update_time": now,
        })

        logger.info("Created Firestore session %s/%s/%s", app_name, user_id, session_id)
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        doc = await self._session_ref(app_name, user_id, session_id).get()
        if not doc.exists:
            return None

        data = doc.to_dict() or {}
        stored_state = _normalize_state(_decode_state(data.get("state", {})))
        last_update = data.get("last_update_time", 0.0)
        if hasattr(last_update, "timestamp"):
            last_update = last_update.timestamp()

        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=stored_state,
            events=[],
            last_update_time=float(last_update),
        )

        # Load events
        events_ref = self._events_coll(app_name, user_id, session_id)

        if config and config.num_recent_events:
            query = events_ref.order_by(
                "timestamp", direction=BaseQuery.DESCENDING
            ).limit(config.num_recent_events)

            if config.after_timestamp:
                query = (
                    events_ref.where("timestamp", ">", config.after_timestamp)
                    .order_by("timestamp", direction=BaseQuery.DESCENDING)
                    .limit(config.num_recent_events)
                )

            events: list[Event] = []
            async for edoc in query.stream():
                edata = edoc.to_dict() or {}
                events.append(Event.model_validate_json(edata["event_json"]))
            session.events = list(reversed(events))
        else:
            query = events_ref.order_by("timestamp")
            if config and config.after_timestamp:
                query = events_ref.where(
                    "timestamp", ">", config.after_timestamp
                ).order_by("timestamp")

            async for edoc in query.stream():
                edata = edoc.to_dict() or {}
                session.events.append(Event.model_validate_json(edata["event_json"]))

        return session

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        sessions: list[Session] = []

        if user_id:
            query = self._sessions_coll(app_name, user_id).order_by(
                "last_update_time", direction=BaseQuery.DESCENDING
            )
            async for doc in query.stream():
                data = doc.to_dict() or {}
                lut = data.get("last_update_time", 0.0)
                if hasattr(lut, "timestamp"):
                    lut = lut.timestamp()
                sessions.append(Session(
                    id=doc.id, app_name=app_name, user_id=user_id,
                    state={}, events=[], last_update_time=float(lut),
                ))
        else:
            query = (
                self._db.collection_group("sessions")
                .where("app_name", "==", app_name)
                .order_by("last_update_time", direction=BaseQuery.DESCENDING)
            )
            async for doc in query.stream():
                data = doc.to_dict() or {}
                lut = data.get("last_update_time", 0.0)
                if hasattr(lut, "timestamp"):
                    lut = lut.timestamp()
                sessions.append(Session(
                    id=doc.id, app_name=app_name,
                    user_id=data.get("user_id", ""),
                    state={}, events=[], last_update_time=float(lut),
                ))

        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        await self._purge_events(app_name, user_id, session_id)
        await self._session_ref(app_name, user_id, session_id).delete()
        logger.info("Deleted Firestore session %s/%s/%s", app_name, user_id, session_id)

    # ── Event persistence ────────────────────────────────────────────────

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event

        self._apply_temp_state(session, event)
        event = self._trim_temp_delta_state(event)

        event_json = event.model_dump_json(exclude_none=True)
        event_ts = event.timestamp or time.time()
        await self._events_coll(
            session.app_name, session.user_id, session.id
        ).document(event.id).set({
            "event_json": event_json,
            "timestamp": event_ts,
        })

        session_updates: dict[str, Any] = {}

        if event.actions and event.actions.state_delta:
            session_updates.update(
                _apply_persisted_state_delta(session.state, event.actions.state_delta)
            )

        # Denormalize preview for backoffice listing
        is_compaction = bool(event.actions and event.actions.compaction)
        if not is_compaction and event.content and event.content.parts:
            text_parts = [
                p.text for p in event.content.parts if getattr(p, "text", None)
            ]
            if text_parts:
                preview_text = " ".join(text_parts)
                preview = preview_text[:80] + ("…" if len(preview_text) > 80 else "")
                author = event.author or ""
                session_updates["last_message_preview"] = preview
                session_updates["last_message_at"] = event_ts
                session_updates["last_message_author"] = (
                    "user" if author == "user" else "agent"
                )
                session_updates["message_count"] = Increment(1)

        now = time.time()
        session_updates["last_update_time"] = now
        await self._session_ref(
            session.app_name, session.user_id, session.id
        ).update(session_updates)
        session.last_update_time = now

        session.events.append(event)

        return event
