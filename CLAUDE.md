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
```

Tests hit real Vertex AI and BigQuery — they are integration tests, not unit tests, and require valid ADC plus `GOOGLE_CLOUD_PROJECT`. There is currently no offline test path.

Environment comes from `.env` (see `.env.example`), read both by `pydantic-settings` and directly via `os.environ`:

- `GOOGLE_CLOUD_PROJECT` — required by `models.py` (Vertex client) and `bq.py`; nothing works without it.
- `GOOGLE_GENAI_USE_VERTEXAI=True`, `FIRESTORE_DATABASE`, `BQ_LOCATION` (defaults `us-east1`), `USE_FIRESTORE_SESSIONS`, `APP_ENV`, `APP_PORT`.

## Architecture

An ADK (Google Agent Development Kit) natural-language-to-SQL agent served as a FastAPI app.

**Two-tier agent design.** [talk_to_database_agent/agent.py](talk_to_database_agent/agent.py) defines `root_agent` (persona "Michael") — a conversational front end that owns clarification, formatting, and time/math tools, and is instructed to delegate *all* SQL generation to a `bigquery_agent` tool. [sub_agents/bigquery_agent/agent.py](talk_to_database_agent/sub_agents/bigquery_agent/agent.py) is the SQL specialist: it returns a fixed JSON envelope (`explain`, `sql`, `sql_results`, `nl_results`) and executes through the single `run_sql_query` tool. The two agents share `GEMINI_MODEL` and `MATH_TOOLS`.

**The `App` wrapper is where cross-cutting behavior lives.** `agent.py` wraps `root_agent` in an ADK `App` carrying `ContextCacheConfig` (implicit prompt caching) and plugins (`ReflectAndRetryToolPlugin`, `LogPlugin`). Plugins in [talk_to_database_agent/plugins/](talk_to_database_agent/plugins/) are app-level and apply to every agent — `LogPlugin` is a verbose console tracer for every ADK callback point, `RateLimiterPlugin` throttles LLM calls (written but not currently registered).

**Static vs. dynamic instructions.** Agents use `static_instruction` for the stable prompt body (this is what the context cache keys on) and `instruction` for a callable that recomputes per-turn context — see `dynamic_instruction` in [sub_agents/bigquery_agent/prompts.py](talk_to_database_agent/sub_agents/bigquery_agent/prompts.py). Keep volatile data (timestamps, RAG snippets) out of `static_instruction` or caching breaks. `before_model_callback=rag` in the BigQuery agent is the intended hook for schema retrieval.

**Model config is centralized** in [app_utils/models.py](talk_to_database_agent/app_utils/models.py): one `Gemini` instance with an explicit `api_client` pinned to Vertex AI `location="global"` and `HttpRetryOptions` on both the model and the client. Both agents run `temperature=0.01` and `function_calling_config mode="VALIDATED"`.

**No agent does arithmetic itself.** Both prompts forbid it and route through `calculate` / `percentage_change` / `proportion` in [app_utils/math.py](talk_to_database_agent/app_utils/math.py). Likewise, the model must never invent the current date — `build_timezone_metadata()` ([app_utils/utils.py](talk_to_database_agent/app_utils/utils.py)) is the only source, fixed to `America/Sao_Paulo` with an April-start fiscal year.

**SQL safety is layered in `tools.py`, not in the prompt.** `run_sql_query` runs `sanitize_sql` (strip markdown fences, strip trailing `;`, reject multi-statement queries — using `_mask_literals_and_comments` so keywords inside string literals don't trip it), then `check_sql_read_only` (keyword denylist), then executes and passes rows through `sanitize_value`/`sanitize_rows` to make BigQuery types JSON-safe (Decimal, datetime, bytes, non-finite floats). New validation helpers belong here as pure functions so they stay testable.

**Session persistence.** [app_utils/firestore_session.py](talk_to_database_agent/app_utils/firestore_session.py) is a hand-written `BaseSessionService` on async Firestore, laid out as `adk_sessions/{app}/users/{user}/sessions/{session}/events/{event}`. Two non-obvious details: Firestore rejects field names wrapped in `__`, so state keys are escaped via `_Z_...._Z_` (`_encode_key`/`_decode_key`); and each `append_event` denormalizes a `last_message_preview` / `message_count` summary onto the session doc for backoffice listing. `State.TEMP_PREFIX` keys are never persisted.

[services.py](services.py) registers this service against the `firestore://` URI scheme with ADK's service registry. [main.py](main.py) decides whether to use it: in `APP_ENV=development` outside Cloud Run (`K_SERVICE` unset) it falls back to in-memory sessions so local dev doesn't require ADC; `USE_FIRESTORE_SESSIONS` overrides either way. Note `main.py` does not import `services.py` — if the `firestore://` scheme fails to resolve, that registration is the thing to check.

## Conventions

- Prompts and user-facing agent text are English; tool docstrings, error strings, and code comments are largely Portuguese (pt-BR). Match the surrounding file rather than normalizing.
- Tool functions return a `dict` with a `"status": "success" | "error"` key and never raise into the agent loop — errors come back as data so the model can retry.
- `talk_to_database_agent/` and `sub_agents/` have no `__init__.py` (only `app_utils/` does); they resolve as namespace packages via `pythonpath = ["."]`.

## Known gaps

`todo.txt` tracks the open work (in Portuguese: chat compaction, token/temperature tuning, per-helper SQL validation tests, AI-as-judge for expensive queries). Beyond that, several seams are stubbed or broken and will bite before anything runs end to end:

- `root_agent.tools` does not actually include the BigQuery sub-agent, though its prompt tells it to call one — the `AgentTool` wiring is missing.
- `prompts.py::dynamic_instruction` does `"" + build_timezone_metadata()`, concatenating a str with a dict (`TypeError`).
- `bq.py::get_bq_client` reads a `_BQ_CLIENT` global that is never defined (`NameError`).
- `context.py::rag` is an empty stub, but is wired as `before_model_callback`.
- `config.py::excluded_branches_set` references a `rede_excluded_branches` field that does not exist on `Settings`.
