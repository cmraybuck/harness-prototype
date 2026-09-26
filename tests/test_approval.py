"""Approval policy checks independent of tool side effects."""

import unittest

from harness_prototype.approval import (
    ApprovalPolicy,
    ApprovalPromptResult,
    ApprovalRequest,
    ToolCategory,
    decide_tool_approval,
)


class ApprovalPolicyTests(unittest.TestCase):
    """Cover every policy outcome and supported risk category."""

    def test_always_allow_does_not_prompt(self) -> None:
        """Approve an always-allow operation entirely from configuration."""
        request = ApprovalRequest("read", {"path": "notes.txt"}, ToolCategory.READ_ONLY)

        def unexpected_prompt(proposed: ApprovalRequest) -> bool:
            """Fail if an always-allow policy attempts user interaction."""
            self.fail(f"Unexpected prompt for {proposed.tool_name}")

        decision = decide_tool_approval(
            request,
            {ToolCategory.READ_ONLY: ApprovalPolicy.ALWAYS_ALLOW},
            unexpected_prompt,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source, "policy")

    def test_ask_first_uses_the_exact_operation(self) -> None:
        """Present tool name and arguments before honoring an approval response."""
        request = ApprovalRequest("write", {"path": "a.txt", "text": "hello"}, ToolCategory.WRITE)
        seen = []
        decision = decide_tool_approval(
            request,
            {ToolCategory.WRITE: ApprovalPolicy.ASK_FIRST},
            lambda proposed: seen.append(proposed) or True,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(seen, [request])
        self.assertEqual(decision.log_fields()["operation"], request.operation())

    def test_ask_first_records_a_session_grant_source(self) -> None:
        """Preserve the audit source when an existing scoped grant approves a call."""
        request = ApprovalRequest("write", {"path": "a.txt"}, ToolCategory.WRITE)
        decision = decide_tool_approval(
            request,
            {ToolCategory.WRITE: ApprovalPolicy.ASK_FIRST},
            lambda proposed: ApprovalPromptResult(True, "session_file_grant"),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source, "session_file_grant")

    def test_ask_first_denial_is_recoverable(self) -> None:
        """Return a structured refusal for a declined operation."""
        request = ApprovalRequest("run", {"command": "build"}, ToolCategory.EXECUTION)
        decision = decide_tool_approval(
            request,
            {ToolCategory.EXECUTION: ApprovalPolicy.ASK_FIRST},
            lambda proposed: False,
        )
        result = decision.denial_result()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(result["operation"], request.operation())

    def test_ask_first_without_a_prompt_fails_closed(self) -> None:
        """Deny ask-first operations when no approval UI is available."""
        request = ApprovalRequest("deploy", {"target": "production"}, ToolCategory.DEPLOYMENT)
        decision = decide_tool_approval(
            request,
            {ToolCategory.DEPLOYMENT: ApprovalPolicy.ASK_FIRST},
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.source, "user")

    def test_deny_never_prompts_for_each_risk_category(self) -> None:
        """Deny every category from policy without consulting the user."""
        for category in ToolCategory:
            with self.subTest(category=category):
                request = ApprovalRequest("example", {}, category)

                def unexpected_prompt(proposed: ApprovalRequest) -> bool:
                    """Fail if a deny policy attempts user interaction."""
                    self.fail(f"Unexpected prompt for {proposed.tool_name}")

                decision = decide_tool_approval(
                    request,
                    {category: ApprovalPolicy.DENY},
                    unexpected_prompt,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.source, "policy")


if __name__ == "__main__":
    unittest.main()
