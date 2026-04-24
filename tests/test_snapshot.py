"""U2 tests for snapshot generation, validation, tail slicing, and rebuild.

Exercises the snapshot building blocks without making real network calls.
``_request_snapshot`` is tested by injecting a fake ``api_call`` callable.
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


def _full_snapshot_text(overrides: dict[str, str] | None = None) -> str:
    """Return a snapshot body that satisfies _validate_snapshot."""
    sections = {
        "Active Task": "Implement context-management U2",
        "Current Step": "Writing the snapshot validator",
        "Decisions Made": "- Snapshot injected as system role",
        "Ruled Out / Don't Retry": "- Wiping system prompt (loses identity)",
        "Verified Facts": "- _should_dump returns True at 60% threshold",
        "Unverified Assumptions": "- Model will obey strict template rules",
        "Open Questions": "- Should manager get checkpoints? (resolved: yes)",
        "Files Touched": "- omlx_agent/omlx_agent.py",
        "Next Intended Action": "Add the safe tail slicer",
    }
    if overrides:
        sections.update(overrides)
    parts = ["CONTEXT_SNAPSHOT v1", ""]
    for field in omlx_agent.SNAPSHOT_REQUIRED_FIELDS:
        parts.append(f"## {field}")
        parts.append(sections[field])
        parts.append("")
    return "\n".join(parts)


class ValidateSnapshotTests(unittest.TestCase):
    def test_full_snapshot_validates(self):
        result = omlx_agent._validate_snapshot(_full_snapshot_text())
        for field in omlx_agent.SNAPSHOT_REQUIRED_FIELDS:
            self.assertIn(field, result)
            self.assertTrue(result[field])

    def test_none_marker_passes_validation(self):
        # The literal "(none)" is the supported empty-section signal.
        result = omlx_agent._validate_snapshot(
            _full_snapshot_text({"Ruled Out / Don't Retry": "(none)"})
        )
        self.assertEqual(result["Ruled Out / Don't Retry"], "(none)")

    def test_missing_ruled_out_field_raises(self):
        # Build a body without the "Ruled Out / Don't Retry" heading at all.
        sections = {f: "filled" for f in omlx_agent.SNAPSHOT_REQUIRED_FIELDS}
        del sections["Ruled Out / Don't Retry"]
        parts = ["CONTEXT_SNAPSHOT v1", ""]
        for field, body in sections.items():
            parts.append(f"## {field}")
            parts.append(body)
            parts.append("")
        with self.assertRaises(omlx_agent.SnapshotInvalid) as ctx:
            omlx_agent._validate_snapshot("\n".join(parts))
        self.assertIn("Ruled Out / Don't Retry", ctx.exception.missing)

    def test_empty_body_section_is_missing(self):
        # Heading present, body empty -> still considered missing.
        body = _full_snapshot_text({"Active Task": ""})
        with self.assertRaises(omlx_agent.SnapshotInvalid) as ctx:
            omlx_agent._validate_snapshot(body)
        self.assertIn("Active Task", ctx.exception.missing)

    def test_completely_empty_response_lists_all_missing(self):
        with self.assertRaises(omlx_agent.SnapshotInvalid) as ctx:
            omlx_agent._validate_snapshot("")
        self.assertEqual(
            set(ctx.exception.missing),
            set(omlx_agent.SNAPSHOT_REQUIRED_FIELDS),
        )


class RequestSnapshotTests(unittest.TestCase):
    def test_request_snapshot_disables_tools(self):
        captured: dict = {}

        def fake_api_call(messages, model, tools=None):
            captured["tools"] = tools
            captured["model"] = model
            captured["last_role"] = messages[-1]["role"] if messages else None
            return {
                "choices": [
                    {"message": {"content": _full_snapshot_text()}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }

        result = omlx_agent._request_snapshot(
            [{"role": "system", "content": "sys"},
             {"role": "user", "content": "do work"}],
            "test-model",
            api_call_fn=fake_api_call,
        )
        # Snapshot template was appended as a user-role instruction.
        self.assertEqual(captured["last_role"], "user")
        # Tools list MUST be empty for the snapshot call.
        self.assertEqual(captured["tools"], [])
        self.assertEqual(captured["model"], "test-model")
        # Returned dict has all required fields.
        self.assertIn("Active Task", result)
        self.assertIn("Ruled Out / Don't Retry", result)

    def test_request_snapshot_invalid_response_raises(self):
        def fake_api_call(messages, model, tools=None):
            return {
                "choices": [
                    {"message": {"content": "## Active Task\nonly one field"},
                     "finish_reason": "stop"}
                ],
            }

        with self.assertRaises(omlx_agent.SnapshotInvalid):
            omlx_agent._request_snapshot(
                [{"role": "user", "content": "hi"}],
                "test-model",
                api_call_fn=fake_api_call,
            )

    def test_request_snapshot_api_failure_raises_runtime(self):
        def fake_api_call(messages, model, tools=None):
            return None

        with self.assertRaises(RuntimeError):
            omlx_agent._request_snapshot(
                [{"role": "user", "content": "hi"}],
                "test-model",
                api_call_fn=fake_api_call,
            )

    def test_request_snapshot_overflow_raises_runtime(self):
        def fake_api_call(messages, model, tools=None):
            return {"_context_overflow": True}

        with self.assertRaises(RuntimeError):
            omlx_agent._request_snapshot(
                [{"role": "user", "content": "hi"}],
                "test-model",
                api_call_fn=fake_api_call,
            )


class SafeTailSliceTests(unittest.TestCase):
    def _make_msgs(self, n: int):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(n):
            msgs.append({"role": "user", "content": f"u{i} " + "x" * 200})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 200})
        return msgs

    def test_slice_respects_budget(self):
        msgs = self._make_msgs(10)  # ~ many tokens each
        small_budget = 50
        tail = omlx_agent._safe_tail_slice(msgs, small_budget)
        # Cannot return less than the last complete assistant turn.
        self.assertGreaterEqual(len(tail), 1)
        # Must end on assistant with no in-flight tool_calls.
        self.assertEqual(tail[-1]["role"], "assistant")
        self.assertFalse(tail[-1].get("tool_calls"))

    def test_slice_does_not_orphan_tool_response(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "n", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        tail = omlx_agent._safe_tail_slice(msgs, 30)  # very tight budget
        # The slice must NOT begin on the role:"tool" message in isolation.
        if tail:
            self.assertNotEqual(
                tail[0].get("role"), "tool",
                f"tail must not start with orphaned tool response; got {tail!r}",
            )

    def test_slice_drops_inflight_tail_assistant(self):
        # Last message is an assistant with tool_calls but no matching tool
        # response. _safe_tail_slice should walk past it (defensive guard).
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "earlier"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "n", "arguments": "{}"}}],
            },
        ]
        tail = omlx_agent._safe_tail_slice(msgs, 10_000)
        # Must end on a clean assistant turn, not the in-flight one.
        self.assertGreaterEqual(len(tail), 1)
        self.assertEqual(tail[-1].get("role"), "assistant")
        self.assertFalse(tail[-1].get("tool_calls"))

    def test_empty_input_returns_empty(self):
        self.assertEqual(omlx_agent._safe_tail_slice([], 1000), [])

    def test_full_budget_returns_everything_after_first_message(self):
        # Generous budget should capture most/all of the conversation.
        msgs = self._make_msgs(3)
        tail = omlx_agent._safe_tail_slice(msgs, 1_000_000)
        self.assertEqual(len(tail), len(msgs))


class RebuildMessagesTests(unittest.TestCase):
    def _snapshot(self):
        return {f: f"body for {f}" for f in omlx_agent.SNAPSHOT_REQUIRED_FIELDS}

    def test_rebuild_layout(self):
        msgs = [
            {"role": "system", "content": "sys-1"},
            {"role": "system", "content": "sys-2"},
            {"role": "user", "content": "first user"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "second user"},
            {"role": "assistant", "content": "a2"},
        ]
        rebuilt = omlx_agent._rebuild_messages_with_snapshot(
            msgs, self._snapshot(), tail_budget_tokens=1000,
        )
        # Leading system messages preserved.
        self.assertEqual(rebuilt[0]["content"], "sys-1")
        self.assertEqual(rebuilt[1]["content"], "sys-2")
        # First user prompt preserved.
        self.assertEqual(rebuilt[2]["role"], "user")
        self.assertEqual(rebuilt[2]["content"], "first user")
        # Snapshot injected as a system message.
        self.assertEqual(rebuilt[3]["role"], "system")
        self.assertIn("CONTEXT_SNAPSHOT v1", rebuilt[3]["content"])
        # Tail follows.
        self.assertGreaterEqual(len(rebuilt), 4)

    def test_rebuild_preserves_tool_call_pairing(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "n", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            {"role": "assistant", "content": "done"},
        ]
        rebuilt = omlx_agent._rebuild_messages_with_snapshot(
            msgs, self._snapshot(), tail_budget_tokens=10_000,
        )
        # Every assistant tool_calls id has a matching tool response in result.
        expected_ids: set = set()
        seen_ids: set = set()
        for m in rebuilt:
            if isinstance(m, dict) and m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        expected_ids.add(tc.get("id"))
            if isinstance(m, dict) and m.get("role") == "tool":
                seen_ids.add(m.get("tool_call_id"))
        self.assertTrue(
            expected_ids.issubset(seen_ids),
            f"unmatched tool_call ids in rebuilt: {expected_ids - seen_ids}",
        )

    def test_rebuild_avoids_duplicating_first_user(self):
        # If the tail slice would include the first user message, it should
        # not be repeated.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "only user"},
            {"role": "assistant", "content": "only reply"},
        ]
        rebuilt = omlx_agent._rebuild_messages_with_snapshot(
            msgs, self._snapshot(), tail_budget_tokens=10_000,
        )
        user_msgs = [m for m in rebuilt if m.get("role") == "user"]
        self.assertEqual(
            len(user_msgs), 1,
            f"first user prompt should appear exactly once: {rebuilt!r}",
        )

    def test_rebuild_with_explicit_first_user_idx(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first user (pre-context)"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "context-scoped first user"},
            {"role": "assistant", "content": "b"},
        ]
        rebuilt = omlx_agent._rebuild_messages_with_snapshot(
            msgs, self._snapshot(), first_user_idx=3, tail_budget_tokens=10_000,
        )
        # The user message right after the leading system block must be the
        # contextID-scoped one (index 3 in input), not the earliest one.
        self.assertEqual(rebuilt[1]["content"], "context-scoped first user")

    def test_rebuild_handles_empty_input(self):
        self.assertEqual(
            omlx_agent._rebuild_messages_with_snapshot(
                [], self._snapshot(), tail_budget_tokens=1000,
            ),
            [],
        )


class IntegrationTests(unittest.TestCase):
    def test_simulated_checkpoint_shrinks_message_list(self):
        """Simulates the U2-portion of the checkpoint cycle end-to-end."""
        # Build a large conversation.
        msgs = [{"role": "system", "content": "sys-prompt"},
                {"role": "user", "content": "the original task"}]
        for i in range(40):
            msgs.append({"role": "user", "content": f"step {i} " + "x" * 400})
            msgs.append({"role": "assistant", "content": f"reply {i} " + "y" * 400})

        original_tokens = sum(omlx_agent._message_token_estimate(m) for m in msgs)

        # Fake the model response with a valid snapshot.
        def fake_api_call(messages, model, tools=None):
            return {
                "choices": [
                    {"message": {"content": _full_snapshot_text()},
                     "finish_reason": "stop"}
                ],
            }

        snapshot = omlx_agent._request_snapshot(msgs, "test-model", api_call_fn=fake_api_call)
        rebuilt = omlx_agent._rebuild_messages_with_snapshot(
            msgs, snapshot, first_user_idx=1, tail_budget_tokens=2048,
        )
        rebuilt_tokens = sum(omlx_agent._message_token_estimate(m) for m in rebuilt)

        # Substantial shrinkage.
        self.assertLess(
            rebuilt_tokens, original_tokens // 3,
            f"expected significant shrinkage; got {rebuilt_tokens} from {original_tokens}",
        )
        # Snapshot is present as system role.
        snapshot_msgs = [m for m in rebuilt
                         if m.get("role") == "system" and "CONTEXT_SNAPSHOT" in (m.get("content") or "")]
        self.assertEqual(len(snapshot_msgs), 1)


if __name__ == "__main__":
    unittest.main()
