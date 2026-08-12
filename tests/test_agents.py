import os
import sys
import unittest

import pytest
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from talk_to_database_agent.agent import root_agent
from talk_to_database_agent.sub_agents.bigquery_agent import bigquery_agent

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

session_service = InMemorySessionService()
artifact_service = InMemoryArtifactService()


class TestAgents(unittest.IsolatedAsyncioTestCase):
    """Test cases for the analytics agent and its sub-agents."""

    async def asyncSetUp(self):
        """Set up for test methods."""
        super().setUp()
        self.session = await session_service.create_session(
            app_name="DataAgent",
            user_id="test_user",
        )
        self.user_id = "test_user"
        self.session_id = self.session.id

        self.runner = Runner(
            app_name="DataAgent",
            agent=None,
            artifact_service=artifact_service,
            session_service=session_service,
        )

    def _run_agent(self, agent, query):
        """Helper method to run an agent and get the final response."""
        self.runner.agent = agent
        content = types.Content(role="user", parts=[types.Part(text=query)])
        events = list(
            self.runner.run(
                user_id=self.user_id,
                session_id=self.session_id,
                new_message=content,
            )
        )

        last_event = events[-1]
        final_response = "".join(
            [part.text for part in last_event.content.parts if part.text]
        )
        return final_response

    @pytest.mark.bigquery_agent
    async def test_bigquery_agent_can_handle_env_query(self):
        """Test the bigquery_agent with a query from environment variable."""
        query = "what countries exist in the train table?"
        response = self._run_agent(bigquery_agent, query)
        print(response)
        self.assertIsNotNone(response)

if __name__ == "__main__":
    unittest.main()