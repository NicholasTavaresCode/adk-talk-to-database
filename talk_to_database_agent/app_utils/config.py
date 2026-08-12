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

    # ── App ──────────────────────────────────────────────────────────────
    app_env: str = "development"
    app_port: int = 8000
    app_log_level: str = "INFO"

    @property
    def excluded_branches_set(self) -> set[int]:
        return {int(b.strip()) for b in self.rede_excluded_branches.split(",") if b.strip()}

settings = Settings()