from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables / .env."""

    app_name: str = "Bid Inspection"
    environment: str = "development"
    api_prefix: str = "/api/v1"
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 12 * 60
    cors_origins: str = "http://localhost:8080,http://localhost:5173"

    database_url: str = "postgresql+psycopg2://bid:bid@db:5432/bid_inspection"
    redis_url: str = "redis://redis:6379/0"

    storage_backend: str = "local"
    storage_root: str = "/data/storage"

    max_upload_size_mb: int = 500
    max_companies_per_project: int = 20
    allowed_file_extensions: str = "pdf,docx,doc,txt,md,wps"

    # Run background jobs inline (useful for local development / tests).
    sync_tasks: bool = False
    seed_demo: bool = False
    # 开发模式下允许 Worker 重启后重新执行停留在 running 的任务（生产环境保持 false）
    allow_rerun_running_tasks: bool = False
    # 开发模式下自动为已有表补齐新增列（仅 ADD COLUMN），改表结构后无需重建数据库
    auto_sync_schema: bool = False

    admin_username: str = "admin"
    admin_password: str = "admin123"

    # Analysis defaults. These are rule thresholds, not model parameters.
    text_similarity_threshold: float = 0.6
    text_min_match_chars: int = 40
    text_min_block_chars: int = 50
    # 文本分块：目标 300～500 字，最大 800 字，低于 50 字合并或过滤
    text_chunk_target_min: int = 300
    text_chunk_target_max: int = 500
    text_chunk_max_chars: int = 800
    text_chunk_min_chars: int = 50
    # 字符级 3-gram，不依赖中文分词
    text_shingle_size: int = 3
    # MinHash / LSH
    text_minhash_num_perm: int = 128
    text_lsh_threshold: float = 0.5
    max_blocks_per_company: int = 3000
    max_candidate_pairs: int = 20000
    max_matches_per_pair: int = 200

    price_cv_threshold: float = 0.05
    price_close_deviation: float = 0.01
    price_sequence_tolerance: float = 0.005

    algorithm_version: str = "v1.0.0-minhash-lsh"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_extensions(self) -> set[str]:
        return {ext.strip().lower().lstrip(".") for ext in self.allowed_file_extensions.split(",")}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
