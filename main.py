import logging, os, warnings
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from talk_to_database_agent.app_utils.config import settings

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

def _should_use_firestore_sessions() -> bool:
    if settings.use_firestore_sessions is not None:
        return settings.use_firestore_sessions

    # Local dev should not block on Google ADC just to use the dev UI.
    if settings.app_env == "development" and not os.getenv("K_SERVICE"):
        return False

    return True


session_uri = None
if _should_use_firestore_sessions() and settings.firestore_database:
    session_uri = f"firestore://{settings.firestore_database}"

if session_uri is None and settings.firestore_database:
    logging.getLogger(__name__).warning(
        "Local dev is using in-memory sessions. Clearing Firestore will not reset conversation state. "
        "Set USE_FIRESTORE_SESSIONS=true to persist sessions in Firestore locally."
    )

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    session_service_uri=session_uri,
    web=True,
)

if __name__ == "__main__":
    import uvicorn
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
