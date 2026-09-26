"""Turn-scoped protection against repetitive or repeatedly failing tool use."""

from dataclasses import dataclass, field
import json
from typing import Any

from .tool_results import tool_error


@dataclass(frozen=True)
class ToolLoopStop:
    """Describe why further tool execution should stop for the current turn."""

    reason: str
    threshold: int
    guidance: str
    operation: dict[str, Any] | None = None

    def fields(self) -> dict[str, Any]:
        """Return structured stop details for results and interaction logs."""
        details: dict[str, Any] = {
            "stop_reason": self.reason,
            "threshold": self.threshold,
            "guidance": self.guidance,
        }
        if self.operation is not None:
            details["operation"] = self.operation
        return details

    def result(self) -> dict:
        """Return model-facing recovery guidance as a failed tool result."""
        return tool_error(
            "Tool execution stopped to prevent an unproductive loop.",
            status="tool_loop_stopped",
            **self.fields(),
        )

    def notice(self) -> str:
        """Return a concise user-facing explanation of the stopped turn."""
        if self.reason == "identical_call_limit":
            return (
                "Tool execution stopped after the same operation was requested "
                f"more than {self.threshold} times. Completed results are retained; "
                "continue with different arguments or a narrower request."
            )
        return (
            f"Tool execution stopped after {self.threshold} consecutive failures. "
            "Completed results are retained; review the errors and continue with "
            "corrected arguments or a different approach."
        )


@dataclass
class ToolLoopGuard:
    """Track repeated calls and consecutive failures within one user turn."""

    identical_call_limit: int
    consecutive_failure_limit: int
    call_counts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0

    def before_call(self, name: str, arguments: Any) -> ToolLoopStop | None:
        """Block a call only after its exact operation reached the execution limit."""
        operation = {"tool_name": name, "arguments": arguments}
        signature = json.dumps(operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        count = self.call_counts.get(signature, 0)
        if count >= self.identical_call_limit:
            return ToolLoopStop(
                reason="identical_call_limit",
                threshold=self.identical_call_limit,
                guidance="Use different arguments, advance pagination, or ask the user how to proceed.",
                operation=operation,
            )
        self.call_counts[signature] = count + 1
        return None

    def after_result(self, result: object) -> ToolLoopStop | None:
        """Stop at the configured consecutive-failure threshold and reset on success."""
        failed = isinstance(result, dict) and result.get("ok") is False
        if not failed:
            self.consecutive_failures = 0
            return None
        self.consecutive_failures += 1
        if self.consecutive_failures < self.consecutive_failure_limit:
            return None
        return ToolLoopStop(
            reason="consecutive_failure_limit",
            threshold=self.consecutive_failure_limit,
            guidance="Review the recorded errors and retry with corrected arguments or a different approach.",
        )
