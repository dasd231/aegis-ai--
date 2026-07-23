from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import BACKEND_DIR, get_settings


PROMPT_DIR = BACKEND_DIR / "prompts"


@dataclass(frozen=True)
class PromptSpec:
    version: str
    content: str
    sha256: str


class PromptRegistry:
    """Fail-closed file registry for reviewed, immutable prompt versions."""

    def __init__(self, root: Path = PROMPT_DIR):
        self.root = root.resolve()

    def get(self, version: str) -> PromptSpec:
        if not version or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in version):
            raise ValueError("invalid prompt version")
        path = (self.root / f"{version}.md").resolve()
        if path.parent != self.root or not path.is_file():
            raise LookupError(f"unknown prompt version: {version}")
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"prompt version is empty: {version}")
        return PromptSpec(
            version=version,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def validate_configured_versions(self) -> None:
        settings = get_settings()
        self.get(settings.prompt_stable_version)
        self.get(settings.prompt_canary_version)


@lru_cache(maxsize=1)
def get_prompt_registry() -> PromptRegistry:
    registry = PromptRegistry()
    registry.validate_configured_versions()
    return registry
