#!/usr/bin/env python
"""Record an eval case by running the agent for real and capturing what it did.

`tool_trajectory_avg_score` compares tool calls by exact name *and* args, and
the args here are free-form natural language the model writes itself. Any
prompt, model or tool change re-words them and the recorded tape stops
matching, so re-recording is a routine operation rather than a one-off.

Pin the question to a fixed period. "this month" is resolved at run time, so a
case phrased that way silently changes meaning and cannot stay green.

Usage:
    uv run python scripts/record_eval.py --question "In March 2026, ..." \
        --eval-id session_01 --dry-run
    uv run python scripts/record_eval.py --question "In March 2026, ..." \
        --eval-id session_01
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.evaluation.eval_case import (
    EvalCase,
    IntermediateData,
    Invocation,
    SessionInput,
)
from google.adk.evaluation.eval_set import EvalSet
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv()

from talk_to_database_agent.agent import app as adk_app  # noqa: E402

DEFAULT_EVALSET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "talk_to_database_agent",
    "query_sql.evalset.json",
)


async def run_once(question: str, app_name: str) -> Invocation:
    """Run the agent once and capture the invocation as an eval expectation."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name=app_name, user_id="user")

    # Built from the App, not the bare agent, so the recording exercises the
    # same plugins, context cache and compaction the served agent uses.
    runner = Runner(
        app=adk_app,
        app_name=app_name,
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
    )

    user_content = types.Content(role="user", parts=[types.Part(text=question)])

    tool_uses: list[types.FunctionCall] = []
    tool_responses: list[types.FunctionResponse] = []
    final_text: list[str] = []

    async for event in runner.run_async(
        user_id="user", session_id=session.id, new_message=user_content
    ):
        for call in event.get_function_calls() or []:
            tool_uses.append(call)
        for response in event.get_function_responses() or []:
            tool_responses.append(response)
        if event.is_final_response() and event.content and event.content.parts:
            final_text.extend(p.text for p in event.content.parts if p.text)

    return Invocation(
        invocation_id=f"e-{uuid.uuid4()}",
        user_content=user_content,
        final_response=types.Content(
            role="model", parts=[types.Part(text="".join(final_text))]
        ),
        intermediate_data=IntermediateData(
            tool_uses=tool_uses, tool_responses=tool_responses
        ),
        creation_timestamp=time.time(),
    )


def load_or_create(path: str, eval_set_id: str) -> EvalSet:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return EvalSet.model_validate_json(handle.read())
    return EvalSet(
        eval_set_id=eval_set_id,
        name=eval_set_id,
        description="Recorded by scripts/record_eval.py",
        eval_cases=[],
        creation_timestamp=time.time(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, help="Pinned, date-explicit question.")
    parser.add_argument("--eval-id", default="session_01")
    parser.add_argument("--evalset", default=DEFAULT_EVALSET)
    parser.add_argument("--eval-set-id", default="query_sql")
    parser.add_argument(
        "--app-name",
        # `adk eval` derives its app name from the agent directory
        # (`os.path.basename(agent_module_file_path)`), so anything else here
        # makes the recorded case run under a different session app name than
        # the one the eval reports against.
        default="talk_to_database_agent",
        help="Must match session_input.app_name used by the eval runner.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what was captured; write nothing."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        print("GOOGLE_CLOUD_PROJECT is not set.", file=sys.stderr)
        return 1

    print(f"Running the agent on: {args.question!r}\n")
    invocation = asyncio.run(run_once(args.question, args.app_name))

    calls = invocation.intermediate_data.tool_uses
    print(f"--- captured {len(calls)} tool call(s) ---")
    for call in calls:
        print(f"  {call.name}({json.dumps(call.args, ensure_ascii=False)[:160]})")
    text = invocation.final_response.parts[0].text or ""
    print(f"\n--- final response ({len(text)} chars) ---\n{text[:1200]}")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    eval_set = load_or_create(args.evalset, args.eval_set_id)
    case = EvalCase(
        eval_id=args.eval_id,
        conversation=[invocation],
        session_input=SessionInput(app_name=args.app_name, user_id="user", state={}),
        creation_timestamp=time.time(),
    )
    eval_set.eval_cases = [
        c for c in eval_set.eval_cases if c.eval_id != args.eval_id
    ] + [case]

    with open(args.evalset, "w", encoding="utf-8") as handle:
        handle.write(eval_set.model_dump_json(indent=2, exclude_none=True))

    print(f"\nWrote case {args.eval_id!r} to {args.evalset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
