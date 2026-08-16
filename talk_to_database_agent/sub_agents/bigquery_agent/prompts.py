import logging

from google.adk.agents.readonly_context import ReadonlyContext

from talk_to_database_agent.app_utils.utils import build_timezone_metadata

logger = logging.getLogger(__name__)


async def dynamic_instruction(readonly_context: ReadonlyContext) -> str:
    """Build the BigQuery agent instruction with fresh datetime metadata.

    Recomputed per turn, so it must not go in static_instruction: that is the
    part the context cache keys on.
    """
    meta = build_timezone_metadata()

    prompt = (
        "# CURRENT DATE AND TIME\n"
        "Use these values for any relative date the user mentions "
        "(today, this month, last year). Do not infer the date yourself.\n"
        f"- Current timestamp (America/Sao_Paulo): {meta['now_str']}\n"
        f"- Today (DD/MM/YYYY): {meta['now_date']}\n"
        f"- Current month: {meta['month_year']}\n"
        f"- Current fiscal year (starts in April): {meta['fy_current']}\n"
        f"- Previous fiscal year: {meta['fy_prev']}\n"
    )

    logger.debug("Dynamic instruction built: %s", prompt)
    return prompt
