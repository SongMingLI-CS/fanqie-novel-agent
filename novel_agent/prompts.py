"""Centralised prompt management for the novel-writing agent.

Prompt text lives in Markdown files under the repository ``prompts/`` directory
(one file per stage/role), separated from the pipeline code. Templates use the
``<<name>>`` placeholder syntax (chosen over ``str.format`` because the prose
contains literal braces from JSON examples).

``PromptManager`` loads each file lazily, validates that every placeholder is
satisfied before rendering, and stamps a ``prompt_version`` that is written to
the ``usage`` table so every model call is traceable to the exact prompt text it
used.
"""
import re
from pathlib import Path

# Matches <<name>> placeholders; names are [A-Za-z0-9_]+ only.
_PLACEHOLDER = re.compile(r"<<(\w+)>>")

VERSION = "novel-writer@2"


class PromptError(ValueError):
    """Raised when a prompt file is missing or a placeholder is unsatisfied."""


class PromptManager:
    """Loads and renders prompt templates from the ``prompts/`` directory."""

    def __init__(self, root=None):
        # Default to the repository root (which also holds .agents/skills).
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.dir = self.root / "prompts"
        self._cache = {}

    @property
    def version(self):
        return VERSION

    def _load(self, name):
        if name not in self._cache:
            path = self.dir / f"{name}.md"
            if not path.is_file():
                raise PromptError(f"prompt file missing: {path}")
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def required_variables(self, name):
        """Return the sorted placeholder names a template expects."""
        return sorted({m for m in _PLACEHOLDER.findall(self._load(name))})

    def render(self, name, mapping=None):
        """Render ``name`` with ``mapping``, failing loudly on missing keys."""
        template = self._load(name)
        mapping = mapping or {}
        placeholders = _PLACEHOLDER.findall(template)
        missing = sorted({p for p in placeholders if p not in mapping})
        if missing:
            raise PromptError(
                f"prompt '{name}' is missing variables: {', '.join(missing)}"
            )
        return _PLACEHOLDER.sub(lambda m: str(mapping[m.group(1)]), template)

    def system(self):
        """The shared system role for every model call."""
        return self.render("system")
