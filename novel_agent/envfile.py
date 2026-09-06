"""Tiny pure-stdlib ``.env`` loader.

The runtime intentionally keeps zero third-party dependencies, so this replaces
python-dotenv with a small, well-tested subset: comments (``#``/``;``),
``export``-prefixed lines, optional single/double quotes and, crucially, *never
overriding* a variable that is already present in the process environment
(real injected env vars always win over the file).

Search order when ``path`` is not given:

1. ``NOVEL_ENV_FILE`` if set (lets operators/tests point at a specific file,
   or disable loading entirely by pointing at a non-existent file).
2. ``<cwd>/.env``
3. the repository ``.env`` next to the package.

Only the first file found is read.
"""
import os
from pathlib import Path


class EnvFileError(ValueError):
    """Raised for a structurally broken line in an env file."""


def default_env_path():
    explicit = os.getenv("NOVEL_ENV_FILE")
    if explicit:
        return Path(explicit)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env
    repo_env = Path(__file__).resolve().parents[1] / ".env"
    if repo_env.is_file():
        return repo_env
    return cwd_env


def _strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_line(line):
    """Return (key, value) for one env-file line, or None to skip it."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith(";"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    # Strip an unquoted trailing comment, but keep '#' when inside quotes.
    value = raw_value.strip()
    if value and value[0] in ('"', "'"):
        value = _strip_quotes(value)
    else:
        value = value.split(" #", 1)[0].split("\t#", 1)[0].rstrip()
    return key, value


def load_env(path=None, override=False):
    """Load ``path`` (or the auto-detected file) into ``os.environ``.

    Returns True when a file was read. Existing environment variables are kept
    unless ``override=True``.
    """
    env_path = Path(path) if path else default_env_path()
    if not env_path.is_file():
        return False
    parsed = []
    for lineno, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            pair = _parse_line(line)
        except ValueError as exc:  # pragma: no cover - defensive
            raise EnvFileError(f"{env_path}:{lineno}: {exc}") from exc
        if pair is not None:
            parsed.append(pair)
    for key, value in parsed:
        if override or key not in os.environ:
            os.environ[key] = value
    return True
