"""Schema retrieval for the BigQuery agent.

Wired as the agent's `before_model_callback`. Calls the
`search_table_cards(question, top_k)` table function in BigQuery, which embeds
the question and runs the vector search server-side, and appends the returned
table cards to the request so the model sees only the handful of tables it
plausibly needs instead of the whole warehouse.

Two placement rules matter here:

* The block is appended to `llm_request.contents`, never to
  `config.system_instruction`. ADK puts `static_instruction` at the *front* of
  `contents` and the context cache covers that prefix, so appending at the end
  leaves the cache intact.
* Retrieval runs once per invocation, not once per model call. The BigQuery
  agent loops (write SQL → run it → interpret results) and this callback fires
  on every leg of that loop; re-searching each time would burn a BigQuery job
  per leg and could swap the schema out from under a retry.
"""

import asyncio
import logging
import time

from google.adk.agents import Context
from google.adk.models import LlmRequest, LlmResponse
from google.cloud import bigquery
from google.genai import types

from talk_to_database_agent.app_utils.config import settings
from talk_to_database_agent.sub_agents.bigquery_agent.bq import get_bq_client
from talk_to_database_agent.sub_agents.bigquery_agent.tools import format_bytes

logger = logging.getLogger(__name__)

_STATE_KEY = "temp:schema_context"

_HEADER = """# RELEVANT DATABASE SCHEMA

These BigQuery tables were retrieved as the most relevant to the user's
question. Write SQL against these tables only, using the fully-qualified names
exactly as shown. Do not invent tables or columns. If none of them can answer
the question, say so instead of guessing.
"""

def _user_query(callback_context: Context) -> str:
    """The text of the message that opened this invocation."""
    content = callback_context.user_content
    if not content or not content.parts:
        return ""
    return "\n".join(part.text for part in content.parts if part.text).strip()

def _search_blocking(query: str) -> tuple[list[bigquery.Row], bigquery.QueryJob]:
    """Run the table function. Blocking — call it through asyncio.to_thread.

    Devolve o job junto com as linhas: é dele que saem o id, os bytes cobrados
    e o cache hit que a chamada registra no log.
    """
    sql = f"""
    SELECT table_id, distance, card
    FROM `{settings.schema_search_routine}`(@question, @top_k)
    ORDER BY distance
    """
    job = get_bq_client(settings.schema_search_location).query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("question", "STRING", query),
                bigquery.ScalarQueryParameter(
                    "top_k", "INT64", settings.schema_index_top_k
                ),
            ],
            job_timeout_ms=int(settings.bq_query_timeout_seconds * 1000),
        ),
    )
    return list(job.result()), job

async def retrieve_schema_context(query: str) -> str:
    """Return the rendered schema block for a question, or "" if nothing matched."""
    logger.info(
        "Schema search starting: top_k=%d routine=%s location=%s",
        settings.schema_index_top_k,
        settings.schema_search_routine,
        settings.schema_search_location,
    )
    started = time.perf_counter()
    rows, job = await asyncio.to_thread(_search_blocking, query)
    elapsed = time.perf_counter() - started

    logger.info(
        "Schema search done in %.2fs: %d row(s), %s billed, cache_hit=%s, job=%s",
        elapsed,
        len(rows),
        format_bytes(job.total_bytes_billed or 0),
        job.cache_hit,
        job.job_id,
    )

    max_distance = settings.schema_index_max_distance

    cards: list[str] = []
    matches: list[str] = []
    dropped: list[str] = []
    for row in rows:
        table_id = row.get("table_id")
        distance = row.get("distance")
        card = row.get("card")

        if not card:
            logger.debug("Schema card %s came back empty; skipping.", table_id)
            continue

        if max_distance is not None and distance is not None and distance > max_distance:
            dropped.append(f"{table_id}@{distance}")
            continue

        cards.append(card)
        matches.append(f"{table_id}@{distance}")

    if dropped:
        logger.info(
            "Dropped %d card(s) above max_distance=%s: %s",
            len(dropped),
            max_distance,
            ", ".join(dropped),
        )

    if not cards:
        logger.warning(
            "No schema cards for query %r (routine=%s, %d row(s) returned, "
            "%d dropped by max_distance).",
            query[:120],
            settings.schema_search_routine,
            len(rows),
            len(dropped),
        )
        return ""

    block = _HEADER + "\n" + "\n\n---\n\n".join(cards)
    logger.info(
        "Injecting %d schema card(s), %d chars: %s",
        len(cards),
        len(block),
        ", ".join(matches),
    )
    logger.debug("Schema block:\n%s", block)
    return block

async def retriever(
    callback_context: Context, llm_request: LlmRequest
) -> LlmResponse | None:
    """Append retrieved table schemas to the request.

    Always returns None: this callback augments the request and never
    short-circuits the model call. Retrieval failures are logged and swallowed
    so a missing index degrades answer quality instead of breaking the agent.
    """
    block = callback_context.state.get(_STATE_KEY)

    query = _user_query(callback_context)

    logger.info("Retriever callback for user query: %r", query)

    if block is not None:
        logger.info(
            "Schema context reused from invocation cache (%d chars).", len(block)
        )
    else:
        if not query:
            logger.warning(
                "Invocation has no user text; skipping schema retrieval. "
                "The agent will write SQL without any schema."
            )
            return None
        try:
            block = await retrieve_schema_context(query)
        except Exception:
            logger.exception(
                "Schema retrieval failed; continuing without schema context. "
                "NotFound usually means the wrong location (the routine is in "
                "%s); Forbidden means the service account cannot query %s.",
                settings.schema_search_location,
                settings.schema_search_routine,
            )
            return None
        callback_context.state[_STATE_KEY] = block

    if block:
        logger.info(
            "Appending schema context to request contents (%d chars).", len(block)
        )
        llm_request.contents.append(
            types.Content(role="user", parts=[types.Part(text=block)])
        )
    else:
        logger.warning(
            "Proceeding with no schema context for agent %s.",
            callback_context.agent_name,
        )

    return None
