"""Tests for HybridIndex data model, streaming builder, and SQLite embedding cache."""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class ChunkMarkdownTests(unittest.TestCase):
    def test_splits_by_h2_headers(self):
        text = "## Problem\nSome problem.\n\n## Solution\nSome solution."
        chunks = omlx_agent._chunk_markdown(text, "/tmp/test.md", {})
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].section_title, "Problem")
        self.assertEqual(chunks[1].section_title, "Solution")
        self.assertIn("Some problem.", chunks[0].text)
        self.assertIn("Some solution.", chunks[1].text)

    def test_no_headers_produces_single_chunk(self):
        text = "Just some plain text without any headers."
        chunks = omlx_agent._chunk_markdown(text, "/tmp/test.md", {})
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].section_title, "(full document)")
        self.assertEqual(chunks[0].chunk_id, "test.md::full")

    def test_empty_text_returns_empty(self):
        chunks = omlx_agent._chunk_markdown("", "/tmp/test.md", {})
        self.assertEqual(len(chunks), 0)

    def test_chunk_id_is_lowercase_with_dashes(self):
        text = "## My Great Section\nContent here."
        chunks = omlx_agent._chunk_markdown(text, "/tmp/test.md", {})
        self.assertNotIn(" ", chunks[0].chunk_id)
        self.assertEqual(chunks[0].chunk_id, "test.md::my-great-section")

    def test_source_meta_carried_through(self):
        text = "## Test\nContent."
        meta = {"tags": ["foo", "bar"], "title": "Test Doc"}
        chunks = omlx_agent._chunk_markdown(text, "/tmp/test.md", meta)
        self.assertEqual(chunks[0].source_meta, meta)

    def test_required_fields_present(self):
        text = "## Section\nContent."
        chunks = omlx_agent._chunk_markdown(text, "/tmp/test.md", {"key": "val"})
        c = chunks[0]
        self.assertTrue(hasattr(c, "text"))
        self.assertTrue(hasattr(c, "path"))
        self.assertTrue(hasattr(c, "chunk_id"))
        self.assertTrue(hasattr(c, "section_title"))
        self.assertTrue(hasattr(c, "source_meta"))


class StreamSolutionsTests(unittest.TestCase):
    def test_indexes_real_solution_docs(self):
        chunks = omlx_agent._stream_solutions_for_index(_REPO_ROOT)
        self.assertGreater(len(chunks), 0)
        paths = {c.path for c in chunks}
        self.assertTrue(any("completion-verifier" in p for p in paths))
        self.assertTrue(any("web-research" in p for p in paths))

    def test_indexes_learnings_entries(self):
        chunks = omlx_agent._stream_solutions_for_index(_REPO_ROOT)
        learnings_chunks = [c for c in chunks if c.source_meta.get("_source") == "learnings"]
        self.assertGreater(len(learnings_chunks), 0)

    def test_empty_directory_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chunks = omlx_agent._stream_solutions_for_index(tmpdir)
            self.assertEqual(len(chunks), 0)

    def test_unreadable_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sol_dir = os.path.join(tmpdir, "docs", "solutions")
            os.makedirs(sol_dir)
            good = os.path.join(sol_dir, "good.md")
            with open(good, "w") as f:
                f.write("## Good\nContent.")
            bad = os.path.join(sol_dir, "bad.md")
            with open(bad, "w") as f:
                f.write("## Bad\nContent.")
            os.chmod(bad, 0o000)
            try:
                chunks = omlx_agent._stream_solutions_for_index(tmpdir)
                self.assertGreater(len(chunks), 0)
                self.assertTrue(any("good" in c.path for c in chunks))
            finally:
                os.chmod(bad, 0o644)


class HybridIndexTests(unittest.TestCase):
    def test_batch_build_returns_dict(self):
        idx = omlx_agent.HybridIndex()
        result = idx.batch_build(_REPO_ROOT)
        self.assertIsInstance(result, dict)
        self.assertGreater(len(result), 0)

    def test_is_built_flag(self):
        idx = omlx_agent.HybridIndex()
        self.assertFalse(idx.is_built)
        idx.batch_build(_REPO_ROOT)
        self.assertTrue(idx.is_built)

    def test_bm25_index_populated(self):
        idx = omlx_agent.HybridIndex()
        idx.batch_build(_REPO_ROOT)
        self.assertGreater(len(idx._bm25_doc_freqs), 0)
        self.assertGreater(idx._bm25_avg_dl, 0)

    def test_empty_build(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = omlx_agent.HybridIndex()
            result = idx.batch_build(tmpdir)
            self.assertEqual(len(result), 0)
            self.assertTrue(idx.is_built)


class RagTokenizeTests(unittest.TestCase):
    def test_handles_short_tokens(self):
        tokens = omlx_agent._rag_tokenize("db UI CI sqlite cache")
        self.assertIn("db", tokens)
        self.assertIn("ui", tokens)
        self.assertIn("ci", tokens)

    def test_lowercase(self):
        tokens = omlx_agent._rag_tokenize("SQLite CACHE TTL")
        self.assertTrue(all(t == t.lower() for t in tokens))

    def test_unicode(self):
        tokens = omlx_agent._rag_tokenize("café naïve résumé")
        self.assertIsInstance(tokens, list)


class EmbeddingCacheTests(unittest.TestCase):
    def test_init_creates_table(self):
        import sqlite3
        omlx_agent._init_embedding_cache()
        conn = sqlite3.connect(omlx_agent.SEARCH_CACHE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_cache'")
        self.assertIsNotNone(cursor.fetchone())
        conn.close()

    def test_store_and_retrieve(self):
        import struct
        omlx_agent._init_embedding_cache()
        chunk = omlx_agent.DocumentChunk(
            text="test text", path="/tmp/test.md",
            chunk_id="test::chunk1", section_title="Test",
            source_meta={},
        )
        embedding = [0.1] * 384
        omlx_agent._store_cached_embeddings("/tmp/test.md", 1000.0, [chunk], {"test::chunk1": embedding})
        result = omlx_agent._get_cached_embeddings("/tmp/test.md", 1000.0)
        self.assertIsNotNone(result)
        self.assertIn("test::chunk1", result)
        self.assertAlmostEqual(result["test::chunk1"][0], 0.1, places=5)

    def test_stale_mtime_returns_none(self):
        omlx_agent._init_embedding_cache()
        chunk = omlx_agent.DocumentChunk(
            text="test", path="/tmp/stale.md",
            chunk_id="stale::chunk1", section_title="Test",
            source_meta={},
        )
        omlx_agent._store_cached_embeddings("/tmp/stale.md", 1000.0, [chunk], {"stale::chunk1": [0.0] * 384})
        result = omlx_agent._get_cached_embeddings("/tmp/stale.md", 2000.0)
        self.assertIsNone(result)

    def test_missing_path_returns_none(self):
        omlx_agent._init_embedding_cache()
        result = omlx_agent._get_cached_embeddings("/tmp/nonexistent.md", 1000.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
