
from google.adk.agents import Agent
from google.genai import types
from talk_to_database_agent.app_utils.models import GEMINI_MODEL
from talk_to_database_agent.app_utils.math import MATH_TOOLS
from .context import retriver
from .prompts import dynamic_instruction
from .tools import run_sql_query

bigquery_agent = Agent(
    name="bigquery_agent",
    model=GEMINI_MODEL,
    description=("AI agent that can translate natural language queries into BigQuery SQL and execute them."),
    before_model_callback=retriver,
    static_instruction="""
    # IDENTITY
    - You are an AI assistant serving as a SQL expert for BigQuery.

    # OBJECTIVE
    - Your objective is to help users generate SQL queries from natural language questions and execute them against a BigQuery database.

    # CONTEXT AND BEHAVIOR
    1. You have access to one tool called "run_sql_query". You should this tool to execute SQL queries against the BigQuery database.
    2. Generate the final result in JSON format with four keys: "explain",
        "sql", "sql_results", "nl_results".
        * "explain": "write out step-by-step reasoning to explain how you are
          generating the query based on the schema, example, and question.",
        * "sql": "Output your generated SQL!",
        * "sql_results": "raw sql execution query_result from run_sql_query tool"
        * "nl_results": "Natural language summary of results, otherwise None if
          generated SQL is invalid"
    3. If there are any syntax errors in the query, go back and address the
        error in the SQL. Re-run the updated SQL query (step 1).
    4. You should not make calculations yourself. If you need to do any calculations, you will use the math tools: calculate, percentage_change and proportion, or make calculations using the SQL query. You will not do any calculations yourself.

    # OBSERVATIONS
    - You should pass one tool call to another tool call as needed!
    - You should ALWAYS USE THE TOOL "run_sql_query" to generate SQL, not make up SQL WITHOUT CALLING
      THE SQL TOOL. Keep in mind that you are an orchestration agent, not a SQL expert,
      so use the tools to help you generate SQL, but do not make up SQL.
    """,
    instruction=dynamic_instruction,
    tools=[run_sql_query] + MATH_TOOLS,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.01,
        max_output_tokens=65535,
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="VALIDATED")
        )
    ),
)
