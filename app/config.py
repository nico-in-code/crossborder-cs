from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: str = "dev"
    app_base_url: str = "http://127.0.0.1:8000"
    database_url: str = ""
    test_database_url: str = ""
    mcp_database_url: str = ""
    session_secret: str = "dev-session-secret"
    mcp_context_secret: str = "dev-mcp-context-secret"
    workers_enabled: bool = True
    seed_password: str = "DevPass#2026"
    test_channel_secret: str = "local-channel-secret-change-me"

    mcp_tool_timeout_seconds: float = 10.0
    agent_total_timeout_seconds: float = 60.0
    agent_max_steps: int = 6

    llm_provider: str = "fake"
    translation_provider: str = "fake"
    embedding_provider: str = "hash"
    reranker_provider: str = "lexical"

    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    deepl_api_key: str = ""

    integration_enabled_store_ids: str = ""

    amazon_sp_api_enabled: bool = False
    amazon_lwa_client_id: str = ""
    amazon_lwa_client_secret: str = ""
    amazon_refresh_token: str = ""
    amazon_region: str = "us-east-1"
    amazon_endpoint: str = "https://sellingpartnerapi-fe.amazon.com"
    amazon_lwa_endpoint: str = "https://api.amazon.com/auth/o2/token"
    amazon_marketplace_ids: str = "A1VC38T7YXB528"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    track17_enabled: bool = False
    track17_api_key: str = ""
    track17_base_url: str = "https://api.17track.net/track/v2.2"

    erp_enabled: bool = False
    erp_base_url: str = ""
    erp_api_key: str = ""

    channel_enabled: bool = False
    channel_base_url: str = ""
    channel_api_key: str = ""
    channel_timeout_seconds: float = 10.0

    translation_enabled: bool = False
    translation_base_url: str = ""
    translation_api_key: str = ""

    attachment_root: str = "data/attachments"
    attachment_max_bytes: int = 10 * 1024 * 1024

    milvus_enabled: bool = False
    milvus_uri: str = ""
    milvus_token: str = ""
    ocr_provider: str = "fake"
    ocr_base_url: str = ""
    ocr_api_key: str = ""

    remote_ssh_host: str = "36.139.119.132"
    remote_ssh_port: int = 22
    remote_ssh_user: str = "root"
    remote_pg_port: int = 55432
    remote_pg_remote_port: int = 5433
    remote_ssh_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()