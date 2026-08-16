#!/usr/bin/env python
"""Build the BigQuery schema vector index in Firestore.

Walks the configured BigQuery datasets, renders one "schema card" per table
(description, columns with their descriptions, partitioning, and a handful of
sample rows), embeds each card with gemini-embedding-2, and writes the vector
plus the card text to Firestore. The runtime retriever in the BigQuery agent's
`before_model_callback` then does a nearest-neighbour lookup over this
collection and injects only the matching cards into the prompt.

Re-running is cheap: each card is content-hashed, and unchanged tables are
skipped without an embedding call. Use --force to re-embed regardless.

Usage:
    uv run python scripts/index_schema.py --dry-run          # inspect cards only
    uv run python scripts/index_schema.py --datasets sales
    uv run python scripts/index_schema.py --prune
"""

import argparse
import hashlib
import logging
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# Running this as a file (rather than -m) puts scripts/ on sys.path, not the
# repo root, so the talk_to_database_agent package would not resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from google.cloud import bigquery, firestore
from google.cloud.firestore_v1.vector import Vector

from talk_to_database_agent.app_utils.config import settings
from talk_to_database_agent.app_utils.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    embed_documents,
    get_genai_client,
)
from talk_to_database_agent.sub_agents.bigquery_agent.tools import sanitize_value

load_dotenv()

logger = logging.getLogger("index_schema")

# Firestore caps a batched write at 500 operations.
_FIRESTORE_BATCH_SIZE = 400

# Sample cells are for teaching the model value *shapes* (date formats, country
# codes, enum spellings), not for carrying full payloads.
_MAX_CELL_CHARS = 60
_SAMPLEABLE_TABLE_TYPES = {"TABLE", "MATERIALIZED_VIEW"}

# Bare numbers ("02", "0004", "3.14") and clock times ("07:00") stored as
# STRING: identifiers and measurements, never labels worth retrieving on.
_IDENTIFIER_VALUE_RE = re.compile(r"[\d.\-+:/ ]+")

# Row-level geography describes whichever rows tabledata.list happened to
# return, not the table. Leaving it in makes "ozone in California" match a card
# whose sample says Alaska, so these columns never become representative
# values. They stay in the injected card, where the model can still use them.
_INCIDENTAL_COLUMN_RE = re.compile(
    r"address|city|county|state|site|cbsa|zip|postal|street|location|latitude|longitude",
    re.IGNORECASE,
)


@dataclass
class SchemaCard:
    """One table's retrievable description.

    `card` is what gets injected into the prompt; `embed_text` is what gets
    embedded. They differ on purpose. Warehouses built from a template (every
    EPA air-quality table repeats the same 20 boilerplate columns with
    identical descriptions) produce cards that are ~97% identical, so embedding
    the full card buries the few tokens that actually distinguish one table
    from another. `embed_text` keeps only the discriminative parts.
    """

    table_id: str
    project: str
    dataset: str
    table: str
    table_type: str
    description: str
    num_rows: int | None
    column_names: list[str]
    card: str
    embed_text: str

    @property
    def content_hash(self) -> str:
        payload = f"{self.card}\x00{self.embed_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def doc_id(self) -> str:
        # Firestore document ids may not contain "/".
        return self.table_id.replace("/", "_")


# ── Card rendering ───────────────────────────────────────────────────────────


def _iter_fields(
    fields: list[bigquery.SchemaField], prefix: str = ""
) -> Iterator[tuple[str, bigquery.SchemaField]]:
    """Yield (dotted_path, field) for every column, descending into STRUCTs."""
    for field in fields:
        path = f"{prefix}{field.name}"
        yield path, field
        if field.field_type in ("RECORD", "STRUCT") and field.fields:
            yield from _iter_fields(list(field.fields), prefix=f"{path}.")


def _format_cell(value: Any) -> str:
    """Render one sample value compactly on a single line."""
    if value is None:
        # "None" would read as a Python literal to a model writing SQL.
        return "NULL"
    text = str(sanitize_value(value))
    text = text.replace("\n", " ").replace("|", "\\|").strip()
    if len(text) > _MAX_CELL_CHARS:
        text = text[:_MAX_CELL_CHARS] + "…"
    return text


def _partition_summary(table: bigquery.Table) -> str | None:
    if table.time_partitioning:
        field = table.time_partitioning.field or "_PARTITIONTIME"
        return f"{table.time_partitioning.type_} on `{field}`"
    if table.range_partitioning:
        return f"RANGE on `{table.range_partitioning.field}`"
    return None


def _fetch_sample_rows(
    client: bigquery.Client, table: bigquery.Table, limit: int
) -> tuple[list[str], list[list[str]]]:
    """Read a few rows via the free tabledata.list API (no query billed)."""
    if limit <= 0 or table.table_type not in _SAMPLEABLE_TABLE_TYPES:
        return [], []

    # Only top-level scalar columns: nested/repeated values are unreadable once
    # flattened into a text row, and they dominate the card's token budget.
    selected = [
        field
        for field in table.schema
        if field.field_type not in ("RECORD", "STRUCT") and field.mode != "REPEATED"
    ]
    if not selected:
        return [], []

    try:
        rows = list(
            client.list_rows(table, selected_fields=selected, max_results=limit)
        )
    except Exception as exc:  # noqa: BLE001 - sampling is best-effort
        logger.warning("Could not sample %s: %s", table.full_table_id, exc)
        return [], []

    headers = [field.name for field in selected]
    return headers, [[_format_cell(value) for value in row.values()] for row in rows]


def _distinctive_values(
    headers: list[str],
    rows: list[list[str]],
    schema: list[bigquery.SchemaField],
    max_columns: int = 8,
) -> list[str]:
    """Pick out sampled values that identify what a table actually holds.

    For template-shaped warehouses the column *names* are shared but the values
    are not: `parameter_name = "Ozone"` is what separates o3_hourly_summary
    from co_hourly_summary. Long free-text and high-cardinality columns are
    skipped — they add noise, not identity.
    """
    types_by_name = {field.name: field.field_type for field in schema}
    lines: list[str] = []

    for position, header in enumerate(headers):
        if types_by_name.get(header) != "STRING":
            continue
        if _INCIDENTAL_COLUMN_RE.search(header):
            continue

        values = {row[position] for row in rows if row[position] != "NULL"}
        # All-distinct across a 5-row sample means it is an id or a timestamp,
        # not a label. Long values are free text.
        if not values or len(values) == len(rows) or any(len(v) > 40 for v in values):
            continue
        # Zero-padded FIPS codes, site numbers and clock times are stored as
        # STRING but carry no meaning a question would ever match on.
        if all(_IDENTIFIER_VALUE_RE.fullmatch(value) for value in values):
            continue

        lines.append(f"- {header}: {', '.join(sorted(values))}")
        if len(lines) >= max_columns:
            break

    return lines


def build_embed_text(
    table: bigquery.Table,
    table_id: str,
    column_names: list[str],
    headers: list[str],
    rows: list[list[str]],
) -> str:
    """Render the compact, discriminative text that actually gets embedded."""
    # Underscores hide the terms a question would use ("o3_hourly_summary" ->
    # "o3 hourly summary"), so split them out for the tokenizer.
    terms = table.table_id.replace("_", " ")
    lines = [
        f"BigQuery table: {table_id}",
        f"Table name: {terms}",
        f"Dataset: {table.dataset_id.replace('_', ' ')}",
    ]

    if table.description:
        lines.append(f"Description: {table.description.strip()}")
    if table.num_rows is not None:
        lines.append(f"Approximate rows: {table.num_rows:,}")

    # Names only, never the descriptions: those are the identical boilerplate.
    lines.append(f"Columns: {', '.join(column_names)}")

    distinctive = _distinctive_values(headers, rows, list(table.schema))
    if distinctive:
        lines.append("Representative values:")
        lines.extend(distinctive)

    return "\n".join(lines)


def build_card(
    client: bigquery.Client, table: bigquery.Table, sample_rows: int
) -> SchemaCard:
    """Render the text that gets embedded and later injected into the prompt."""
    table_id = f"{table.project}.{table.dataset_id}.{table.table_id}"
    lines: list[str] = [f"# Table: `{table_id}`"]

    if table.table_type and table.table_type != "TABLE":
        lines.append(f"Type: {table.table_type}")
    if table.description:
        lines.append(f"Description: {table.description.strip()}")
    if table.num_rows is not None:
        lines.append(f"Approximate rows: {table.num_rows:,}")

    partition = _partition_summary(table)
    if partition:
        lines.append(f"Partitioned by: {partition}")
    if table.clustering_fields:
        lines.append(f"Clustered by: {', '.join(table.clustering_fields)}")

    lines.append("")
    lines.append("## Columns")

    column_names: list[str] = []
    for path, field in _iter_fields(list(table.schema)):
        column_names.append(path)
        modifiers = field.field_type
        if field.mode and field.mode != "NULLABLE":
            modifiers += f", {field.mode}"
        entry = f"- `{path}` ({modifiers})"
        if field.description:
            entry += f" — {field.description.strip()}"
        lines.append(entry)

    headers, rows = _fetch_sample_rows(client, table, sample_rows)
    if rows:
        lines.append("")
        lines.append(f"## Sample rows ({len(rows)})")
        lines.append(" | ".join(headers))
        lines.append(" | ".join("---" for _ in headers))
        lines.extend(" | ".join(row) for row in rows)

    return SchemaCard(
        table_id=table_id,
        project=table.project,
        dataset=table.dataset_id,
        table=table.table_id,
        table_type=table.table_type or "TABLE",
        description=(table.description or "").strip(),
        num_rows=table.num_rows,
        column_names=column_names,
        card="\n".join(lines),
        embed_text=build_embed_text(table, table_id, column_names, headers, rows),
    )


# ── BigQuery walk ────────────────────────────────────────────────────────────


def resolve_datasets(
    client: bigquery.Client, datasets: list[str]
) -> list[bigquery.DatasetReference]:
    """Turn dataset arguments into references, defaulting to the billing project.

    Accepts both bare ids ("sales") and cross-project ids
    ("bigquery-public-data.epa_historical_air_quality"), so a public dataset can
    be indexed while queries are still billed to GOOGLE_CLOUD_PROJECT.
    """
    if not datasets:
        refs = [ds.reference for ds in client.list_datasets()]
        logger.info(
            "Discovered %d dataset(s) in %s: %s",
            len(refs),
            client.project,
            ", ".join(ref.dataset_id for ref in refs),
        )
        return refs

    return [
        bigquery.DatasetReference.from_string(name, default_project=client.project)
        for name in datasets
    ]


def collect_cards(
    client: bigquery.Client,
    dataset_refs: list[bigquery.DatasetReference],
    sample_rows: int,
) -> list[SchemaCard]:
    cards: list[SchemaCard] = []
    for ref in dataset_refs:
        for item in client.list_tables(ref):
            table = client.get_table(item.reference)
            cards.append(build_card(client, table, sample_rows))
            logger.info("Rendered card for %s", cards[-1].table_id)

    return cards


# ── Firestore write ──────────────────────────────────────────────────────────


def _existing_hashes(
    db: firestore.Client, collection: str
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for doc in db.collection(collection).stream():
        data = doc.to_dict() or {}
        # A vector written at a different size or by a different model must be
        # rebuilt, so treat it as a miss rather than comparing hashes.
        if (
            data.get("embedding_model") == EMBEDDING_MODEL
            and data.get("embedding_dimensions") == EMBEDDING_DIMENSIONS
        ):
            hashes[doc.id] = data.get("content_hash", "")
    return hashes


def write_cards(
    db: firestore.Client,
    collection: str,
    cards: list[SchemaCard],
    vectors: list[list[float]],
) -> None:
    batch = db.batch()
    pending = 0

    for card, vector in zip(cards, vectors):
        batch.set(
            db.collection(collection).document(card.doc_id),
            {
                "table_id": card.table_id,
                "project": card.project,
                "dataset": card.dataset,
                "table": card.table,
                "table_type": card.table_type,
                "description": card.description,
                "num_rows": card.num_rows,
                "column_names": card.column_names,
                "card": card.card,
                # Stored for debugging retrieval: this, not `card`, is the text
                # the vector was built from.
                "embed_text": card.embed_text,
                "content_hash": card.content_hash,
                "embedding": Vector(vector),
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
                "indexed_at": firestore.SERVER_TIMESTAMP,
            },
        )
        pending += 1
        if pending >= _FIRESTORE_BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            pending = 0

    if pending:
        batch.commit()


def prune_missing(
    db: firestore.Client, collection: str, live_doc_ids: set[str]
) -> int:
    """Delete indexed tables that no longer exist in BigQuery."""
    deleted = 0
    for doc in db.collection(collection).stream():
        if doc.id not in live_doc_ids:
            doc.reference.delete()
            logger.info("Pruned stale entry %s", doc.id)
            deleted += 1
    return deleted


# ── Entry point ──────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasets",
        default=",".join(settings.bq_datasets_list),
        help="Comma-separated dataset ids. Defaults to BQ_DATASETS, or all datasets.",
    )
    parser.add_argument(
        "--collection",
        default=settings.schema_index_collection,
        help="Firestore collection to write to.",
    )
    parser.add_argument(
        "--sample-rows", type=int, default=5, help="Sample rows per table (0 disables)."
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-embed even if the card is unchanged."
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete indexed tables that no longer exist in BigQuery.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered cards; do not embed or write.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        logger.error("GOOGLE_CLOUD_PROJECT is not set.")
        return 1

    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    bq = bigquery.Client(project=project, location=settings.bq_location)

    dataset_refs = resolve_datasets(bq, datasets)
    for ref in dataset_refs:
        if ref.project != project:
            logger.info(
                "Indexing %s from project %s; queries stay billed to %s.",
                ref.dataset_id,
                ref.project,
                project,
            )

    cards = collect_cards(bq, dataset_refs, args.sample_rows)
    if not cards:
        logger.warning("No tables found. Nothing to index.")
        return 0

    if args.dry_run:
        for card in cards:
            print(f"\n{'=' * 78}\n{card.card}")
            print(f"\n{'-' * 30} EMBEDDED TEXT {'-' * 30}\n{card.embed_text}")
        print(f"\n{len(cards)} card(s) rendered. Nothing embedded or written.")
        return 0

    db = firestore.Client(project=project, database=settings.firestore_database)

    known = {} if args.force else _existing_hashes(db, args.collection)
    stale = [card for card in cards if known.get(card.doc_id) != card.content_hash]

    skipped = len(cards) - len(stale)
    if skipped:
        logger.info("Skipping %d unchanged table(s).", skipped)

    if stale:
        logger.info("Embedding %d card(s) with %s…", len(stale), EMBEDDING_MODEL)
        vectors = embed_documents(
            get_genai_client(), [card.embed_text for card in stale]
        )
        write_cards(db, args.collection, stale, vectors)
        logger.info("Wrote %d card(s) to %s.", len(stale), args.collection)

    if args.prune:
        prune_missing(db, args.collection, {card.doc_id for card in cards})

    print(
        "\nIndex ready. Create the vector index once (it is not created "
        "automatically):\n\n"
        f"  gcloud firestore indexes composite create \\\n"
        f"    --collection-group={args.collection} \\\n"
        f"    --query-scope=COLLECTION \\\n"
        f"    --field-config=field-path=embedding,"
        f"vector-config='{{\"dimension\":\"{EMBEDDING_DIMENSIONS}\",\"flat\":\"{{}}\"}}' \\\n"
        f"    --database={settings.firestore_database}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
