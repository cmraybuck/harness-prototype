"""Validated local settings: environment overrides project .env, then defaults."""

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .approval import ApprovalPolicy, ToolCategory

# Anchor to this checkout, not the directory being searched by the harness.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class CheckConfig:
    """Store one configured check command and its optional named target commands."""

    command: tuple[str, ...]
    targets: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Settings:
    """Validated connection and generation settings for one application startup."""

    host: str
    model: str
    models: tuple[str, ...]
    temperature: float
    context_length: int
    research_context_length: int
    max_tool_rounds: int
    inspection_tool_round_allowance: int
    identical_tool_call_limit: int
    consecutive_tool_failure_limit: int
    test_command: tuple[str, ...]
    targeted_test_command: tuple[str, ...]
    checks: dict[str, CheckConfig]
    test_timeout_seconds: int
    test_output_limit: int
    log_directory: Path
    log_redact_values: tuple[str, ...]
    approval_policies: dict[ToolCategory, ApprovalPolicy]


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

    def approval_policy(category: ToolCategory, default: ApprovalPolicy) -> ApprovalPolicy:
        """Parse one category policy without accepting an implicit fallback."""
        name = f"HARNESS_{category.value.upper()}_POLICY"
        raw_policy = value(name, default.value).casefold()
        try:
            return ApprovalPolicy(raw_policy)
        except ValueError:
            choices = ", ".join(policy.value for policy in ApprovalPolicy)
            raise ValueError(f"{name} must be one of: {choices}.") from None

    def command(name: str, default: str, *, targets: bool) -> tuple[str, ...]:
        """Parse a shell-free JSON argument array and validate its target placeholder."""
        try:
            arguments = json.loads(value(name, default))
        except json.JSONDecodeError:
            raise ValueError(f"{name} must be a JSON array of nonempty strings.") from None
        if (
            not isinstance(arguments, list)
            or not arguments
            or any(
                not isinstance(argument, str)
                or not argument.strip()
                or any(not character.isprintable() for character in argument)
                for argument in arguments
            )
        ):
            raise ValueError(f"{name} must be a JSON array of nonempty strings.")
        placeholder_count = arguments.count("{targets}")
        expected_count = 1 if targets else 0
        if placeholder_count != expected_count:
            requirement = "exactly one" if targets else "no"
            raise ValueError(f"{name} must contain {requirement} {{targets}} argument.")
        return tuple(arguments)

    def command_arguments(arguments: object, setting: str) -> tuple[str, ...]:
        """Validate one shell-free command array nested inside a setting."""
        if (
            not isinstance(arguments, list)
            or not arguments
            or any(
                not isinstance(argument, str)
                or not argument.strip()
                or any(not character.isprintable() for character in argument)
                for argument in arguments
            )
        ):
            raise ValueError(f"{setting} commands must be nonempty JSON arrays of strings.")
        return tuple(arguments)

    def configured_checks() -> dict[str, CheckConfig]:
        """Parse exact check and target names into shell-free command arrays."""
        setting = "HARNESS_CHECKS"
        try:
            raw_checks = json.loads(value(setting, "{}"))
        except json.JSONDecodeError:
            raise ValueError(f"{setting} must be a JSON object.") from None
        if not isinstance(raw_checks, dict):
            raise ValueError(f"{setting} must be a JSON object.")
        checks: dict[str, CheckConfig] = {}
        for check_name, definition in raw_checks.items():
            if (
                not isinstance(check_name, str)
                or not check_name.strip()
                or check_name != check_name.strip()
                or any(not character.isprintable() for character in check_name)
            ):
                raise ValueError(f"{setting} check names must be nonempty trimmed strings.")
            if not isinstance(definition, dict) or set(definition) - {"command", "targets"}:
                raise ValueError(
                    f"{setting}.{check_name} must contain only command and optional targets."
                )
            if "command" not in definition:
                raise ValueError(f"{setting}.{check_name}.command is required.")
            raw_targets = definition.get("targets", {})
            if not isinstance(raw_targets, dict):
                raise ValueError(f"{setting}.{check_name}.targets must be a JSON object.")
            targets: dict[str, tuple[str, ...]] = {}
            for target_name, target_command in raw_targets.items():
                if (
                    not isinstance(target_name, str)
                    or not target_name.strip()
                    or target_name != target_name.strip()
                    or any(not character.isprintable() for character in target_name)
                ):
                    raise ValueError(
                        f"{setting}.{check_name} target names must be nonempty trimmed strings."
                    )
                targets[target_name] = command_arguments(
                    target_command, f"{setting}.{check_name}.targets.{target_name}"
                )
            checks[check_name] = CheckConfig(
                command_arguments(definition["command"], f"{setting}.{check_name}.command"),
                targets,
            )
        return checks

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

    model = value("HARNESS_MODEL", "devstral-small-2:latest")
    configured_models = value(
        "HARNESS_MODELS",
        "devstral-small-2:latest,qwen3:8b",
    )
    models = tuple(dict.fromkeys(
        candidate.strip() for candidate in configured_models.split(",") if candidate.strip()
    ))
    if not models:
        raise ValueError("HARNESS_MODELS must contain at least one model identifier.")
    if model not in models:
        models = (model, *models)

    log_directory = Path(value("HARNESS_LOG_DIRECTORY", "work/logs")).expanduser()
    if not log_directory.is_absolute():
        log_directory = PROJECT_ROOT / log_directory
    redact_setting = values.get("HARNESS_LOG_REDACT_VARIABLES") or ""
    redact_names = tuple(
        dict.fromkeys(
            name.strip()
            for name in redact_setting.split(",")
            if name.strip()
        )
    )
    missing_redactions = [name for name in redact_names if not values.get(name)]
    if missing_redactions:
        raise ValueError(
            "HARNESS_LOG_REDACT_VARIABLES names unset or empty variables: "
            + ", ".join(missing_redactions)
        )

    approval_policies = {
        ToolCategory.READ_ONLY: approval_policy(
            ToolCategory.READ_ONLY,
            ApprovalPolicy.ALWAYS_ALLOW,
        ),
        ToolCategory.WRITE: approval_policy(ToolCategory.WRITE, ApprovalPolicy.ASK_FIRST),
        ToolCategory.EXECUTION: approval_policy(
            ToolCategory.EXECUTION,
            ApprovalPolicy.ASK_FIRST,
        ),
        ToolCategory.NETWORK: approval_policy(ToolCategory.NETWORK, ApprovalPolicy.ASK_FIRST),
        ToolCategory.DEPLOYMENT: approval_policy(
            ToolCategory.DEPLOYMENT,
            ApprovalPolicy.ASK_FIRST,
        ),
    }

    test_command = command(
        "HARNESS_TEST_COMMAND",
        '["uv","run","python","-m","unittest","discover","-s","tests","-v"]',
        targets=False,
    )
    targeted_test_command = command(
        "HARNESS_TARGETED_TEST_COMMAND",
        '["uv","run","python","-m","unittest","-v","{targets}"]',
        targets=True,
    )

    context_length = positive_integer("HARNESS_CONTEXT_LENGTH", "4096")
    research_context_length = positive_integer(
        "HARNESS_RESEARCH_CONTEXT_LENGTH",
        str(max(context_length, 32768)),
    )
    if research_context_length < context_length:
        raise ValueError(
            "HARNESS_RESEARCH_CONTEXT_LENGTH must be greater than or equal to "
            "HARNESS_CONTEXT_LENGTH."
        )

    return Settings(
        host=host,
        model=model,
        models=models,
        temperature=temperature,
        context_length=context_length,
        research_context_length=research_context_length,
        max_tool_rounds=positive_integer("HARNESS_MAX_TOOL_ROUNDS", "8"),
        inspection_tool_round_allowance=positive_integer(
            "HARNESS_INSPECTION_TOOL_ROUND_ALLOWANCE",
            "4",
        ),
        identical_tool_call_limit=positive_integer(
            "HARNESS_IDENTICAL_TOOL_CALL_LIMIT",
            "2",
        ),
        consecutive_tool_failure_limit=positive_integer(
            "HARNESS_CONSECUTIVE_TOOL_FAILURE_LIMIT",
            "3",
        ),
        test_command=test_command,
        targeted_test_command=targeted_test_command,
        checks=configured_checks(),
        test_timeout_seconds=positive_integer("HARNESS_TEST_TIMEOUT_SECONDS", "120"),
        test_output_limit=positive_integer("HARNESS_TEST_OUTPUT_LIMIT", "12000"),
        log_directory=log_directory.resolve(),
        log_redact_values=tuple(str(values[name]) for name in redact_names),
        approval_policies=approval_policies,
    )


_settings = load_settings()
HOST = _settings.host
MODEL = _settings.model
MODELS = _settings.models
TEMPERATURE = _settings.temperature
CONTEXT_LENGTH = _settings.context_length
RESEARCH_CONTEXT_LENGTH = _settings.research_context_length
MAX_TOOL_ROUNDS = _settings.max_tool_rounds
INSPECTION_TOOL_ROUND_ALLOWANCE = _settings.inspection_tool_round_allowance
IDENTICAL_TOOL_CALL_LIMIT = _settings.identical_tool_call_limit
CONSECUTIVE_TOOL_FAILURE_LIMIT = _settings.consecutive_tool_failure_limit
LOG_DIRECTORY = _settings.log_directory
LOG_REDACT_VALUES = _settings.log_redact_values
APPROVAL_POLICIES = _settings.approval_policies
TEST_COMMAND = _settings.test_command
TARGETED_TEST_COMMAND = _settings.targeted_test_command
CHECKS = _settings.checks
TEST_TIMEOUT_SECONDS = _settings.test_timeout_seconds
TEST_OUTPUT_LIMIT = _settings.test_output_limit
