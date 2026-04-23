"""U3 tests for tag taxonomy, dump file format, and section tag extraction.

All filesystem operations are scoped to a tempdir passed via ``work_dir`` so
tests do not touch the user's real ``.currentContext/`` directory.
"""

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class SectionSplitTests(unittest.TestCase):
    def test_pairs_user_with_assistant(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        secs = omlx_agent._split_messages_into_sections(msgs)
        # 1 system section + 2 assistant-paired sections = 3 sections.
        self.assertEqual(len(secs), 3)
        self.assertEqual(secs[0]["role"], "system")
        self.assertEqual(secs[1]["role"], "assistant")
        # The user message is grouped INTO the assistant section's messages.
        self.assertEqual(len(secs[1]["messages"]), 2)

    def test_oversized_message_is_split(self):
        # Single assistant message way over the 2000-token threshold.
        big = "x" * (omlx_agent.SECTION_SPLIT_TOKEN_THRESHOLD * 4 * 3 + 100)
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": big},
        ]
        secs = omlx_agent._split_messages_into_sections(msgs)
        # Should produce more than one section for the oversized chunk.
        self.assertGreater(len(secs), 1)
        # All produced sections share the assistant role.
        self.assertTrue(all(s["role"] == "assistant" for s in secs))
        # split_chunk metadata is present and increments.
        chunks = [s["split_chunk"] for s in secs if "split_chunk" in s]
        self.assertEqual(chunks, list(range(len(chunks))))

    def test_trailing_user_without_assistant_still_emitted(self):
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2 (no reply yet)"},
        ]
        secs = omlx_agent._split_messages_into_sections(msgs)
        self.assertGreaterEqual(len(secs), 2)
        # The last section captures the trailing user message.
        last_text = omlx_agent._section_body_text(secs[-1]["messages"])
        self.assertIn("u2 (no reply yet)", last_text)


class AnchorTests(unittest.TestCase):
    def test_anchor_is_zero_padded(self):
        self.assertEqual(omlx_agent._anchor_for(1), "s-001")
        self.assertEqual(omlx_agent._anchor_for(14), "s-014")
        self.assertEqual(omlx_agent._anchor_for(999), "s-999")


class MasterTagsTests(unittest.TestCase):
    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(omlx_agent._load_master_tags(work_dir=tmp), set())

    def test_append_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            accepted = omlx_agent._append_master_tags(
                [("auth", "covers login"), ("payments", "stripe")],
                work_dir=tmp,
            )
            self.assertEqual(set(accepted), {"auth", "payments"})
            tags = omlx_agent._load_master_tags(work_dir=tmp)
            self.assertEqual(tags, {"auth", "payments"})

    def test_append_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            omlx_agent._append_master_tags([("auth", "x")], work_dir=tmp)
            accepted = omlx_agent._append_master_tags(
                [("auth", "again"), ("billing", "new")], work_dir=tmp,
            )
            self.assertEqual(accepted, ["billing"])

    def test_corrupt_master_file_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = omlx_agent._master_tags_path(tmp)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Write some binary garbage.
            with open(path, "wb") as f:
                f.write(b"\x00\x01\x02<binary>" + bytes(range(256)))
            # Must not raise; just returns whatever salvageable tags exist
            # (likely empty set).
            tags = omlx_agent._load_master_tags(work_dir=tmp)
            # Anything we did get back must look like a valid tag.
            for t in tags:
                self.assertTrue(omlx_agent._is_valid_tag(t),
                                f"loaded an invalid tag from binary garbage: {t!r}")

    def test_invalid_tag_rejected_on_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            accepted = omlx_agent._append_master_tags(
                [("", "empty"), ("ok-tag", "fine"),
                 ("with\nnewline", "bad"), ("x" * 200, "too long")],
                work_dir=tmp,
            )
            self.assertEqual(accepted, ["ok-tag"])


class DumpRenderingTests(unittest.TestCase):
    def test_render_includes_anchors_and_tags(self):
        sections = [
            {"role": "system", "messages": [{"role": "system", "content": "sys"}],
             "preview": "sys"},
            {"role": "assistant",
             "messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}],
             "preview": "hi/hello"},
        ]
        tags = {"s-001": ["sys-prompt"], "s-002": ["greeting", "smoke"]}
        text = omlx_agent._render_dump_markdown(
            context_id="HHMMSS-abcd",
            sections=sections,
            tags_by_section=tags,
            timestamp="2026-04-23T12:00:00",
        )
        self.assertIn('<a id="s-001"></a>', text)
        self.assertIn('<a id="s-002"></a>', text)
        self.assertIn("sys-prompt", text)
        self.assertIn("greeting", text)
        self.assertIn("HHMMSS-abcd", text)
        # The Section Tags inverse table is present.
        self.assertIn("## Section Tags", text)
        self.assertIn("`greeting`", text)

    def test_render_handles_no_tags(self):
        sections = [{"role": "user",
                     "messages": [{"role": "user", "content": "hi"}],
                     "preview": "hi"}]
        text = omlx_agent._render_dump_markdown(
            context_id="X", sections=sections, tags_by_section={},
        )
        self.assertIn("_(no tags)_", text)
        self.assertIn("**tags:** _(none)_", text)


class WriteDumpTests(unittest.TestCase):
    def test_write_dump_creates_file_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            sections = [
                {"role": "user",
                 "messages": [{"role": "user", "content": "hi"}],
                 "preview": "hi"},
            ]
            tags = {"s-001": ["greeting"]}
            dump_path, index_path = omlx_agent._write_dump_file(
                "HHMMSS-abcd", sections, tags,
                work_dir=tmp, timestamp="120000",
            )
            self.assertTrue(os.path.isfile(dump_path))
            self.assertTrue(os.path.isfile(index_path))
            # Index round-trip.
            index = omlx_agent._read_index(index_path)
            self.assertIn("greeting", index)
            self.assertEqual(index["greeting"][0]["file"], "dump-120000.md")
            self.assertEqual(index["greeting"][0]["anchor"], "s-001")

    def test_two_dumps_share_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            sec1 = [{"role": "user",
                     "messages": [{"role": "user", "content": "a"}],
                     "preview": "a"}]
            sec2 = [{"role": "user",
                     "messages": [{"role": "user", "content": "b"}],
                     "preview": "b"}]
            omlx_agent._write_dump_file(
                "HHMMSS-abcd", sec1, {"s-001": ["shared", "first-only"]},
                work_dir=tmp, timestamp="120000",
            )
            _, index_path = omlx_agent._write_dump_file(
                "HHMMSS-abcd", sec2, {"s-001": ["shared", "second-only"]},
                work_dir=tmp, timestamp="120100",
            )
            index = omlx_agent._read_index(index_path)
            self.assertEqual(len(index["shared"]), 2)
            files = sorted(h["file"] for h in index["shared"])
            self.assertEqual(files, ["dump-120000.md", "dump-120100.md"])
            self.assertEqual(len(index["first-only"]), 1)
            self.assertEqual(len(index["second-only"]), 1)


class RequestSectionTagsTests(unittest.TestCase):
    def _sections(self):
        return [
            {"role": "user", "messages": [{"role": "user", "content": "fix login bug"}],
             "preview": "fix login bug"},
            {"role": "assistant", "messages": [{"role": "assistant", "content": "ok"}],
             "preview": "ok"},
        ]

    def test_assigns_existing_tags_when_response_uses_them(self):
        def fake_api(messages, model, tools=None):
            return {"choices": [{"message": {"content": json.dumps({
                "sections": {"s-001": ["auth"], "s-002": []},
                "new_tags": [],
            })}}]}

        tags_by_section, new_tags = omlx_agent._request_section_tags(
            self._sections(), {"auth", "payments"}, "test-model",
            api_call_fn=fake_api,
        )
        self.assertEqual(tags_by_section["s-001"], ["auth"])
        self.assertEqual(tags_by_section["s-002"], [])
        self.assertEqual(new_tags, [])

    def test_proposed_new_tags_not_silently_renamed(self):
        # Per plan: if the model proposes a near-duplicate of an existing
        # tag, U3 must accept it as-is. Normalization (U5) does the merge.
        def fake_api(messages, model, tools=None):
            return {"choices": [{"message": {"content": json.dumps({
                "sections": {"s-001": ["authentication"]},
                "new_tags": [{"tag": "authentication", "reason": "auth flow"}],
            })}}]}

        tags_by_section, new_tags = omlx_agent._request_section_tags(
            self._sections()[:1], {"auth"}, "test-model",
            api_call_fn=fake_api,
        )
        # The proposed tag landed verbatim, not silently renamed to "auth".
        self.assertEqual(tags_by_section["s-001"], ["authentication"])
        self.assertEqual(len(new_tags), 1)
        self.assertEqual(new_tags[0][0], "authentication")

    def test_garbled_response_yields_empty_dicts(self):
        def fake_api(messages, model, tools=None):
            return {"choices": [{"message": {"content": "this is not json"}}]}

        tags_by_section, new_tags = omlx_agent._request_section_tags(
            self._sections(), set(), "test-model",
            api_call_fn=fake_api,
        )
        # All anchors are still present (mapped to []).
        self.assertEqual(set(tags_by_section.keys()), {"s-001", "s-002"})
        self.assertEqual(tags_by_section["s-001"], [])
        self.assertEqual(new_tags, [])

    def test_api_failure_is_not_fatal(self):
        def fake_api(messages, model, tools=None):
            return None

        tags_by_section, new_tags = omlx_agent._request_section_tags(
            self._sections(), set(), "test-model",
            api_call_fn=fake_api,
        )
        self.assertEqual(tags_by_section, {"s-001": [], "s-002": []})
        self.assertEqual(new_tags, [])


if __name__ == "__main__":
    unittest.main()
