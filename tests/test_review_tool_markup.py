from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory import config, dispatch

APPROVE_TALLY = (
    "```json\n"
    + json.dumps({"verdict": "APPROVE", "findings": []})
    + "\n```\nVERDICT: APPROVE"
)

MARKUP = (
    '<invoke name="Bash">\n'
    '<parameter name="command">cat ./review.diff</parameter>\n'
    "</invoke>\n"
)

NAMESPACE = "antml" + ":"
NAMESPACED_MARKUP = (
    "<" + NAMESPACE + 'invoke name="Bash">\n'
    "<" + NAMESPACE + 'parameter name="command">ls</' + NAMESPACE + "parameter>\n"
    "</" + NAMESPACE + "invoke>\n"
)


def parse(findings: str):
    return dispatch._parse_review_output(findings, 5)


class ReviewPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dispatch.configure(config.Config(root, "acme/widgets"))

    def assert_tool_free(self, prompt: str) -> None:
        self.assertNotIn("./snapshot", prompt)
        self.assertNotIn("./review.diff", prompt)
        self.assertIn("no file or shell access", prompt)
        self.assertIn("no network access", prompt)
        self.assertIn("tool-call syntax", prompt)
        self.assertIn("claim to have inspected files or run commands", prompt)

    def test_ticket_prompt_is_tool_free(self) -> None:
        self.assert_tool_free(dispatch.review_prompt(7, "PASS"))

    def test_learning_prompt_is_tool_free(self) -> None:
        self.assert_tool_free(
            dispatch.review_prompt(None, "PASS", proposal="Add a rule about widgets."),
        )


class ToolCallMarkupTest(unittest.TestCase):
    def test_bare_markup_is_rejected_even_with_valid_approve_tally(self) -> None:
        _, verdict, parsed, _, reason = parse(MARKUP + "\n" + APPROVE_TALLY)
        self.assertEqual((verdict, parsed, reason), ("REVISE", False, "tool_call_markup"))

    def test_namespaced_markup_is_rejected(self) -> None:
        _, verdict, parsed, _, reason = parse(NAMESPACED_MARKUP + "\n" + APPROVE_TALLY)
        self.assertEqual((verdict, parsed, reason), ("REVISE", False, "tool_call_markup"))

    def test_markup_inside_backtick_fence_is_allowed(self) -> None:
        findings = "```text\n" + MARKUP + "```\n\n" + APPROVE_TALLY
        _, verdict, parsed, _, reason = parse(findings)
        self.assertEqual((verdict, parsed, reason), ("APPROVE", True, None))

    def test_markup_inside_tilde_fence_is_allowed(self) -> None:
        findings = "~~~text\n" + MARKUP + "~~~\n\n" + APPROVE_TALLY
        _, verdict, parsed, _, reason = parse(findings)
        self.assertEqual((verdict, parsed, reason), ("APPROVE", True, None))

    def test_markup_inside_inline_code_is_allowed(self) -> None:
        findings = "Reviewer note: `<invoke name=\"Bash\">` is not a call.\n\n" + APPROVE_TALLY
        _, verdict, parsed, _, reason = parse(findings)
        self.assertEqual((verdict, parsed, reason), ("APPROVE", True, None))

    def test_unclosed_fence_does_not_hide_markup(self) -> None:
        findings = "```text\n" + MARKUP + "VERDICT: APPROVE"
        _, verdict, parsed, _, reason = parse(findings)
        self.assertEqual((verdict, parsed, reason), ("REVISE", False, "tool_call_markup"))


if __name__ == "__main__":
    unittest.main()
