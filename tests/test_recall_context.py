"""U4 tests for the recall_context tool.

All filesystem ops scoped to a tempdir via ``work_dir``. ``_active_context_id``
is patched per-test where the "current" resolution path is exercised.
"""

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


def _seed_context(work_dir: str, *,
                  context_id: str = "120000-abcd",
                  master_tags: list[str] | None = None,
                  dumps: list[dict] | None = None) -> None:
    """Helper: seed master tags + a contextID's dump+index files.

    ``dumps`` is a list of dicts: ``{"timestamp": "120000",
    "sections": [...], "tags_by_section": {...}}``.
    """
    if master_tags:
        omlx_agent._append_master_tags(
            [(t, "seed") for t in master_tags], work_dir=work_dir,
        )
    for d in dumps or []:
        omlx_agent._write_dump_file(
            context_id, d["sections"], d["tags_by_section"],
            work_dir=work_dir, timestamp=d["timestamp"],
        )


def _section(role: str, body: str) -> dict:
    return {"role": role,
            "messages": [{"role": role, "content": body}],
            "preview": body[:200]}


class RecallHappyPathTests(unittest.TestCase):
    def test_returns_matching_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "fix login bug deeply")],
                    "tags_by_section": {"s-001": ["auth"]},
                }],
            )
            with mock.patch.object(omlx_agent, "_active_context_id", "120000-abcd"):
                out = omlx_agent.tool_recall_context(
                    ["auth"], "current", 5, work_dir=tmp,
                )
            self.assertIn("fix login bug deeply", out)
            self.assertIn("dump-120000.md", out)
            self.assertIn("s-001", out)

    def test_explicit_context_id_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "fix login bug")],
                    "tags_by_section": {"s-001": ["auth"]},
                }],
            )
            # No active id needed when context_id is explicit.
            with mock.patch.object(omlx_agent, "_active_context_id", None):
                out = omlx_agent.tool_recall_context(
                    ["auth"], "120000-abcd", 5, work_dir=tmp,
                )
            self.assertIn("fix login bug", out)


class RecallOrderingAndLimitTests(unittest.TestCase):
    def test_most_recent_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[
                    {"timestamp": "120000",
                     "sections": [_section("user", "older login work")],
                     "tags_by_section": {"s-001": ["auth"]}},
                    {"timestamp": "130000",
                     "sections": [_section("user", "newer login work")],
                     "tags_by_section": {"s-001": ["auth"]}},
                ],
            )
            out = omlx_agent.tool_recall_context(
                ["auth"], "120000-abcd", 5, work_dir=tmp,
            )
            # Newer file should appear before the older one in the output.
            newer = out.find("dump-130000.md")
            older = out.find("dump-120000.md")
            self.assertGreaterEqual(newer, 0)
            self.assertGreaterEqual(older, 0)
            self.assertLess(newer, older,
                            f"expected newer dump first; output:\n{out}")

    def test_limit_caps_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[
                    {"timestamp": f"12000{i}",
                     "sections": [_section("user", f"section {i}")],
                     "tags_by_section": {"s-001": ["auth"]}}
                    for i in range(5)
                ],
            )
            out = omlx_agent.tool_recall_context(
                ["auth"], "120000-abcd", 2, work_dir=tmp,
            )
            # Only 2 hits should appear; the metadata line says "returning 2".
            self.assertIn("returning 2", out)
            # Five dumps were created but only two should be in the output.
            count = sum(1 for i in range(5) if f"dump-12000{i}.md" in out)
            self.assertEqual(count, 2)


class RecallErrorPathTests(unittest.TestCase):
    def test_no_active_context_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(omlx_agent, "_active_context_id", None):
                out = omlx_agent.tool_recall_context(
                    ["auth"], "current", 5, work_dir=tmp,
                )
            self.assertIn("No active contextID", out)

    def test_unknown_context_id_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = omlx_agent.tool_recall_context(
                ["auth"], "999999-zzzz", 5, work_dir=tmp,
            )
            self.assertIn("No archive found", out)

    def test_all_tags_unknown_returns_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["authentication", "billing"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "x")],
                    "tags_by_section": {"s-001": ["authentication"]},
                }],
            )
            out = omlx_agent.tool_recall_context(
                ["auth"], "120000-abcd", 5, work_dir=tmp,
            )
            self.assertIn("error", out.lower())
            # Should suggest the close-match "authentication".
            self.assertIn("authentication", out)

    def test_known_tag_with_no_hits_returns_empty_not_error(self):
        # Tag exists in master taxonomy, but no section in this contextID
        # is tagged with it. Per origin FR9: empty list, not an error.
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth", "billing"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "x")],
                    "tags_by_section": {"s-001": ["auth"]},
                }],
            )
            out = omlx_agent.tool_recall_context(
                ["billing"], "120000-abcd", 5, work_dir=tmp,
            )
            # Not an error message.
            self.assertNotIn("error", out.lower())
            # Mentions no matching sections.
            self.assertIn("No matching sections", out)

    def test_empty_tags_list_is_error(self):
        out = omlx_agent.tool_recall_context([], "current", 5)
        self.assertIn("at least one tag", out)

    def test_string_tags_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "found me")],
                    "tags_by_section": {"s-001": ["auth"]},
                }],
            )
            # Comma-separated string should also work.
            out = omlx_agent.tool_recall_context(
                "auth", "120000-abcd", 5, work_dir=tmp,
            )
            self.assertIn("found me", out)


class RecallBudgetTests(unittest.TestCase):
    def test_truncates_last_hit_rather_than_dropping(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Make a section so large its body alone exceeds the budget.
            big = "A" * (omlx_agent.RECALL_TOKEN_BUDGET * 4 + 1000)
            _seed_context(
                tmp,
                master_tags=["big"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("assistant", big)],
                    "tags_by_section": {"s-001": ["big"]},
                }],
            )
            out = omlx_agent.tool_recall_context(
                ["big"], "120000-abcd", 5, work_dir=tmp,
            )
            self.assertIn("[truncated]", out)
            # And the body that DID make it in is at most budget chars long.
            # Sanity: budget * 4 chars; output string is bounded by budget +
            # markdown overhead (a few hundred chars).
            self.assertLess(
                len(out),
                omlx_agent.RECALL_TOKEN_BUDGET * 4 + 2000,
                "recall output exceeded budget by more than markdown overhead",
            )


class RecallToolWiringTests(unittest.TestCase):
    def test_registered_in_tools_and_dispatch(self):
        names = [t["function"]["name"] for t in omlx_agent.TOOLS
                 if isinstance(t, dict) and "function" in t]
        self.assertIn("recall_context", names)
        self.assertIn("recall_context", omlx_agent.TOOL_DISPATCH)

    def test_dispatch_invokes_tool_with_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_context(
                tmp,
                master_tags=["auth"],
                dumps=[{
                    "timestamp": "120000",
                    "sections": [_section("user", "via dispatch")],
                    "tags_by_section": {"s-001": ["auth"]},
                }],
            )
            # Dispatch uses the module-level WORK_DIR; patch it for this test.
            with mock.patch.object(omlx_agent, "WORK_DIR", tmp):
                out = omlx_agent.TOOL_DISPATCH["recall_context"](
                    {"tags": ["auth"], "context_id": "120000-abcd", "limit": 3}
                )
            self.assertIn("via dispatch", out)


class ReadDumpSectionTests(unittest.TestCase):
    def test_extracts_body_between_anchor_and_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            sections = [_section("user", "body of section one")]
            tags = {"s-001": ["x"]}
            dump_path, _ = omlx_agent._write_dump_file(
                "120000-abcd", sections, tags,
                work_dir=tmp, timestamp="120000",
            )
            body = omlx_agent._read_dump_section(dump_path, "s-001")
            self.assertIsNotNone(body)
            self.assertIn("body of section one", body)

    def test_unknown_anchor_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            sections = [_section("user", "x")]
            dump_path, _ = omlx_agent._write_dump_file(
                "120000-abcd", sections, {"s-001": ["x"]},
                work_dir=tmp, timestamp="120000",
            )
            self.assertIsNone(omlx_agent._read_dump_section(dump_path, "s-999"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            omlx_agent._read_dump_section("/nope/does/not/exist.md", "s-001")
        )


if __name__ == "__main__":
    unittest.main()
