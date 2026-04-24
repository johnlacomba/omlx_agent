"""U7 tests for /context state formatter and contextID hook gating."""

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class FormatContextStateTests(unittest.TestCase):
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

    def test_no_context_yet_shows_none(self):
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("Context Management", out)
        self.assertIn("(none -- no chat turn yet)", out)
        self.assertIn("Master taxonomy tags: 0", out)

    def test_after_mint_shows_context_id(self):
        cid = omlx_agent._mint_context_id()
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn(cid, out)
        self.assertIn("not yet created", out)  # archive dir not made yet

    def test_active_layer_shows_token_percent(self):
        omlx_agent._mint_context_id()
        state = omlx_agent._get_layer_state("work")
        state["last_prompt_tokens"] = 8000
        # Default model limit is 16384; 8000 / 16384 = ~49%
        out = omlx_agent._format_context_state(
            active_layer="work", active_model="unknown-model",
            work_dir=self.work_dir,
        )
        self.assertIn("Active layer: work", out)
        self.assertIn("8000/16384", out)
        self.assertIn("49%", out)

    def test_per_layer_dump_count_and_first_user_idx(self):
        omlx_agent._mint_context_id()
        state = omlx_agent._get_layer_state("work")
        state["dump_count"] = 3
        state["last_prompt_tokens"] = 1234
        state["first_user_msg_idx"] = 1
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("dumps=3", out)
        self.assertIn("last_tokens=1234", out)
        self.assertIn("first_user_idx=1", out)

    def test_threshold_pct_present(self):
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("60%", out)
        self.assertIn("90%", out)

    def test_master_tag_count_after_append(self):
        omlx_agent._append_master_tags(
            [("worker", "ok"), ("manager", "ok")], work_dir=self.work_dir,
        )
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("Master taxonomy tags: 2", out)

    def test_dump_archive_count_after_write(self):
        omlx_agent._mint_context_id()
        cid = omlx_agent._active_context_id
        # Write two dump files manually under the contextID dir.
        ctx_dir = omlx_agent._context_dir_for_id(cid, work_dir=self.work_dir)
        os.makedirs(ctx_dir, exist_ok=True)
        for ts in ("120000", "120030"):
            with open(os.path.join(ctx_dir, f"dump-{ts}.md"), "w") as f:
                f.write("dummy\n")
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("Dumps in this contextID: 2", out)
        self.assertIn("dump-120030.md", out)

    def test_last_failure_record_surfaced(self):
        omlx_agent._record_dump_failure({
            "error_type": "SnapshotInvalid",
            "layer": "work",
            "timestamp": "2026-04-23T12:00:00",
            "error": "missing fields",
        })
        out = omlx_agent._format_context_state(work_dir=self.work_dir)
        self.assertIn("Last dump failure: SnapshotInvalid", out)
        self.assertIn("layer=work", out)


class BeginUserChatTurnGatingTests(unittest.TestCase):
    """Sanity checks confirming the hook is the only mint surface for user prompts."""

    def setUp(self):
        omlx_agent._active_context_id = None
        omlx_agent._layer_context_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_call_changes_active_context_id(self):
        before = omlx_agent._active_context_id
        omlx_agent._begin_user_chat_turn(self.work_dir)
        after = omlx_agent._active_context_id
        self.assertNotEqual(before, after)
        self.assertIsNotNone(after)

    def test_normalization_runs_during_begin(self):
        # Pre-stage taxonomy so we can detect the marker that normalization adds.
        path = omlx_agent._master_tags_path(self.work_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("auth\n## added: telemetry -- new\n")
        omlx_agent._begin_user_chat_turn(self.work_dir)
        with open(path) as f:
            text = f.read()
        self.assertIn("## normalized:", text)


if __name__ == "__main__":
    unittest.main()
