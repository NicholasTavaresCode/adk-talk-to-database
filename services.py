import os

from google.adk.cli.service_registry import get_service_registry

from talk_to_database_agent.app_utils.firestore_session import FirestoreSessionService


def _firestore_session_factory(uri: str, **_kwargs) -> FirestoreSessionService:
    """Factory called by get_fast_api_app when parsing firestore:// URI."""
    database = uri.replace("firestore://", "") or "(default)"
    project = os.getenv("GOOGLE_CLOUD_PROJECT")

    return FirestoreSessionService(project=project, database=database)


registry = get_service_registry()
registry.register_session_service("firestore", _firestore_session_factory)