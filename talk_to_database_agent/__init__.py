"""Agent package entrypoint.

`adk eval` loads this file directly with `spec_from_file_location`
(`cli_eval._get_agent_module`) and then reads `root_agent` off it. Loading it
that way does not put the repo root on `sys.path`, so the absolute
`talk_to_database_agent.*` imports used throughout the tree would fail — hence
the same bootstrap `scripts/index_schema.py` and `scripts/record_eval.py` use.

`adk web` / `get_fast_api_app` go through `agent_loader`, which checks for `app`
before `root_agent`. Both are re-exported so every entry point loads the same
objects — note that `adk eval` takes only the bare agent, so an eval run does
*not* exercise the App's plugins, context cache or events compaction.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from talk_to_database_agent.agent import app, root_agent

__all__ = ["app", "root_agent"]
