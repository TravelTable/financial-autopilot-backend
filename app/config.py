from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "dev"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    DATABASE_URL: str
    REDIS_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    JWT_SECRET: str
    JWT_ISSUER: str = "financial-autopilot"
    JWT_AUDIENCE: str = "financial-autopilot-mobile"
    JWT_EXPIRES_MINUTES: int = 60 * 24 * 7

    TOKEN_ENCRYPTION_KEY: str

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    LLM_PROVIDER: str = "none"  # none | openai_chat_completions
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4.0"

    SYNC_LOOKBACK_DAYS: int = 90
    SYNC_DEBUG_WIDE_QUERY: bool = False
    GMAIL_QUERY_BASE: str = (
        '(receipt OR invoice OR "payment received" OR "subscription" OR "renewal" OR "trial" OR "order confirmation") "$"'
    )
    GMAIL_EXCLUDED_CATEGORIES: str = "promotions social"
    GMAIL_QUERY: str = ""

    def model_post_init(self, __context) -> None:
        query_base = self.GMAIL_QUERY_BASE
        excluded_categories = ""
        if self.SYNC_DEBUG_WIDE_QUERY:
            query_base = query_base.replace('"$"', "").replace("$", "")
        else:
            excluded_categories = " ".join(
                f"-category:{category}"
                for category in self.GMAIL_EXCLUDED_CATEGORIES.split()
                if category
            )
        self.GMAIL_QUERY = f"{query_base} {excluded_categories}".strip()

settings = Settings()
