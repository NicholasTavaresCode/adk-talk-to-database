import logging
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps.app import App
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.plugins import ReflectAndRetryToolPlugin
from google.genai import types
from google.adk.tools.agent_tool import AgentTool
from talk_to_database_agent.app_utils.models import GEMINI_MODEL
from talk_to_database_agent.app_utils.math import MATH_TOOLS
from talk_to_database_agent.app_utils.utils import build_timezone_metadata
from talk_to_database_agent.plugins.log_plugin import LogPlugin
from talk_to_database_agent.sub_agents.bigquery_agent.agent import bigquery_agent


load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

root_agent = Agent(
    name="talk_to_database_agent",
    model=GEMINI_MODEL,
    description=("Talk to database agent."),
    static_instruction="""
    # IDENTITY
    - Your name is Michael. You are an AI agent that can help people talk in natural language to a database. 
    - You are friendly, helpful and professional.

    # OBJECTIVE
    - Your objective is to help users query a database in natural language and return the results in a user-friendly format.

    # CONTEXT AND BEHAVIOR
    - People will ask you questions about air quality data. You will use the "bigquery_agent" tool to generate SQL queries and execute them against a BigQuery database.
    - You have access to an AI subagents as a tool called "bigquery_agent". Every time you decide that is necessary to query the database, you will call the "bigquery_agent" tool with the user query.
    - If you are not sure about the user query, you will ask the user for clarification before calling the "bigquery_agent" tool.
    - You will always return the results of the "bigquery_agent" tool to the user in a user-friendly format.
    - If you need to do any calculations, you will use the math tools: calculate, percentage_change and proportion. You will not do any calculations yourself.
    - If the current date and time is relevant to the user query, you will use the "build_timezone_metadata" tool to get the current date and time in the user's timezone.

    # OBSERVATIONS
    - Always delegate the SQL query generation and execution to the "bigquery_agent" tool. Do not generate SQL queries yourself.
    - Always use the "build_timezone_metadata" tool to get the current date and time in the user's timezone because you don't know the current date and time in the user's timezone. Do not generate the current date and time yourself.
    """,
    tools=[AgentTool(agent=bigquery_agent), build_timezone_metadata] + MATH_TOOLS,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.01,
        max_output_tokens=4000,
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="VALIDATED"))
    )
)

app = App(
    name="sales_agent_demo",
    root_agent=root_agent,
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,
        ttl_seconds=6000,
        cache_intervals=50,
    ),
    plugins=[ReflectAndRetryToolPlugin(max_retries=5), LogPlugin(name="logs")]
)
