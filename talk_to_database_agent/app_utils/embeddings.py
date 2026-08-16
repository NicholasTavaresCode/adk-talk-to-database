"""Embedding helpers shared by the schema indexer and the runtime retriever.

Both sides of a vector search must agree on model, dimensionality, L2
normalization and instruction prefix. A mismatch does not raise — it silently
degrades recall — so the indexing script and the `before_model_callback`
retriever both import these constants instead of re-declaring them.
"""

import logging
import math
import os
from collections.abc import Iterable, Sequence

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-2"

# gemini-embedding-2 emits 3072 dimensions by default, but a Firestore vector
# index accepts at most 2048. 1536 is the largest of Google's recommended MRL
# sizes (768 / 1536 / 3072) that fits under that ceiling.
EMBEDDING_DIMENSIONS = 1536

# The model accepts 8192 input tokens. Cards are truncated on a rough 4
# chars/token estimate, with headroom for the instruction prefix.
MAX_INPUT_CHARS = 8192 * 4 - 2000

# gemini-embedding-2 dropped the `task_type` parameter that gemini-embedding-001
# had. Asymmetric retrieval is steered by prefixing the text with an
# instruction instead, so documents and queries land in comparable regions of
# the space. Changing either string invalidates the whole index — re-run the
# indexer if you touch them.
DOC_INSTRUCTION = (
    "Task: represent this BigQuery table's schema and sample data so it can be "
    "retrieved when a user asks a question that requires writing SQL against "
    "this table.\n\n"
)
QUERY_INSTRUCTION = (
    "Task: given a user's natural-language question about their data, retrieve "
    "the BigQuery table schemas needed to write the SQL that answers it.\n\n"
)

_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=5,
    initial_delay=5.0,
    max_delay=60.0,
    exp_base=1.5,
)


def get_genai_client() -> genai.Client:
    """Build a Vertex AI genai client for embedding calls."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT environment variable is not set.")

    return genai.Client(
        vertexai=True,
        project=project,
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        http_options=types.HttpOptions(retry_options=_RETRY_OPTIONS),
    )


def l2_normalize(values: Sequence[float]) -> list[float]:
    """Scale a vector to unit length.

    Google's docs disagree on whether gemini-embedding-2 pre-normalizes MRL
    truncated output, so we always normalize: it is idempotent on an
    already-normalized vector and required for COSINE distance to behave if it
    is not.
    """
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return list(values)
    return [value / norm for value in values]


def _truncate(text: str) -> str:
    if len(text) <= MAX_INPUT_CHARS:
        return text
    logger.warning(
        "Embedding input truncated from %d to %d chars", len(text), MAX_INPUT_CHARS
    )
    return text[:MAX_INPUT_CHARS]


def _request(text: str, instruction: str) -> dict:
    return {
        "model": EMBEDDING_MODEL,
        "contents": _truncate(instruction + text),
        "config": types.EmbedContentConfig(
            output_dimensionality=EMBEDDING_DIMENSIONS
        ),
    }


def _extract(response: types.EmbedContentResponse) -> list[float]:
    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError(f"{EMBEDDING_MODEL} returned no embedding values.")

    values = response.embeddings[0].values
    if len(values) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"Expected {EMBEDDING_DIMENSIONS} dimensions, got {len(values)}."
        )

    return l2_normalize(values)


def embed_text(client: genai.Client, text: str, instruction: str) -> list[float]:
    """Embed one string, returning a normalized vector of EMBEDDING_DIMENSIONS."""
    return _extract(client.models.embed_content(**_request(text, instruction)))


async def embed_text_async(
    client: genai.Client, text: str, instruction: str
) -> list[float]:
    """Async counterpart of embed_text, for use inside ADK callbacks."""
    return _extract(await client.aio.models.embed_content(**_request(text, instruction)))


def embed_document(client: genai.Client, text: str) -> list[float]:
    """Embed a schema card for storage."""
    return embed_text(client, text, DOC_INSTRUCTION)


def embed_query(client: genai.Client, text: str) -> list[float]:
    """Embed a user question for retrieval."""
    return embed_text(client, text, QUERY_INSTRUCTION)


async def embed_query_async(client: genai.Client, text: str) -> list[float]:
    """Embed a user question for retrieval, without blocking the event loop."""
    return await embed_text_async(client, text, QUERY_INSTRUCTION)


def embed_documents(
    client: genai.Client, texts: Iterable[str], max_workers: int = 8
) -> list[list[float]]:
    """Embed many cards concurrently, preserving input order.

    Sent one request per card rather than as a batch: per-request instance
    limits differ between Vertex and the Gemini API, and a single oversized
    card would fail the whole batch.
    """
    from concurrent.futures import ThreadPoolExecutor

    texts = list(texts)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda text: embed_document(client, text), texts))
