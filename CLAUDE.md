# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependencies are managed with `uv` (`[tool.uv] package = false` — the project is run from source, not installed).

```bash
uv sync                                  # install deps (add --no-dev for prod, as the Dockerfile does)
uv run python main.py                    # serve the ADK dev UI + API on APP_PORT (default 8000)
uv run adk web                           # alternative: ADK's own dev UI over the agents dir
uv run pytest                            # run the suite (asyncio_mode=auto, testpaths=tests)
uv run pytest tests/test_agents.py::TestAgents::test_bigquery_agent_can_handle_env_query
uv run ruff check .                      # lint

uv run python scripts/index_schema.py --dry-run    # inspect rendered schema cards
uv run python scripts/index_schema.py --prune      # (re)build the RAG index in Firestore
```

Tests hit real Vertex AI and BigQuery — they are integration tests, not unit tests, and require valid ADC plus `GOOGLE_CLOUD_PROJECT`. There is currently no offline test path.

Environment comes from `.env` (see `.env.example`), read both by `pydantic-settings` and directly via `os.environ`:

- `GOOGLE_CLOUD_PROJECT` — required by `models.py` (Vertex client) and `bq.py`; nothing works without it.
- `GOOGLE_GENAI_USE_VERTEXAI=True`, `FIRESTORE_DATABASE`, `BQ_LOCATION` (defaults `us-east1`), `USE_FIRESTORE_SESSIONS`, `APP_ENV`, `APP_PORT`.

## Architecture

An ADK (Google Agent Development Kit) natural-language-to-SQL agent served as a FastAPI app.

**Two-tier agent design.** [talk_to_database_agent/agent.py](talk_to_database_agent/agent.py) defines `root_agent` (persona "Michael") — a conversational front end that owns clarification, formatting, and time/math tools, and is instructed to delegate *all* SQL generation to a `bigquery_agent` tool. [sub_agents/bigquery_agent/agent.py](talk_to_database_agent/sub_agents/bigquery_agent/agent.py) is the SQL specialist: it returns a fixed JSON envelope (`explain`, `sql`, `sql_results`, `nl_results`) and executes through the single `run_sql_query` tool. The two agents share `GEMINI_MODEL` and `MATH_TOOLS`.

**The `App` wrapper is where cross-cutting behavior lives.** `agent.py` wraps `root_agent` in an ADK `App` carrying `ContextCacheConfig` (implicit prompt caching), `EventsCompactionConfig` (history compaction), and plugins (`ReflectAndRetryToolPlugin`, `LogPlugin`). The `App` is not optional decoration: `agent_loader` checks the module for `app` *before* `root_agent`, and if it finds only a bare agent it synthesizes `App.model_construct(..., plugins=[])` — `Runner` reads `plugin_manager` and `context_cache_config` solely from the App, so deleting the wrapper silently disables caching and both plugins. (`App.name` itself is cosmetic here: the web server passes the agent *directory* name as `app_name`, and `self.app_name = app_name or app.name` lets it win — so Firestore sessions live under `adk_sessions/talk_to_database_agent/...`.)

**History compaction is token-triggered, not turn-triggered**, because what fills the window is one query returning many rows, not long conversation: `run_sql_query` returns every row, `sql_results` is persisted in the agent's JSON envelope, and `AgentTool(skip_summarization=True)` copies it into the root agent's history too. At ~229 tokens/exchange a 1M window takes ~4,500 exchanges; at 200 rows/answer it takes ~116. Hence `token_threshold=100_000` with `event_retention_size=20` (the two are a mandatory pair — as are `compaction_interval`/`overlap_size` for the alternative sliding-window trigger; ADK requires at least one pair). Leaving `summarizer=None` makes ADK build an `LlmEventSummarizer` from the root agent's own model. Note the retrieved schema block is *not* part of this growth — it is appended per-request and never persisted as an event — but it is a flat 8–14k tokens on every call, and since it lands after the cached prefix it is re-billed each turn. Plugins in [talk_to_database_agent/plugins/](talk_to_database_agent/plugins/) are app-level and apply to every agent — `LogPlugin` is a verbose console tracer for every ADK callback point, `RateLimiterPlugin` throttles LLM calls (written but not currently registered).

**Static vs. dynamic instructions.** Agents use `static_instruction` for the stable prompt body (this is what the context cache keys on) and `instruction` for a callable that recomputes per-turn context — see `dynamic_instruction` in [sub_agents/bigquery_agent/prompts.py](talk_to_database_agent/sub_agents/bigquery_agent/prompts.py). Keep volatile data (timestamps, RAG snippets) out of `static_instruction` or caching breaks. `before_model_callback=rag` in the BigQuery agent is the intended hook for schema retrieval.

**Model config is centralized** in [app_utils/models.py](talk_to_database_agent/app_utils/models.py): one `Gemini` instance with an explicit `api_client` pinned to Vertex AI `location="global"` and `HttpRetryOptions` on both the model and the client. Both agents run `temperature=0.01` and `function_calling_config mode="VALIDATED"`.

**No agent does arithmetic itself.** Both prompts forbid it and route through `calculate` / `percentage_change` / `proportion` in [app_utils/math.py](talk_to_database_agent/app_utils/math.py). Likewise, the model must never invent the current date — `build_timezone_metadata()` ([app_utils/utils.py](talk_to_database_agent/app_utils/utils.py)) is the only source, fixed to `America/Sao_Paulo` with an April-start fiscal year.

**SQL safety is layered in `tools.py`, not in the prompt.** `run_sql_query` runs `sanitize_sql` (strip markdown fences, strip trailing `;`, reject multi-statement queries — using `_mask_literals_and_comments` so keywords inside string literals don't trip it), then `check_sql_read_only` (keyword denylist), then executes and passes rows through `sanitize_value`/`sanitize_rows` to make BigQuery types JSON-safe (Decimal, datetime, bytes, non-finite floats). New validation helpers belong here as pure functions so they stay testable.

**Schema RAG.** BigQuery datasets are too wide to put in the prompt, so [scripts/index_schema.py](scripts/index_schema.py) renders one "schema card" per table (description, columns + their descriptions, partitioning, 5 sample rows), embeds it, and stores the vector in Firestore. At runtime `retriever` in [sub_agents/bigquery_agent/context.py](talk_to_database_agent/sub_agents/bigquery_agent/context.py) — the BigQuery agent's `before_model_callback` — embeds the user's question, nearest-neighbours that collection, and appends the matching cards.

**What gets embedded is not what gets injected.** Each table produces both a `card` (full schema, injected into the prompt) and a shorter `embed_text` (what the vector is built from). This matters for template-shaped warehouses: every table in `bigquery-public-data.epa_historical_air_quality` repeats the same ~20 boilerplate columns with byte-identical descriptions and no table description at all, so full-card embeddings are ~97% shared text and `parameter_name = "Ozone"` gets averaged into noise. `embed_text` keeps the table name (underscores split into words), column *names* only, row count, and "representative values" sampled from low-cardinality STRING columns — dropping the long column descriptions that are the boilerplate. Two filters guard that value list: purely numeric/clock-time strings (zero-padded FIPS codes, `07:00`) and row-level geography (`state_name`, `address`, `city`) are excluded, the latter because `tabledata.list` returns contiguous rows from one site, which would make "ozone in California" match a card whose sample happens to say Alaska.

Two placement rules govern the callback. It appends to `llm_request.contents` and never to `config.system_instruction`, because ADK's instruction processor puts `static_instruction` at the *front* of `contents` (it runs before the contents processor) and the context cache covers that prefix — appending at the end leaves the cache intact. And it retrieves once per *invocation*, not once per model call: the agent loops write-SQL → run → interpret, the callback fires on every leg, so the rendered block is memoized in `temp:schema_context` state (`temp:` keys are invocation-scoped and never persisted by `FirestoreSessionService`) and re-appended from cache on later legs. Retrieval failures are logged and swallowed — a missing vector index degrades answer quality rather than breaking the agent.

Indexer and retriever must agree exactly on model, dimensionality, normalization, and instruction prefix — a mismatch degrades recall silently rather than raising — so both import from [app_utils/embeddings.py](talk_to_database_agent/app_utils/embeddings.py) instead of re-declaring the values. Two constraints are baked in there: **Firestore's vector index caps at 2048 dimensions while `gemini-embedding-2` emits 3072 by default**, hence `EMBEDDING_DIMENSIONS = 1536` (the largest recommended MRL size that fits); and `gemini-embedding-2` has no `task_type` parameter, so asymmetric retrieval is steered by the `DOC_INSTRUCTION` / `QUERY_INSTRUCTION` prefixes. Editing either prefix invalidates the whole index.

Cards are content-hashed, so re-running only re-embeds changed tables. Sampling uses `list_rows` (the free tabledata.list API) rather than `SELECT *`, and is skipped for views. The Firestore vector index is not created automatically — the script prints the required `gcloud firestore indexes composite create` command.

**Session persistence.** [app_utils/firestore_session.py](talk_to_database_agent/app_utils/firestore_session.py) is a hand-written `BaseSessionService` on async Firestore, laid out as `adk_sessions/{app}/users/{user}/sessions/{session}/events/{event}`. Two non-obvious details: Firestore rejects field names wrapped in `__`, so state keys are escaped via `_Z_...._Z_` (`_encode_key`/`_decode_key`); and each `append_event` denormalizes a `last_message_preview` / `message_count` summary onto the session doc for backoffice listing. `State.TEMP_PREFIX` keys are never persisted.

[services.py](services.py) registers this service against the `firestore://` URI scheme with ADK's service registry. [main.py](main.py) decides whether to use it: in `APP_ENV=development` outside Cloud Run (`K_SERVICE` unset) it falls back to in-memory sessions so local dev doesn't require ADC; `USE_FIRESTORE_SESSIONS` overrides either way. Note `main.py` does not import `services.py` — if the `firestore://` scheme fails to resolve, that registration is the thing to check.

## Conventions

- Prompts and user-facing agent text are English; tool docstrings, error strings, and code comments are largely Portuguese (pt-BR). Match the surrounding file rather than normalizing.
- Tool functions return a `dict` with a `"status": "success" | "error"` key and never raise into the agent loop — errors come back as data so the model can retry.
- `talk_to_database_agent/` and `sub_agents/` have no `__init__.py` (only `app_utils/` does); they resolve as namespace packages via `pythonpath = ["."]`.

## Known gaps

`todo.txt` tracks the open work (in Portuguese: chat compaction, token/temperature tuning, per-helper SQL validation tests, AI-as-judge for expensive queries). Beyond that, several seams are stubbed or broken and will bite before anything runs end to end:

- `config.py::excluded_branches_set` references a `rede_excluded_branches` field that does not exist on `Settings`.
- `run_sql_query` sets no `maximum_bytes_billed`. Aggregations scan the whole table regardless of `LIMIT` — a `GROUP BY` over `air_quality_annual_summary` bills ~148 MB, and the hourly tables are 100× larger.
