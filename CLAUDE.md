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

# Golden-dataset eval of the happy path (question -> bigquery_agent -> SQL -> answer)
uv run adk eval talk_to_database_agent talk_to_database_agent/query_sql.evalset.json \
    --config_file_path talk_to_database_agent/test_config.json
uv run python scripts/record_eval.py --question "In May 2026, ..." --eval-id session_01
```

`uv run pytest -m "not bigquery_agent"` is the offline subset: `tests/test_sql_guards.py` is pure functions and needs no credentials. Everything else — `tests/test_agents.py` and the eval — hits real Vertex AI and BigQuery and requires valid ADC plus `GOOGLE_CLOUD_PROJECT`.

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

**SQL safety is layered in `tools.py`, not in the prompt.** `run_sql_query` runs `sanitize_sql` (strip markdown fences, strip trailing `;`, reject multi-statement queries — using `_mask_literals_and_comments` so keywords inside string literals don't trip it), then `check_sql_read_only`, then executes and passes rows through `sanitize_value`/`sanitize_rows` to make BigQuery types JSON-safe (Decimal, datetime, bytes, non-finite floats). New validation helpers belong here as pure functions so they stay testable — `tests/test_sql_guards.py` covers them offline.

`check_sql_read_only` allowlists the *leading token* (`SELECT` or `WITH`) of the masked query rather than scanning the raw text for forbidden keywords. Both halves of that matter. Scanning the raw text rejected valid reads whose column names merely contain a keyword (`updated_at`, `created_date`), and any query with a comment like `-- create a summary`; scanning for keywords at all missed `EXPORT DATA OPTIONS(uri='gs://…') AS SELECT *`, which writes the whole result set out to GCS. Since `sanitize_sql` has already rejected multi-statement input, the first token determines the statement kind. A word-boundary denylist over the masked body remains as a second layer.

**Query cost is capped in two places.** `LIMIT` does not reduce bytes scanned — `SELECT * FROM o3_hourly_summary LIMIT 10` reads all 118 GB of that table. `_run_sql_query_blocking` therefore does a `dry_run` first (free) and refuses anything over `bq_max_bytes_billed` (default 20 GB) with an error that tells the model *why* and how to narrow it, so `ReflectAndRetryToolPlugin` can drive a useful retry; `maximum_bytes_billed` is then set on the real job as a backstop against the estimate going stale, alongside `job_timeout_ms`. The dry run costs an extra round trip (~0.8 s) on every query — that is the price of turning an opaque post-hoc 400 into something the model can act on. Successful results carry `bytes_billed` so cost is visible in the transcript.

**`run_sql_query` is `async` on purpose.** The BigQuery client is synchronous and a query takes seconds; called inline it would block the whole uvicorn event loop, stalling every other user's request (see `adk-debug/references/failure-modes.md`, "The whole agent stalls while one tool runs"). The blocking client work lives in `_run_sql_query_blocking` and is dispatched with `asyncio.to_thread`; ADK awaits coroutine tools directly in `FunctionTool._invoke_callable`.

**Schema RAG.** BigQuery datasets are too wide to put in the prompt, so [scripts/index_schema.py](scripts/index_schema.py) renders one "schema card" per table (description, columns + their descriptions, partitioning, 5 sample rows), embeds it, and stores the vector in Firestore. At runtime `retriever` in [sub_agents/bigquery_agent/context.py](talk_to_database_agent/sub_agents/bigquery_agent/context.py) — the BigQuery agent's `before_model_callback` — embeds the user's question, nearest-neighbours that collection, and appends the matching cards.

**What gets embedded is not what gets injected.** Each table produces both a `card` (full schema, injected into the prompt) and a shorter `embed_text` (what the vector is built from). This matters for template-shaped warehouses: every table in `bigquery-public-data.epa_historical_air_quality` repeats the same ~20 boilerplate columns with byte-identical descriptions and no table description at all, so full-card embeddings are ~97% shared text and `parameter_name = "Ozone"` gets averaged into noise. `embed_text` keeps the table name (underscores split into words), column *names* only, row count, and "representative values" sampled from low-cardinality STRING columns — dropping the long column descriptions that are the boilerplate. Two filters guard that value list: purely numeric/clock-time strings (zero-padded FIPS codes, `07:00`) and row-level geography (`state_name`, `address`, `city`) are excluded, the latter because `tabledata.list` returns contiguous rows from one site, which would make "ozone in California" match a card whose sample happens to say Alaska.

Two placement rules govern the callback. It appends to `llm_request.contents` and never to `config.system_instruction`, because ADK's instruction processor puts `static_instruction` at the *front* of `contents` (it runs before the contents processor) and the context cache covers that prefix — appending at the end leaves the cache intact. And it retrieves once per *invocation*, not once per model call: the agent loops write-SQL → run → interpret, the callback fires on every leg, so the rendered block is memoized in `temp:schema_context` state (`temp:` keys are invocation-scoped and never persisted by `FirestoreSessionService`) and re-appended from cache on later legs. Retrieval failures are logged and swallowed — a missing vector index degrades answer quality rather than breaking the agent.

Indexer and retriever must agree exactly on model, dimensionality, normalization, and instruction prefix — a mismatch degrades recall silently rather than raising — so both import from [app_utils/embeddings.py](talk_to_database_agent/app_utils/embeddings.py) instead of re-declaring the values. Two constraints are baked in there: **Firestore's vector index caps at 2048 dimensions while `gemini-embedding-2` emits 3072 by default**, hence `EMBEDDING_DIMENSIONS = 1536` (the largest recommended MRL size that fits); and `gemini-embedding-2` has no `task_type` parameter, so asymmetric retrieval is steered by the `DOC_INSTRUCTION` / `QUERY_INSTRUCTION` prefixes. Editing either prefix invalidates the whole index.

Cards are content-hashed, so re-running only re-embeds changed tables. Sampling uses `list_rows` (the free tabledata.list API) rather than `SELECT *`, and is skipped for views. The Firestore vector index is not created automatically — the script prints the required `gcloud firestore indexes composite create` command.

**Session persistence.** [app_utils/firestore_session.py](talk_to_database_agent/app_utils/firestore_session.py) is a hand-written `BaseSessionService` on async Firestore, laid out as `adk_sessions/{app}/users/{user}/sessions/{session}/events/{event}`. Two non-obvious details: Firestore rejects field names wrapped in `__`, so state keys are escaped via `_Z_...._Z_` (`_encode_key`/`_decode_key`); and each `append_event` denormalizes a `last_message_preview` / `message_count` summary onto the session doc for backoffice listing. `State.TEMP_PREFIX` keys are never persisted.

[services.py](services.py) registers this service against the `firestore://` URI scheme with ADK's service registry. [main.py](main.py) decides whether to use it: in `APP_ENV=development` outside Cloud Run (`K_SERVICE` unset) it falls back to in-memory sessions so local dev doesn't require ADC; `USE_FIRESTORE_SESSIONS` overrides either way. Note `main.py` does not import `services.py` — if the `firestore://` scheme fails to resolve, that registration is the thing to check.

**The eval scores the delegation, not the wording.** `adk eval` loads
`talk_to_database_agent/__init__.py` directly via `spec_from_file_location`
(`cli_eval._get_agent_module`) and reads `root_agent` off it — hence the
`__init__.py`, which also bootstraps `sys.path` because that import path does not
put the repo root on it. Note `adk eval` takes only the bare agent, so an eval run
does *not* exercise the App's plugins, context cache or compaction.

`tool_trajectory_avg_score` is deliberately **not** in `test_config.json`. Every
one of its match types (`EXACT`, `IN_ORDER`, `ANY_ORDER`) compares tool calls with
`actual.args == expected.args`, and the root agent's only tool is `bigquery_agent`,
whose single argument is a free-form natural-language `request` the model composes
itself. Exact dict equality on that string cannot hold across a prompt or model
change, so the metric scored 0.0 while the agent was answering correctly.

Both metrics in `test_config.json` are therefore rubric-based and LLM-judged by
`gemini-2.5-flash` — a different model from the one under test, so the judge is not
grading its own output — with `num_samples: 3` for majority voting.
`rubric_based_tool_use_quality_v1` asserts the delegation itself (the agent calls
`bigquery_agent`; the request it passes preserves the user's intent).
`rubric_based_final_response_quality_v1` asserts the *substance* of the answer
against the golden dataset (ten locations ranked descending; the first is ~42.46
in Scott County, Iowa; the tenth is ~20.12 in Fresno County, California).

Both thresholds are `1.0` on purpose, and three earlier attempts were dropped for
failing to discriminate — verify any new metric against a deliberately broken tape
before trusting it:

- At `0.66` the tool-use metric *passed* a tape where the agent never called the
  tool at all, because the remaining rubrics were vacuously true with no tool call
  to inspect. The rubrics are now worded to fail explicitly in that case.
- An `answer_is_grounded_in_tool_output` rubric scored 1.0 even on a tape with
  wholly fabricated figures, and `hallucinations_v1` failed the *correct* tape
  (0.33) as well as the fabricated one.
- `final_response_match_v2` compares against the recorded response and is too
  strict for this agent: a run that returned all ten locations with all ten values
  identical to the golden tape scored 0.0, because it rendered them as a numbered
  list rather than a table and one incidental site-name cell read "N/A" instead of
  "CITY SANITATION BLDG". The rubric metric passes that same response and still
  scores fabricated figures 0.0.

Eval questions must be pinned to a fixed period. The EPA data ends `2026-05-31`,
so "this month" makes the agent spend an extra exploratory `bigquery_agent` call
discovering there is no current-month data — non-deterministic, and it changes
meaning as time passes. `session_input.app_name` must match the agent directory
name, since `adk eval` derives its app name from `basename(agent_module_file_path)`.

## Conventions

- Prompts and user-facing agent text are English; tool docstrings, error strings, and code comments are largely Portuguese (pt-BR). Match the surrounding file rather than normalizing.
- Tool functions return a `dict` with a `"status": "success" | "error"` key and never raise into the agent loop — errors come back as data so the model can retry.
- `talk_to_database_agent/` and `sub_agents/` have no `__init__.py` (only `app_utils/` does); they resolve as namespace packages via `pythonpath = ["."]`.

## Known gaps

`todo.txt` tracks the open work (in Portuguese: chat compaction, token/temperature tuning, per-helper SQL validation tests, AI-as-judge for expensive queries). Beyond that, several seams are stubbed or broken and will bite before anything runs end to end:

- `config.py::excluded_branches_set` references a `rede_excluded_branches` field that does not exist on `Settings`. Calling it raises `AttributeError`; nothing calls it today.
- `math.py::calculate` runs `eval`. The name filter blocks attribute access and dunders, but not unbounded computation: `pow(9, 9 ** 9)` is pure arithmetic that passes the filter and pins a CPU indefinitely. Being sync, it blocks the event loop the way `run_sql_query` used to — the same fix (a thread plus a timeout, or an expression-tree evaluator instead of `eval`) applies.
- The root agent never receives the current date. `agent.py` imports `build_timezone_metadata` but has no `instruction=` callable, so only the BigQuery agent gets the date block — the front end resolving "this month" before delegating is guessing.
- `AgentTool(agent=bigquery_agent)` is constructed without `skip_summarization`, so the root agent makes an extra LLM call to restate the sub-agent's answer.
