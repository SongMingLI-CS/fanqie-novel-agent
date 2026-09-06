"""Runtime configuration for the novel agent.

Values are read from ``os.environ`` at *instantiation* time (not at import
time), so entry points can load ``.env`` first and then build a ``Config``
that sees those values, while library consumers keep the process env intact.

Malformed numeric/boolean variables raise :class:`ConfigError` with a clear
message instead of silently falling back, so misconfiguration fails loudly at
startup rather than producing surprising behaviour mid-run.
"""
import os
from pathlib import Path


class ConfigError(ValueError):
    """Raised when an environment variable cannot be parsed into its config type."""


def _env_str(name, default):
    value = os.getenv(name)
    return default if value in (None, "") else value


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _env_int(name, default, lo=None, hi=None):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


class Config:
    """Immutable-by-convention configuration snapshot.

    All settings are captured from the environment when the instance is built.
    Keyword overrides win over the environment, which is what tests use to pin
    a single value without touching the process environment.
    """

    def __init__(self, **overrides):
        data_dir = Path(_env_str("NOVEL_DATA_DIR", "./data"))
        base_url = _env_str("DEEPSEEK_BASE_URL", "")
        ca_bundle = _env_str("DEEPSEEK_CA_BUNDLE", "")
        model = _env_str("DEEPSEEK_MODEL", "")
        thinking = _env_str("DEEPSEEK_THINKING", "disabled")
        reasoning_effort = _env_str("DEEPSEEK_REASONING_EFFORT", "low")
        max_tokens = _env_int("NOVEL_MAX_CHAPTER_TOKENS", 6000, lo=256)
        max_retries = _env_int("NOVEL_MAX_RETRIES", 2, lo=0, hi=10)
        connect_timeout = _env_int("DEEPSEEK_CONNECT_TIMEOUT", 10, lo=1)
        timeout = _env_int("NOVEL_REQUEST_TIMEOUT", 180, lo=180)
        job_timeout = _env_int("NOVEL_JOB_TIMEOUT", 900, lo=30)
        worker_once = _env_bool("NOVEL_WORKER_ONCE", False)
        worker_concurrency = _env_int("NOVEL_WORKER_CONCURRENCY", 1, lo=1)
        max_job_attempts = _env_int("NOVEL_MAX_JOB_ATTEMPTS", 3, lo=1)
        auth_token = _env_str("NOVEL_AUTH_TOKEN", "")
        publish_enabled = _env_bool("NOVEL_PUBLISH_ENABLED", False)
        require_review = _env_bool("NOVEL_REQUIRE_REVIEW", True)
        auto_export_txt = _env_bool("NOVEL_AUTO_EXPORT_TXT", True)
        host = _env_str("NOVEL_HOST", "127.0.0.1")
        port = _env_int("NOVEL_PORT", 8787, lo=0, hi=65535)
        log_level = _env_str("NOVEL_LOG_LEVEL", "INFO").upper()
        log_format = _env_str("NOVEL_LOG_FORMAT", "text").lower()
        static_dir = _env_str("NOVEL_STATIC_DIR", "")

        # Thinking mode is a model capability switch, not a free-form string.
        if thinking not in ("enabled", "disabled"):
            raise ConfigError("DEEPSEEK_THINKING must be 'enabled' or 'disabled'")
        if log_format not in ("text", "json"):
            raise ConfigError("NOVEL_LOG_FORMAT must be 'text' or 'json'")
        valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if log_level not in valid_levels:
            raise ConfigError(f"NOVEL_LOG_LEVEL must be one of {sorted(valid_levels)}")

        self.data_dir = data_dir
        self.base_url = base_url
        self.ca_bundle = ca_bundle
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.timeout = timeout
        self.job_timeout = job_timeout
        self.worker_once = worker_once
        self.worker_concurrency = worker_concurrency
        self.max_job_attempts = max_job_attempts
        self.auth_token = auth_token
        self.publish_enabled = publish_enabled
        self.require_review = require_review
        self.auto_export_txt = auto_export_txt
        self.host = host
        self.port = port
        self.log_level = log_level
        self.log_format = log_format
        self.static_dir = static_dir

        # Keyword overrides (used by tests) always win.
        for key, value in overrides.items():
            setattr(self, key, value)

    def __repr__(self):
        return (
            f"Config(host={self.host}, port={self.port}, model={self.model or '<unset>'}, "
            f"data_dir={self.data_dir}, auth={'on' if self.auth_token else 'off'})"
        )
