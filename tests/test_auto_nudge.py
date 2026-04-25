"""Regression tests for completion-aware CE narration nudging."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class AutoNudgeTests(unittest.TestCase):
    def test_completion_report_does_not_trigger_nudge(self):
        text = "The implementation is already complete and correct. All requirements have been fulfilled."
        self.assertFalse(omlx_agent._should_auto_nudge_for_narration(text, "work", 0))

    def test_forward_action_still_triggers_nudge(self):
        text = "Let me update the file now."
        self.assertTrue(omlx_agent._should_auto_nudge_for_narration(text, "work", 0))

    def test_completion_with_future_action_still_triggers_nudge(self):
        text = "The implementation is already complete, but let me verify the file one more time."
        self.assertTrue(omlx_agent._should_auto_nudge_for_narration(text, "work", 0))


if __name__ == "__main__":
    unittest.main()