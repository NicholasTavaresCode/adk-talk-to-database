"""Clientes BigQuery.

Vertex AI, Firestore e BigQuery seguem todos a MESMA credencial: o ADC do
runtime. Em produção é a service account do Cloud Run, que tem
bigquery.jobUser e bigquery.dataViewer no projeto do warehouse; local, é o ADC
do gcloud. Não há chave de service account em lugar nenhum — o `SA_BQ` que
existia aqui (JSON da chave vindo do Secret Manager) foi removido junto com o
segredo, porque a identidade do runtime já basta.

As opções de chave e de impersonation continuam em Settings, para o caso de o
warehouse voltar a ficar em outro projeto; nenhuma delas é usada hoje.

Uma query só enxerga datasets da location do cliente que a executa, e os dois
consumidores deste módulo estão em locations diferentes:

* a busca de schema roda em `US`, onde ficam `auditor_contratos_ia` e o modelo
  de embedding que `AI.EMBED` usa (ele não existe em outras regiões);
* `run_sql_query` roda em `us-east1`, onde ficam os dados (`raw`, `loads`).

Por isso o cache é por location: uma credencial, um cliente para cada região.
"""

import logging

import google.auth
from google.auth import impersonated_credentials
from google.cloud import bigquery
from google.oauth2 import service_account

from talk_to_database_agent.app_utils.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_BQ_CLIENTS: dict[str, bigquery.Client] = {}

_CREDENTIALS: tuple | None = None

def _project_from_service_account_email(email: str) -> str | None:
    """Extrai o projeto de `nome@projeto.iam.gserviceaccount.com`."""
    _, _, domain = email.partition("@")
    project, sep, suffix = domain.partition(".")
    if sep and suffix == "iam.gserviceaccount.com" and project:
        return project
    return None

def _load_credentials() -> tuple[object | None, str | None, str]:
    """Monta a credencial do BigQuery.

    A ordem de precedência é a declarada em Settings: a primeira opção
    preenchida vence. Devolve `(credentials, project, mode)`, onde
    `credentials=None` significa ADC — o cliente descobre sozinho.
    """
    if settings.bq_service_account_file:
        credentials = service_account.Credentials.from_service_account_file(
            settings.bq_service_account_file, scopes=_SCOPES
        )
        return credentials, credentials.project_id, "service account (key file)"

    if settings.bq_impersonate_service_account:
        target = settings.bq_impersonate_service_account
        source_credentials, _ = google.auth.default(scopes=_SCOPES)
        credentials = impersonated_credentials.Credentials(
            source_credentials=source_credentials,
            target_principal=target,
            target_scopes=_SCOPES,
        )
        return (
            credentials,
            _project_from_service_account_email(target),
            f"impersonation of {target}",
        )

    return None, None, "application default credentials"

def _credentials() -> tuple:
    """Descobre a credencial uma vez e reaproveita entre os clientes."""
    global _CREDENTIALS
    if _CREDENTIALS is None:
        _CREDENTIALS = _load_credentials()
    return _CREDENTIALS

def get_bq_client(location: str | None = None) -> bigquery.Client:
    """Cliente BigQuery em cache para uma location, como service account.

    Args:
        location: Região onde a query roda. O default é `settings.bq_location`
            (`us-east1`, onde estão os dados). A busca de schema passa
            `settings.schema_search_location` (`US`) explicitamente.
    """
    resolved_location = location or settings.bq_location

    if not resolved_location:
        raise RuntimeError("BQ_LOCATION environment variable is not set.")

    cached = _BQ_CLIENTS.get(resolved_location)
    if cached is not None:
        return cached

    credentials, credential_project, mode = _credentials()

    project = (
        settings.bq_project or credential_project or settings.google_cloud_project
    )

    if not project:
        raise RuntimeError(
            "Nenhum projeto para faturar as queries do BigQuery. Defina BQ_PROJECT "
            "(ou GOOGLE_CLOUD_PROJECT, se a aplicação e os dados estiverem no "
            "mesmo projeto)."
        )

    client = bigquery.Client(
        project=project,
        location=resolved_location,
        credentials=credentials,
    )
    _BQ_CLIENTS[resolved_location] = client
    logger.info(
        "BigQuery client initialized: project=%s location=%s auth=%s",
        project,
        resolved_location,
        mode,
    )
    return client
