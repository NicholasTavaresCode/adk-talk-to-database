from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Firestore ────────────────────────────────────────────────────────
    firestore_database: str = "agent-demo-db"
    use_firestore_sessions: bool | None = None

    # ── Schema index (RAG) ───────────────────────────────────────────────
    schema_index_collection: str = "schema_index"
    # How many table cards to inject per question.
    schema_index_top_k: int = 5
    # Optional COSINE distance ceiling (0 = identical, 2 = opposite). Left unset
    # by default: a miscalibrated threshold silently returns no schema at all.
    schema_index_max_distance: float | None = None
    # Comma-separated dataset ids to index; empty means every dataset in the
    # project that BQ_LOCATION exposes.
    bq_datasets: str = ""
    bq_location: str = "us-east1"

    @property
    def bq_datasets_list(self) -> list[str]:
        return [d.strip() for d in self.bq_datasets.split(",") if d.strip()]

    # ── App ──────────────────────────────────────────────────────────────
    app_env: str = "development"
    app_port: int = 8000
    app_log_level: str = "INFO"

    @property
    def excluded_branches_set(self) -> set[int]:
        return {int(b.strip()) for b in self.rede_excluded_branches.split(",") if b.strip()}

settings = Settings()