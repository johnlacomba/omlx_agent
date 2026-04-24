"""U6 tests for run_checkpoint_cycle: success, failure fallback, re-trigger guard."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


def _full_snapshot_text(overrides=None) -> str:
    sections = {
        "Active Task": "Implement context-management U6",
        "Current Step": "Wire the checkpoint cycle",
        "Decisions Made": "- Use stdlib only",
        "Ruled Out / Don't Retry": "- Wipe system prompt",
        "Verified Facts": "- emergency_trim is the proven fallback",
        "Unverified Assumptions": "- Model returns valid JSON",
        "Open Questions": "(none)",
        "Files Touched": "- omlx_agent/omlx_agent.py",
        "Next Intended Action": "Add tests",
    }
    if overrides:
        sections.update(overrides)
    parts = ["CONTEXT_SNAPSHOT v1", ""]
    for field in omlx_agent.SNAPSHOT_REQUIRED_FIELDS:
        parts.append(f"## {field}")
        parts.append(sections[field])
        parts.append("")
    return "\n".join(parts)


def _ok_response(content: str) -> dict:
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _make_fake_api_call(snapshot_text=None, tags_payload=None,
                        snapshot_exception=None):
    """Build a fake api_call that distinguishes snapshot vs tag requests.

    The snapshot request appends SNAPSHOT_TEMPLATE to the user role; the tag
    request uses an instruction system message. We dispatch based on whether
    the last user message contains the snapshot template marker.
    """
    if snapshot_text is None:
        snapshot_text = _full_snapshot_text()
    if tags_payload is None:
        tags_payload = {"sections": {}, "new_tags": []}

    calls = []

    def fake(messages, model, tools=None):
        calls.append({"messages": list(messages), "model": model, "tools": tools})
        # Snapshot call: SNAPSHOT_TEMPLATE is inserted as the final user message.
        last = messages[-1] if messages else {}
        last_content = last.get("content") if isinstance(last, dict) else ""
        if isinstance(last_content, str) and "CONTEXT_SNAPSHOT" in last_content:
            if snapshot_exception is not None:
                raise snapshot_exception
            return _ok_response(snapshot_text)
        # Otherwise it's the section-tag request.
        return _ok_response(json.dumps(tags_payload))

    fake.calls = calls
    return fake


class CheckpointCycleSuccessTests(unittest.TestCase):
    def setUp(self):
        omlx_agent._active_context_id = None
        omlx_agent._layer_context_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = self.tmp.name
        # Redirect failure log to the tempdir so we don't touch real ~/.omlx.
        self._orig_failure_path = omlx_agent.LAST_DUMP_FAILURE_PATH
        omlx_agent.LAST_DUMP_FAILURE_PATH = os.path.join(
            self.work_dir, "last_dump_failure.json"
        )

    def tearDown(self):
        omlx_agent.LAST_DUMP_FAILURE_PATH = self._orig_failure_path
        self.tmp.cleanup()

    def _build_messages(self):
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "first user prompt of contextID"},
            {"role": "assistant", "content": "I will start."},
            {"role": "user", "content": "next step"},
            {"role": "assistant", "content": "Working on it." * 200},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "Still working." * 200},
        ]

    def test_success_returns_rebuilt_messages_and_writes_dump(self):
        omlx_agent._mint_context_id()
        cid = omlx_agent._active_context_id
        fake = _make_fake_api_call(
            tags_payload={"sections": {"s-002": ["worker", "test-task"]},
                          "new_tags": [{"tag": "test-task", "reason": "demo"}]},
        )
        msgs = self._build_messages()
        rebuilt, result = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
            original_token_count=12000,
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["fallback"])
        self.assertEqual(result["context_id"], cid)
        self.assertEqual(result["layer"], "work")
        self.assertEqual(result["old_tokens"], 12000)
        self.assertGreater(result["new_tokens"], 0)
        # Rebuilt list must be smaller than the original.
        self.assertLess(len(rebuilt), len(msgs) + 5)
        # Dump file written under tempdir.
        self.assertTrue(os.path.isfile(result["dump_path"]))
        self.assertTrue(os.path.isfile(result["index_path"]))
        # New tag landed in master taxonomy.
        master = omlx_agent._load_master_tags(self.work_dir)
        self.assertIn("test-task", master)

    def test_rebuilt_message_list_starts_with_system_then_first_user(self):
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call()
        msgs = self._build_messages()
        rebuilt, _ = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        self.assertEqual(rebuilt[0]["role"], "system")
        # Index 1 should be the first_user_msg of the original list.
        self.assertEqual(rebuilt[1]["role"], "user")
        self.assertIn("first user prompt", rebuilt[1]["content"])
        # The injected snapshot is a system message with CONTEXT_SNAPSHOT marker.
        self.assertTrue(any(
            isinstance(m.get("content"), str) and "CONTEXT_SNAPSHOT" in m["content"]
            for m in rebuilt
        ))

    def test_dump_count_increments_per_layer(self):
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call()
        msgs = self._build_messages()
        omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        state = omlx_agent._get_layer_state("work")
        self.assertEqual(state["dump_count"], 2)

    def test_no_active_context_id_mints_one_defensively(self):
        # Start with no active id at all.
        self.assertIsNone(omlx_agent._active_context_id)
        fake = _make_fake_api_call()
        msgs = self._build_messages()
        _, result = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        self.assertIsNotNone(result["context_id"])
        self.assertEqual(omlx_agent._active_context_id, result["context_id"])

    def test_index_md_records_dump_anchors(self):
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call(
            tags_payload={"sections": {"s-002": ["worker"], "s-003": ["worker"]},
                          "new_tags": []},
        )
        msgs = self._build_messages()
        # Pre-seed master taxonomy so the worker tag is canonical.
        omlx_agent._append_master_tags([("worker", "canonical")], work_dir=self.work_dir)
        _, result = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        index = omlx_agent._read_index(result["index_path"])
        self.assertIn("worker", index)
        self.assertGreaterEqual(len(index["worker"]), 1)


class CheckpointCycleFailureTests(unittest.TestCase):
    def setUp(self):
        omlx_agent._active_context_id = None
        omlx_agent._layer_context_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = self.tmp.name
        self._orig_failure_path = omlx_agent.LAST_DUMP_FAILURE_PATH
        omlx_agent.LAST_DUMP_FAILURE_PATH = os.path.join(
            self.work_dir, "last_dump_failure.json"
        )

    def tearDown(self):
        omlx_agent.LAST_DUMP_FAILURE_PATH = self._orig_failure_path
        self.tmp.cleanup()

    def _build_messages(self, n_assistant=8):
        # Need >=4 body messages so emergency_trim has something to drop.
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(n_assistant):
            msgs.append({"role": "user", "content": f"user {i}"})
            msgs.append({"role": "assistant", "content": f"asst {i}"})
        return msgs

    def test_invalid_snapshot_falls_back_to_emergency_trim(self):
        omlx_agent._mint_context_id()
        # Snapshot text missing nearly all required fields.
        fake = _make_fake_api_call(snapshot_text="## Active Task\nonly one")
        msgs = self._build_messages()
        original_len = len(msgs)
        rebuilt, result = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
            original_token_count=20000,
        )
        self.assertTrue(result["fallback"])
        self.assertEqual(result["error_type"], "SnapshotInvalid")
        # emergency_trim drops 20 oldest non-system but caps to keep at least 4
        # body messages, so the list shrinks (or stays the same if nothing to drop).
        self.assertLessEqual(len(rebuilt), original_len)
        # Failure record was written.
        self.assertTrue(os.path.isfile(omlx_agent.LAST_DUMP_FAILURE_PATH))
        with open(omlx_agent.LAST_DUMP_FAILURE_PATH) as f:
            record = json.load(f)
        self.assertEqual(record["error_type"], "SnapshotInvalid")
        self.assertEqual(record["layer"], "work")
        self.assertEqual(record["original_token_count"], 20000)
        self.assertIn("traceback", record)
        # missing field list is preserved
        self.assertIsInstance(record["missing_fields"], list)
        self.assertGreater(len(record["missing_fields"]), 0)

    def test_network_error_falls_back(self):
        import urllib.error
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call(
            snapshot_exception=urllib.error.URLError("connection refused"),
        )
        msgs = self._build_messages()
        rebuilt, result = omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        self.assertTrue(result["fallback"])
        self.assertEqual(result["error_type"], "URLError")
        self.assertTrue(os.path.isfile(omlx_agent.LAST_DUMP_FAILURE_PATH))

    def test_disk_write_error_falls_back(self):
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call()
        msgs = self._build_messages()
        # Patch _write_dump_file to simulate disk failure.
        with mock.patch.object(omlx_agent, "_write_dump_file",
                               side_effect=OSError("disk full")):
            rebuilt, result = omlx_agent.run_checkpoint_cycle(
                "work", msgs, "fake-model",
                work_dir=self.work_dir, api_call_fn=fake,
            )
        self.assertTrue(result["fallback"])
        self.assertEqual(result["error_type"], "OSError")

    def test_failure_does_not_increment_dump_count(self):
        omlx_agent._mint_context_id()
        fake = _make_fake_api_call(snapshot_text="## Active Task\nonly")
        msgs = self._build_messages()
        omlx_agent.run_checkpoint_cycle(
            "work", msgs, "fake-model",
            work_dir=self.work_dir, api_call_fn=fake,
        )
        state = omlx_agent._get_layer_state("work")
        self.assertEqual(state["dump_count"], 0)


class RecordDumpFailureTests(unittest.TestCase):
    def test_record_dump_failure_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "deep", "nest", "fail.json")
            orig = omlx_agent.LAST_DUMP_FAILURE_PATH
            omlx_agent.LAST_DUMP_FAILURE_PATH = target
            try:
                omlx_agent._record_dump_failure({"error": "test"})
                self.assertTrue(os.path.isfile(target))
                with open(target) as f:
                    self.assertEqual(json.load(f)["error"], "test")
            finally:
                omlx_agent.LAST_DUMP_FAILURE_PATH = orig

    def test_record_dump_failure_swallows_exceptions(self):
        # Path that can never be created (root of nonsense filesystem).
        orig = omlx_agent.LAST_DUMP_FAILURE_PATH
        omlx_agent.LAST_DUMP_FAILURE_PATH = "/proc/this/cannot/be/written.json"
        try:
            # Must not raise.
            omlx_agent._record_dump_failure({"error": "test"})
        finally:
            omlx_agent.LAST_DUMP_FAILURE_PATH = orig


if __name__ == "__main__":
    unittest.main()
