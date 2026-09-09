"""Central settings. Secrets come from env only; nothing dataset-specific here."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llama_cloud_api_key: str = ""
    litellm_model: str = ""          # e.g. "openrouter/anthropic/claude-..." — provided later
    openai_base_url: str = ""
    openai_api_key: str = ""

    tier: str = "agentic"            # LlamaParse tier for complex pages
    data_dir: str = "data"           # sqlite + cache + crops (gitignored, rebuilt)
    max_chunk_chars: int = 4000
    link_tolerance: float = 0.02     # numeric corroboration tolerance (relative)
    fuzzy_threshold: int = 82        # rapidfuzz token_set_ratio for blocking

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
