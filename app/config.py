"""Central settings. Secrets come from env only; nothing dataset-specific here."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llama_cloud_api_key: str = ""
    litellm_model: str = ""          # legacy path (unused if bedrock key set)
    openai_base_url: str = ""
    openai_api_key: str = ""
    # Bedrock mantle path (primary): OpenAI-compatible /v1/chat/completions
    # with a Bedrock API key. Model is env-switchable (Luna = one-line swap
    # once the account is enabled for it).
    bedrock_api_key: str = ""        # AWS_BEARER_TOKEN_BEDROCK also accepted
    bedrock_region: str = "us-east-1"
    bedrock_model: str = "zai.glm-4.7-flash"
    bedrock_max_link_calls: int = 80  # per-run cap on LLM-as-judge link calls

    data_dir: str = "data"           # sqlite + cache + crops (gitignored, rebuilt)
    max_chunk_chars: int = 4000
    link_tolerance: float = 0.02     # numeric corroboration tolerance (relative)
    fuzzy_threshold: int = 82        # rapidfuzz token_set_ratio for blocking

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
