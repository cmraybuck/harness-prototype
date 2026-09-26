"""Reusable policy decisions for model-requested tool operations."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .tool_results import tool_error


class ToolCategory(StrEnum):
    """Group tools that share an approval risk profile."""

    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTION = "execution"
    NETWORK = "network"
    DEPLOYMENT = "deployment"


class ApprovalPolicy(StrEnum):
    """Describe how a tool category is authorized before dispatch."""

    ALWAYS_ALLOW = "always_allow"
    ASK_FIRST = "ask_first"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRequest:
    """Identify the exact proposed operation presented for approval."""

    tool_name: str
    arguments: Any
    category: ToolCategory

    def operation(self) -> dict[str, Any]:
        """Return a loggable representation of the requested operation."""
        return {"tool_name": self.tool_name, "arguments": self.arguments}


@dataclass(frozen=True)
class ApprovalDecision:
    """Capture one policy outcome for logging and dispatch control."""

    request: ApprovalRequest
    policy: ApprovalPolicy
    allowed: bool
    source: str

    def log_fields(self) -> dict[str, Any]:
        """Return stable fields for an interaction-log approval event."""
        return {
            "category": self.request.category.value,
            "policy": self.policy.value,
            "decision": "approved" if self.allowed else "denied",
            "source": self.source,
            "operation": self.request.operation(),
        }

    def denial_result(self) -> dict:
        """Return a recoverable model-facing denial without dispatching the tool."""
        return tool_error(
            "Tool operation denied by approval policy. Do not retry unless the user changes the policy or request.",
            status="approval_denied",
            category=self.request.category.value,
            policy=self.policy.value,
            operation=self.request.operation(),
        )


ApprovalPrompt = Callable[[ApprovalRequest], bool]


def deny_approval_by_default(request: ApprovalRequest) -> bool:
    """Fail closed when an ask-first policy has no interactive prompt."""
    return False


def decide_tool_approval(
    request: ApprovalRequest,
    policies: Mapping[ToolCategory, ApprovalPolicy],
    prompt: ApprovalPrompt = deny_approval_by_default,
) -> ApprovalDecision:
    """Apply the configured category policy to one exact tool operation."""
    policy = policies[request.category]
    if policy == ApprovalPolicy.ALWAYS_ALLOW:
        return ApprovalDecision(request, policy, True, "policy")
    if policy == ApprovalPolicy.DENY:
        return ApprovalDecision(request, policy, False, "policy")
    return ApprovalDecision(request, policy, bool(prompt(request)), "user")
