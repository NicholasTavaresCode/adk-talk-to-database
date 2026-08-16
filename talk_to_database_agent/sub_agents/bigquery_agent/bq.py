import os, logging
from google.cloud import bigquery

logger = logging.getLogger(__name__)

# Cache do cliente: criar um por query desperdiça a descoberta de credenciais.
_BQ_CLIENT: bigquery.Client | None = None


def get_bq_client() -> bigquery.Client:
    """Devolve um cliente BigQuery em cache, autenticado via ADC."""
    global _BQ_CLIENT
    if _BQ_CLIENT is not None:
        return _BQ_CLIENT

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bq_location = os.environ.get("BQ_LOCATION", "us-east1")

    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT environment variable is not set.")

    if not bq_location:
        raise RuntimeError("BQ_LOCATION environment variable is not set.")


    _BQ_CLIENT = bigquery.Client(
        project=project,
        location=bq_location,
    )
    logger.info("BigQuery client initialized")
    return _BQ_CLIENT