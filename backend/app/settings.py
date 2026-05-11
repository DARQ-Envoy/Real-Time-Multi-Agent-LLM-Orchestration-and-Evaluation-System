from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LLM_API_KEY: str = "stub-not-used-in-slice-0"
    LLM_MODEL: str = "claude-sonnet-4-20250514"
    LLM_BASE_URL: str | None = None

    DATABASE_URL: str = "postgresql://mega:mega@db:5432/mega"
    REDIS_URL: str = "redis://redis:6379"

    TAVILY_API_KEY: str = ""
    MEGA_RO_PASSWORD: str = "mega_ro"

    MAX_BUDGET_TOKENS: int = 32768
    TOOL_TIMEOUT_SECONDS: int = 10
    CODE_EXEC_TIMEOUT_SECONDS: int = 10
    WEB_SEARCH_TIMEOUT_SECONDS: int = 5
    SQL_LOOKUP_TIMEOUT_SECONDS: int = 8

    # Per-agent context budgets (E3). Values are token counts; the gate uses
    # the deterministic `len(text) // 4` heuristic in `app.budget`.
    BUDGET_ORCHESTRATOR: int = 4096
    BUDGET_DECOMP: int = 3072
    BUDGET_RAG: int = 6144
    BUDGET_CRITIQUE: int = 4096
    BUDGET_SYNTHESIS: int = 8192
    BUDGET_COMPRESSION: int = 2048

    LOG_LEVEL: str = "INFO"
    EVAL_CONCURRENCY: int = 4
    CUT_DECOMPOSITION: bool = False
    CUT_RESOLUTION_LOOP: bool = False


settings = Settings()
