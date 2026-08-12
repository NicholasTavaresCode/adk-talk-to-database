
from google.adk.agents import Agent
from google.genai import types
from talk_to_database_agent.app_utils.models import GEMINI_MODEL
from talk_to_database_agent.app_utils.math import MATH_TOOLS
from .callbacks import inject_database_context
from .prompts import build_bigquery_instruction
from .tools import run_sql_query

bigquery_agent = Agent(
    name="bigquery_agent",
    model=GEMINI_MODEL,
    description=("AI agent that can translate natural language queries into BigQuery SQL and execute them."),
    before_model_callback=inject_database_context,
    instruction=build_bigquery_instruction,
    tools=[run_sql_query] + MATH_TOOLS,
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=65535,
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="VALIDATED")
        )
    ),
)
