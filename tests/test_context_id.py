"""U5 tests for contextID minting and synchronous tag normalization.

All filesystem operations are scoped to a tempdir passed via ``work_dir``.
"""

import os
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class MintContextIdTests(unittest.TestCase):
    def setUp(self):
        omlx_agent._active_context_id = None
        omlx_agent._layer_context_state.clear()

    def test_format_is_hhmmss_dash_4hex(self):
        cid = omlx_agent._mint_context_id()
        parts = cid.split("-")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 6)  # HHMMSS
        self.assertTrue(parts[0].isdigit())
        self.assertEqual(len(parts[1]), 4)  # 2 random bytes -> 4 hex chars
        self.assertTrue(all(c in "0123456789abcdef" for c in parts[1]))

    def test_sets_active_context_id(self):
        cid = omlx_agent._mint_context_id()
        self.assertEqual(omlx_agent._active_context_id, cid)

    def test_subsequent_mints_are_distinct(self):
        a = omlx_agent._mint_context_id()
        # Sleep just enough that os.urandom collisions are vanishingly unlikely
        # AND that an HHMMSS rollover is at least possible (not required).
        time.sleep(0.01)
        b = omlx_agent._mint_context_id()
        self.assertNotEqual(a, b)

    def test_resets_existing_layer_state(self):
        state = omlx_agent._get_layer_state("work")
        state["context_id"] = "old-cid"
        state["first_user_msg_idx"] = 7
        state["dump_count"] = 3
        state["last_dump_at_round"] = 5
        cid = omlx_agent._mint_context_id()
        self.assertEqual(state["context_id"], cid)
        self.assertIsNone(state["first_user_msg_idx"])
        self.assertEqual(state["dump_count"], 0)
        self.assertEqual(state["last_dump_at_round"], -1)


class TagMergeTargetTests(unittest.TestCase):
    def test_substring_match_picks_canonical(self):
        # "auth" already canonical; "authentication" should merge into "auth".
        self.assertEqual(
            omlx_agent._tag_merge_target("authentication", ["auth", "manager"]),
            "auth",
        )

    def test_reverse_substring_also_matches(self):
        # canonical is the longer name; pending is the shortcut.
        self.assertEqual(
            omlx_agent._tag_merge_target("auth", ["authentication", "manager"]),
            "authentication",
        )

    def test_typo_close_match(self):
        # Close ratio without substring containment.
        self.assertEqual(
            omlx_agent._tag_merge_target("authentcation", ["authentication"]),
            "authentication",
        )

    def test_no_close_match_returns_none(self):
        self.assertIsNone(
            omlx_agent._tag_merge_target("telemetry", ["auth", "manager"]),
        )

    def test_empty_canonical_returns_none(self):
        self.assertIsNone(omlx_agent._tag_merge_target("anything", []))


class NormalizeTagsPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write_taxonomy(self, body: str) -> None:
        path = omlx_agent._master_tags_path(self.work_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    def _read_taxonomy(self) -> str:
        with open(omlx_agent._master_tags_path(self.work_dir), "r") as f:
            return f.read()

    def test_no_taxonomy_file_returns_empty(self):
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(result["merges"], {})
        self.assertEqual(result["promoted"], [])
        self.assertEqual(result["marker"], "")

    def test_pending_added_merges_into_existing_canonical(self):
        # auth is canonical (plain line, before marker); authentication is pending.
        self._write_taxonomy(
            "# Context Tags Master Taxonomy\n\n"
            "auth\n"
            "## normalized: 2026-04-01T00:00:00\n"
            "## added: authentication -- model proposed it\n"
        )
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(result["merges"], {"authentication": "auth"})
        self.assertEqual(result["promoted"], [])
        text = self._read_taxonomy()
        self.assertIn("## merged: authentication -> auth", text)
        # The marker is the last non-empty line in the file.
        non_empty = [ln for ln in text.splitlines() if ln.strip()]
        self.assertTrue(non_empty[-1].startswith("## normalized:"))

    def test_pending_with_no_close_match_is_promoted(self):
        self._write_taxonomy(
            "auth\n"
            "## normalized: 2026-04-01T00:00:00\n"
            "## added: telemetry -- new domain\n"
        )
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(result["merges"], {})
        self.assertEqual(result["promoted"], ["telemetry"])

    def test_no_prior_marker_first_added_promoted_second_merges(self):
        # First contextTags.md ever: no marker, two added lines.
        self._write_taxonomy(
            "## added: auth -- first prompt\n"
            "## added: authentication -- second prompt\n"
        )
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(result["promoted"], ["auth"])
        self.assertEqual(result["merges"], {"authentication": "auth"})

    def test_rewrites_index_md_for_merged_tag(self):
        # Build .currentContext/<date>/<cid>/index.md referencing the duplicate.
        date_str = "2026-04-23"
        cid = "120000-aaaa"
        cid_dir = omlx_agent._context_dir_for_id(
            cid, work_dir=self.work_dir, date_str=date_str
        )
        os.makedirs(cid_dir, exist_ok=True)
        idx_path = os.path.join(cid_dir, "index.md")
        omlx_agent._write_index(idx_path, {
            "authentication": [{"file": "dump-120000.md", "anchor": "s-002"}],
            "manager": [{"file": "dump-120000.md", "anchor": "s-003"}],
        })
        self._write_taxonomy(
            "auth\n"
            "## normalized: 2026-04-01T00:00:00\n"
            "## added: authentication -- duplicate\n"
        )
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertIn("authentication", result["merges"])
        rewritten = omlx_agent._read_index(idx_path)
        self.assertNotIn("authentication", rewritten)
        self.assertIn("auth", rewritten)
        self.assertEqual(rewritten["auth"][0]["anchor"], "s-002")
        # untouched key survives
        self.assertIn("manager", rewritten)

    def test_index_with_existing_target_tag_appends_hits(self):
        date_str = "2026-04-23"
        cid = "120000-bbbb"
        cid_dir = omlx_agent._context_dir_for_id(
            cid, work_dir=self.work_dir, date_str=date_str
        )
        os.makedirs(cid_dir, exist_ok=True)
        idx_path = os.path.join(cid_dir, "index.md")
        omlx_agent._write_index(idx_path, {
            "auth": [{"file": "dump-1.md", "anchor": "s-001"}],
            "authentication": [{"file": "dump-2.md", "anchor": "s-002"}],
        })
        self._write_taxonomy(
            "auth\n"
            "## normalized: 2026-04-01T00:00:00\n"
            "## added: authentication -- duplicate\n"
        )
        omlx_agent._normalize_tags_pass(self.work_dir)
        rewritten = omlx_agent._read_index(idx_path)
        self.assertNotIn("authentication", rewritten)
        anchors = [h["anchor"] for h in rewritten["auth"]]
        self.assertIn("s-001", anchors)
        self.assertIn("s-002", anchors)

    def test_corrupt_taxonomy_does_not_raise(self):
        # Write binary garbage where the taxonomy should be.
        path = omlx_agent._master_tags_path(self.work_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02\xffnot valid utf8 \xfe garbage")
        # Should not raise; returns a result dict.
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertIsInstance(result, dict)
        self.assertIn("marker", result)

    def test_marker_appended_on_success(self):
        self._write_taxonomy("auth\n## added: telemetry -- new\n")
        result = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertTrue(result["marker"])
        text = self._read_taxonomy()
        self.assertIn(f"## normalized: {result['marker']}", text)

    def test_second_pass_does_not_remerge(self):
        # First pass merges; second pass should see no pending lines.
        self._write_taxonomy(
            "auth\n"
            "## added: authentication -- duplicate\n"
        )
        first = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(first["merges"], {"authentication": "auth"})
        second = omlx_agent._normalize_tags_pass(self.work_dir)
        self.assertEqual(second["merges"], {})
        self.assertEqual(second["promoted"], [])


class BeginUserChatTurnTests(unittest.TestCase):
    def setUp(self):
        omlx_agent._active_context_id = None
        omlx_agent._layer_context_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_mints_and_sets_active_context_id(self):
        cid = omlx_agent._begin_user_chat_turn(self.work_dir)
        self.assertEqual(omlx_agent._active_context_id, cid)
        self.assertRegex(cid, r"^\d{6}-[0-9a-f]{4}$")

    def test_does_not_raise_when_taxonomy_missing(self):
        # No contextTags.md exists; must not raise.
        omlx_agent._begin_user_chat_turn(self.work_dir)

    def test_two_calls_produce_distinct_ids(self):
        a = omlx_agent._begin_user_chat_turn(self.work_dir)
        time.sleep(0.005)
        b = omlx_agent._begin_user_chat_turn(self.work_dir)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
