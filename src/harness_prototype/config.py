"""Validated local settings: environment overrides project .env, then defaults."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

# Anchor to this checkout, not the directory being searched by the harness.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    """Validated connection and generation settings for one application startup."""

    host: str
    model: str
    temperature: float
    context_length: int
    max_tool_rounds: int


def load_settings(
    env_file: Path | None = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load an optional UTF-8 dotenv file, apply environment overrides, and validate."""
    values = dict(dotenv_values(env_file, encoding="utf-8", interpolate=False)) if env_file else {}
    values.update(os.environ if environ is None else environ)

    def value(name: str, default: str) -> str:
        """Read a nonempty setting without including its value in error messages."""
        raw = values.get(name, default)
        if raw is None or not raw.strip():
            raise ValueError(f"{name} must not be empty.")
        return raw.strip()

    def positive_integer(name: str, default: str) -> int:
        """Parse positive whole-number settings with actionable validation errors."""
        try:
            number = int(value(name, default))
        except ValueError:
            raise ValueError(f"{name} must be a positive integer.") from None
        if number < 1:
            raise ValueError(f"{name} must be a positive integer.")
        return number

    host = value("HARNESS_OLLAMA_HOST", "http://127.0.0.1:11434")
    try:
        url = urlsplit(host)
        valid_host = (
            url.scheme in {"http", "https"} and url.hostname and
            url.path in {"", "/"} and not url.query and not url.fragment and
            url.username is None and url.password is None and
            not any(character.isspace() for character in host)
        )
        port = url.port
        if not valid_host or (port is not None and port < 1):
            raise ValueError
    except ValueError:
        raise ValueError("HARNESS_OLLAMA_HOST must be an HTTP(S) base URL without credentials or API paths.") from None

    try:
        temperature = float(value("HARNESS_TEMPERATURE", "0.15"))
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError
    except ValueError:
        raise ValueError("HARNESS_TEMPERATURE must be a number between 0 and 2.") from None

    return Settings(
        host=host,
        model=value("HARNESS_MODEL", "devstral-small-2:latest"),
        temperature=temperature,
        context_length=positive_integer("HARNESS_CONTEXT_LENGTH", "4096"),
        max_tool_rounds=positive_integer("HARNESS_MAX_TOOL_ROUNDS", "8"),
    )


_settings = load_settings()
HOST = _settings.host
MODEL = _settings.model
TEMPERATURE = _settings.temperature
CONTEXT_LENGTH = _settings.context_length
MAX_TOOL_ROUNDS = _settings.max_tool_rounds
