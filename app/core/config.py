from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Vaelan"
    database_url: str = "sqlite:///./vaelan.db"
    secret_key: str = "dev-secret-change-me"
    admin_email: str = "admin@vaelan.com"
    admin_password: str = "changeme"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
