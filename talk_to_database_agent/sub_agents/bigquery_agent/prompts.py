import logging
from google.adk.agents.readonly_context import ReadonlyContext
from talk_to_database_agent.app_utils.utils import build_timezone_metadata

async def dynamic_instruction(readonly_context: ReadonlyContext) -> str:
    """Build BigQuery agent instruction with fresh datetime metadata."""
    logging.log(logging.DEBUG, "Building dynamic instruction for BigQuery agent...") 
    prompt = "" + build_timezone_metadata()
    logging.log(logging.DEBUG, f"Dynamic instruction built: {prompt}")

    return prompt