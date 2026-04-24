"""U1 tests for proactive context-management trigger detection.

These tests exercise the dump-trigger predicate, the layer-aware context-limit
lookup, and the --context-limit CLI persistence path. No oMLX server or
network calls are involved; everything runs in-process against the public
helpers exported from omlx_agent.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Make the repo root importable regardless of where the tests are run from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class ShouldDumpTests(unittest.TestCase):
    def setUp(self):
        # Pin a deterministic model + limit for the trigger math.
        self._orig_limits = dict(omlx_agent.MODEL_CONTEXT_LIMITS)
        omlx_agent.MODEL_CONTEXT_LIMITS["test-model"] = 1000

    def tearDown(self):
        omlx_agent.MODEL_CONTEXT_LIMITS.clear()
        omlx_agent.MODEL_CONTEXT_LIMITS.update(self._orig_limits)

    def _msgs_clean_assistant(self, n_user: int = 1):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(n_user):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        return msgs

    def test_fires_at_threshold_on_clean_boundary(self):
        msgs = self._msgs_clean_assistant(n_user=2)
        # 60% of 1000 = 600; pre-flight overhead = 5*64 + 4096 = 4416 -> total
        # 5016/1000 = 502% which exceeds the hard ceiling, so use a much higher
        # limit so the pre-flight gate passes.
        omlx_agent.MODEL_CONTEXT_LIMITS["test-model"] = 100_000
        # 60% = 60_000; overhead = 5*64 + 4096 = 4416; sum 64_416 / 100_000 = 64% < 90% ceiling.
        self.assertTrue(
            omlx_agent._should_dump("work", 60_000, msgs, "test-model"),
            "expected dump to fire at 60% threshold on a clean assistant boundary",
        )

    def test_blocked_by_orphaned_tool_calls(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "x", "arguments": "{}"}}],
            },
            # Note: matching tool response NOT yet appended.
        ]
        omlx_agent.MODEL_CONTEXT_LIMITS["test-model"] = 100_000
        self.assertFalse(
            omlx_agent._should_dump("work", 80_000, msgs, "test-model"),
            "should refuse to dump while a tool_calls turn is unresolved",
        )

    def test_blocked_when_prompt_tokens_unknown(self):
        msgs = self._msgs_clean_assistant()
        self.assertFalse(omlx_agent._should_dump("work", 0, msgs, "test-model"))
        self.assertFalse(omlx_agent._should_dump("work", None, msgs, "test-model"))  # type: ignore[arg-type]

    def test_blocked_by_preflight_ceiling(self):
        msgs = self._msgs_clean_assistant()
        # limit=1000, prompt_tokens=900, overhead minimum 4416 -> way past 90%.
        self.assertFalse(omlx_agent._should_dump("work", 900, msgs, "test-model"))

    def test_unmapped_model_uses_default_limit(self):
        # Default limit is 16384. 60% = 9830. Use a tiny message list so
        # overhead is small enough to clear the 90% ceiling: 3*64+4096=4288;
        # 9830+4288=14118 / 16384 = 86% < 90%. Should fire.
        msgs = self._msgs_clean_assistant(n_user=1)
        self.assertEqual(
            omlx_agent.get_model_context_limit("totally-new-model"),
            omlx_agent.DEFAULT_CONTEXT_LIMIT,
        )
        self.assertTrue(
            omlx_agent._should_dump("work", 9900, msgs, "totally-new-model"),
            "unmapped model should fall back to DEFAULT_CONTEXT_LIMIT and still trigger",
        )


class ContextLimitArgTests(unittest.TestCase):
    def test_parses_valid_arg(self):
        model, limit = omlx_agent._parse_context_limit_arg("gpt-foo=32000")
        self.assertEqual(model, "gpt-foo")
        self.assertEqual(limit, 32000)

    def test_rejects_missing_equals(self):
        with self.assertRaises(ValueError):
            omlx_agent._parse_context_limit_arg("gpt-foo")

    def test_rejects_empty_model(self):
        with self.assertRaises(ValueError):
            omlx_agent._parse_context_limit_arg("=32000")

    def test_rejects_non_int_limit(self):
        with self.assertRaises(ValueError):
            omlx_agent._parse_context_limit_arg("gpt-foo=lots")

    def test_rejects_zero_or_negative(self):
        with self.assertRaises(ValueError):
            omlx_agent._parse_context_limit_arg("gpt-foo=0")
        with self.assertRaises(ValueError):
            omlx_agent._parse_context_limit_arg("gpt-foo=-5")


class ContextLimitPersistenceTests(unittest.TestCase):
    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "model_context_limits.json")
            with mock.patch.object(omlx_agent, "MODEL_CONTEXT_LIMITS_FILE", path):
                # Snapshot + reset in-memory state for a clean baseline.
                orig = dict(omlx_agent.MODEL_CONTEXT_LIMITS)
                omlx_agent.MODEL_CONTEXT_LIMITS.clear()
                try:
                    omlx_agent._save_model_context_override("gpt-foo", 32000)
                    self.assertEqual(omlx_agent.MODEL_CONTEXT_LIMITS["gpt-foo"], 32000)

                    # File on disk should reflect the override.
                    with open(path) as f:
                        on_disk = json.load(f)
                    self.assertEqual(on_disk, {"gpt-foo": 32000})

                    # Reset memory and reload from disk.
                    omlx_agent.MODEL_CONTEXT_LIMITS.clear()
                    omlx_agent._load_model_context_overrides()
                    self.assertEqual(omlx_agent.MODEL_CONTEXT_LIMITS["gpt-foo"], 32000)
                finally:
                    omlx_agent.MODEL_CONTEXT_LIMITS.clear()
                    omlx_agent.MODEL_CONTEXT_LIMITS.update(orig)


class CleanBoundaryTests(unittest.TestCase):
    def test_user_last_is_clean(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.assertTrue(omlx_agent._last_message_is_clean_boundary(msgs))

    def test_assistant_no_tool_calls_is_clean(self):
        msgs = [{"role": "assistant", "content": "hi"}]
        self.assertTrue(omlx_agent._last_message_is_clean_boundary(msgs))

    def test_assistant_with_unmatched_tool_calls_is_dirty(self):
        msgs = [{"role": "assistant", "content": None,
                 "tool_calls": [{"id": "1", "type": "function",
                                 "function": {"name": "n", "arguments": "{}"}}]}]
        self.assertFalse(omlx_agent._last_message_is_clean_boundary(msgs))

    def test_tool_response_with_all_ids_matched_is_clean(self):
        msgs = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "n", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
        ]
        self.assertTrue(omlx_agent._last_message_is_clean_boundary(msgs))

    def test_tool_response_with_missing_pair_is_dirty(self):
        msgs = [
            {"role": "assistant", "content": None,
             "tool_calls": [
                 {"id": "1", "type": "function",
                  "function": {"name": "n", "arguments": "{}"}},
                 {"id": "2", "type": "function",
                  "function": {"name": "n", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
        ]
        self.assertFalse(omlx_agent._last_message_is_clean_boundary(msgs))

    def test_empty_is_dirty(self):
        self.assertFalse(omlx_agent._last_message_is_clean_boundary([]))


if __name__ == "__main__":
    unittest.main()
