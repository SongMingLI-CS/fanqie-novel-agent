import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path(os.getenv("NOVEL_DATA_DIR", "./data"))
    base_url: str = os.getenv("DEEPSEEK_BASE_URL", "")
    ca_bundle: str = os.getenv("DEEPSEEK_CA_BUNDLE", "")
    model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    max_tokens: int = int(os.getenv("NOVEL_MAX_CHAPTER_TOKENS", "6000"))
    max_retries: int = min(2, int(os.getenv("NOVEL_MAX_RETRIES", "2")))
    connect_timeout: int = int(os.getenv("DEEPSEEK_CONNECT_TIMEOUT", "10"))
    timeout: int = max(180, int(os.getenv("NOVEL_REQUEST_TIMEOUT", "180")))
    job_timeout: int = int(os.getenv("NOVEL_JOB_TIMEOUT", "900"))
    worker_once: bool = os.getenv("NOVEL_WORKER_ONCE", "false").lower() == "true"
    worker_concurrency: int = max(1, int(os.getenv("NOVEL_WORKER_CONCURRENCY", "1")))
    max_job_attempts: int = int(os.getenv("NOVEL_MAX_JOB_ATTEMPTS", "3"))
    auth_token: str = os.getenv("NOVEL_AUTH_TOKEN", "")
    publish_enabled: bool = os.getenv("NOVEL_PUBLISH_ENABLED", "false").lower() == "true"
    require_review: bool = os.getenv("NOVEL_REQUIRE_REVIEW", "true").lower() == "true"
