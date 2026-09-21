from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./career.db"
    redis_url: str = "redis://localhost:6379/0"
    object_store_endpoint: str = "http://localhost:9000"
    object_store_bucket: str = "career-artifacts"
    object_store_access_key: str = "minioadmin"
    object_store_secret_key: str = "minioadmin"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "career-system"
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str | None = None
    gmail_sender: str | None = None
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    gmail_refresh_token: str | None = None
    github_org: str | None = None
    github_token: str | None = None
    browser_enabled: bool = False
    artifact_dir: str = "/tmp/career-artifacts"
    live_actions_enabled: bool = False
    daily_application_limit: int = 0
    daily_outreach_limit: int = 0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
