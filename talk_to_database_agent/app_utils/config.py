from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_cloud_project: str = ""

    firestore_database: str = "(default)"
    use_firestore_sessions: bool | None = None

    schema_search_routine: str = (
        "pluz-ai.rag_practice.search_table_cards"
    )
    schema_search_location: str = "US"
    schema_index_top_k: int = 5
    schema_index_max_distance: float | None = None
    bq_datasets: str = ""
    bq_location: str = "us-central1"

    bq_max_bytes_billed: int = 20 * 1024**3
    bq_query_timeout_seconds: float = 120.0

    @property
    def bq_datasets_list(self) -> list[str]:
        return [d.strip() for d in self.bq_datasets.split(",") if d.strip()]

    bq_project: str = ""

    bq_service_account_file: str = ""
    bq_impersonate_service_account: str = ""

    app_env: str = "development"
    app_port: int = 8000
    app_log_level: str = "INFO"

    @property
    def excluded_branches_set(self) -> set[int]:
        return {int(b.strip()) for b in self.rede_excluded_branches.split(",") if b.strip()}

settings = Settings()
