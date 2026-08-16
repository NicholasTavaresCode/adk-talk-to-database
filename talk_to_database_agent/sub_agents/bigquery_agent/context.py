"""Schema retrieval for the BigQuery agent.

Wired as the agent's `before_model_callback`. Embeds the user's question,
finds the nearest table cards in the Firestore index built by
`scripts/index_schema.py`, and appends them to the request so the model sees
only the handful of tables it plausibly needs instead of the whole warehouse.

Two placement rules matter here:

* The block is appended to `llm_request.contents`, never to
  `config.system_instruction`. ADK puts `static_instruction` at the *front* of
  `contents` and the context cache covers that prefix, so appending at the end
  leaves the cache intact.
* Retrieval runs once per invocation, not once per model call. The BigQuery
  agent loops (write SQL → run it → interpret results) and this callback fires
  on every leg of that loop; re-searching each time would burn an embedding
  call per leg and could swap the schema out from under a retry.
"""

import logging

from google.adk.agents import Context
from google.adk.models import LlmRequest, LlmResponse
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from google.genai import types

from talk_to_database_agent.app_utils.config import settings
from talk_to_database_agent.app_utils.embeddings import (
    embed_query_async,
    get_genai_client,
)

logger = logging.getLogger(__name__)

# `temp:` state is invocation-scoped and never written to Firestore (see
# FirestoreSessionService._apply_persisted_state_delta).
_STATE_KEY = "temp:schema_context"

_DISTANCE_FIELD = "vector_distance"

_HEADER = """# RELEVANT DATABASE SCHEMA

These BigQuery tables were retrieved as the most relevant to the user's
question. Write SQL against these tables only, using the fully-qualified names
exactly as shown. Do not invent tables or columns. If none of them can answer
the question, say so instead of guessing.
"""

_genai_client = None
_firestore_client: AsyncClient | None = None


def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = get_genai_client()
    return _genai_client


def _get_firestore_client() -> AsyncClient:
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = AsyncClient(database=settings.firestore_database)
    return _firestore_client


def _user_query(callback_context: Context) -> str:
    """The text of the message that opened this invocation."""
    content = callback_context.user_content
    if not content or not content.parts:
        return ""
    return "\n".join(part.text for part in content.parts if part.text).strip()


async def retrieve_schema_context(query: str) -> str:
    """Return the rendered schema block for a question, or "" if nothing matched."""
    vector = await embed_query_async(_get_genai_client(), query)

    collection = _get_firestore_client().collection(settings.schema_index_collection)
    vector_query = collection.find_nearest(
        vector_field="embedding",
        query_vector=Vector(vector),
        limit=settings.schema_index_top_k,
        distance_measure=DistanceMeasure.COSINE,
        distance_result_field=_DISTANCE_FIELD,
        distance_threshold=settings.schema_index_max_distance,
    )

    cards: list[str] = []
    matches: list[str] = []
    for doc in await vector_query.get():
        data = doc.to_dict() or {}
        card = data.get("card")
        if not card:
            continue
        cards.append(card)
        matches.append(f"{data.get('table_id', doc.id)}@{data.get(_DISTANCE_FIELD):.4f}")

    if not cards:
        logger.warning("No schema cards matched query: %r", query[:120])
        return ""

    logger.info("Retrieved %d schema card(s): %s", len(cards), ", ".join(matches))
    return _HEADER + "\n" + "\n\n---\n\n".join(cards)


async def retriever(
    callback_context: Context, llm_request: LlmRequest
) -> LlmResponse | None:
    """Append retrieved table schemas to the request.

    Always returns None: this callback augments the request and never
    short-circuits the model call. Retrieval failures are logged and swallowed
    so a missing index degrades answer quality instead of breaking the agent.
    """
    block = callback_context.state.get(_STATE_KEY)

    if block is None:
        query = _user_query(callback_context)
        if not query:
            return None
        try:
            block = await retrieve_schema_context(query)
        except Exception:
            logger.exception(
                "Schema retrieval failed; continuing without schema context. "
                "If this is FAILED_PRECONDITION, the Firestore vector index is "
                "missing — see scripts/index_schema.py."
            )
            return None
        # Cached even when empty, so one bad lookup does not retry every leg.
        callback_context.state[_STATE_KEY] = block

    if block:
        llm_request.contents.append(
            types.Content(role="user", parts=[types.Part(text=block)])
        )

    return None
