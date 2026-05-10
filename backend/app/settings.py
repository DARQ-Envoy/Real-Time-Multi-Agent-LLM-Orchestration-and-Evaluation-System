from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LLM_API_KEY: str = "stub-not-used-in-slice-0"
    LLM_MODEL: str = "claude-sonnet-4-20250514"
    LLM_BASE_URL: str | None = None

    DATABASE_URL: str = "postgresql://mega:mega@db:5432/mega"
    REDIS_URL: str = "redis://redis:6379"

    MAX_BUDGET_TOKENS: int = 32768
    TOOL_TIMEOUT_SECONDS: int = 10
    CODE_EXEC_TIMEOUT_SECONDS: int = 10
    LOG_LEVEL: str = "INFO"
    EVAL_CONCURRENCY: int = 4
    CUT_DECOMPOSITION: bool = False
    CUT_RESOLUTION_LOOP: bool = False


settings = Settings()
