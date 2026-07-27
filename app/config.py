from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Devin
    devin_api_key: str = ""
    devin_org_id: str = ""
    # Attributes API-created sessions to this human user in the Devin dashboard
    # instead of the API key's own service-user/bot identity. Optional.
    devin_user_id: str = ""
    # Reusable "how to remediate a finding" playbook. Optional --
    # sessions still work without it, just without the standing procedure.
    devin_playbook_id: str = ""

    # GitHub
    github_token: str = ""
    github_repo_owner: str = ""
    github_repo_name: str = ""

    # Webhook
    webhook_secret: str = "changeme"

    # Orchestrator behavior
    acu_cap: float = 10
    max_concurrent_sessions: int = 3

    # Lifecycle poller
    poll_interval_seconds: int = 20
    session_max_age_hours: float = 2
    escalation_acu_ratio: float = 0.85
    auto_merge: bool = True
    max_concurrent_http: int = 12

    # Datastore
    database_url: str = "sqlite:///./data/orchestrator.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
