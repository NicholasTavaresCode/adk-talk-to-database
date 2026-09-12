import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from talk_to_database_agent.sub_agents.bigquery_agent.agent import bigquery_agent

root_agent = bigquery_agent

__all__ = ["root_agent"]