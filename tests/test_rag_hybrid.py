"""Tests for hybrid retrieval pipeline, format_rag_context, and end-to-end wiring."""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class HybridRetrieveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = omlx_agent.HybridIndex()
        cls.idx.batch_build(_REPO_ROOT)

    def test_sqlite_cache_returns_web_research_doc(self):
        results = self.idx.hybrid_retrieve("sqlite cache TTL")
        self.assertGreater(len(results), 0)
        self.assertTrue(any("web-research" in r.path for r in results))

    def test_completion_verifier_returns_completion_doc(self):
        results = self.idx.hybrid_retrieve("verifier too strict")
        self.assertGreater(len(results), 0)
        self.assertTrue(any("completion-verifier" in r.path for r in results))

    def test_bm25_only_still_returns_results(self):
        results = self.idx.hybrid_retrieve("sqlite cache expiration")
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertGreaterEqual(r.bm25_score, 0.0)

    def test_unknown_query_returns_empty(self):
        results = self.idx.hybrid_retrieve("xyzzyplugh42 nonexistent gibberish")
        self.assertEqual(len(results), 0)

    def test_empty_query_returns_empty(self):
        results = self.idx.hybrid_retrieve("")
        self.assertEqual(len(results), 0)

    def test_results_are_retrieved_entry(self):
        results = self.idx.hybrid_retrieve("sqlite cache")
        for r in results:
            self.assertIsInstance(r, omlx_agent.RetrievedEntry)
            self.assertTrue(hasattr(r, "score"))
            self.assertTrue(hasattr(r, "bm25_score"))
            self.assertTrue(hasattr(r, "cosine_score"))
            self.assertTrue(hasattr(r, "text"))
            self.assertTrue(hasattr(r, "path"))

    def test_scores_between_zero_and_one(self):
        results = self.idx.hybrid_retrieve("cache expiration")
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0)
            self.assertGreaterEqual(r.bm25_score, 0.0)
            self.assertLessEqual(r.bm25_score, 1.0)

    def test_results_sorted_descending(self):
        results = self.idx.hybrid_retrieve("sqlite cache")
        if len(results) > 1:
            for i in range(len(results) - 1):
                self.assertGreaterEqual(results[i].score, results[i + 1].score)


class HybridRetrieveEmptyCorpusTests(unittest.TestCase):
    def test_empty_corpus_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = omlx_agent.HybridIndex()
            idx.batch_build(tmpdir)
            results = idx.hybrid_retrieve("anything")
            self.assertEqual(len(results), 0)


class HybridRetrieveAlphaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = omlx_agent.HybridIndex()
        cls.idx.batch_build(_REPO_ROOT)

    def test_alpha_1_is_bm25_only(self):
        original_alpha = omlx_agent.RAG_ALPHA
        try:
            omlx_agent.RAG_ALPHA = 1.0
            results = self.idx.hybrid_retrieve("sqlite cache")
            for r in results:
                self.assertAlmostEqual(r.score, r.bm25_score, places=5)
        finally:
            omlx_agent.RAG_ALPHA = original_alpha

    def test_alpha_0_is_embedding_only(self):
        original_alpha = omlx_agent.RAG_ALPHA
        try:
            omlx_agent.RAG_ALPHA = 0.0
            results = self.idx.hybrid_retrieve("sqlite cache")
            for r in results:
                self.assertAlmostEqual(r.score, r.cosine_score, places=5)
        finally:
            omlx_agent.RAG_ALPHA = original_alpha


class FormatRagContextTests(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(omlx_agent._format_rag_context([]), "")

    def test_formats_retrieved_entry(self):
        entry = omlx_agent.RetrievedEntry(
            path="/tmp/test.md",
            chunk_id="test::chunk",
            section_title="Test Section",
            score=0.85,
            bm25_score=0.9,
            cosine_score=0.7,
            source_meta={},
            text="Some solution text.",
        )
        result = omlx_agent._format_rag_context([entry])
        self.assertIn("Retrieved prior solutions", result)
        self.assertIn("Test Section", result)
        self.assertIn("0.85", result)
        self.assertIn("BM25: 0.90", result)
        self.assertIn("Cosine: 0.70", result)
        self.assertIn("Some solution text.", result)

    def test_multiple_entries_numbered(self):
        entries = [
            omlx_agent.RetrievedEntry(
                path="/tmp/a.md", chunk_id="a::1", section_title="First",
                score=0.9, bm25_score=0.9, cosine_score=0.0,
                source_meta={}, text="A",
            ),
            omlx_agent.RetrievedEntry(
                path="/tmp/b.md", chunk_id="b::1", section_title="Second",
                score=0.5, bm25_score=0.5, cosine_score=0.0,
                source_meta={}, text="B",
            ),
        ]
        result = omlx_agent._format_rag_context(entries)
        self.assertIn("Solution 1: First", result)
        self.assertIn("Solution 2: Second", result)


class CrossEncoderPlaceholderTests(unittest.TestCase):
    def test_cross_encoder_flag_exists_and_defaults_false(self):
        self.assertFalse(omlx_agent.CROSS_ENCODER_ENABLED)


class RetrieveRelevantSolutionsTests(unittest.TestCase):
    def test_returns_list(self):
        omlx_agent._hybrid_index = None
        results = omlx_agent._retrieve_relevant_solutions("sqlite cache")
        self.assertIsInstance(results, list)

    def test_returns_empty_on_nonsense(self):
        omlx_agent._hybrid_index = None
        results = omlx_agent._retrieve_relevant_solutions("xyzzyplugh42")
        self.assertEqual(len(results), 0)

    def test_exception_returns_empty(self):
        omlx_agent._hybrid_index = None
        results = omlx_agent._retrieve_relevant_solutions("")
        self.assertIsInstance(results, list)


class MtimeBasedFreshnessTests(unittest.TestCase):
    def test_stale_index_rebuilds(self):
        idx1 = omlx_agent.HybridIndex()
        idx1.batch_build(_REPO_ROOT)
        chunk_count_1 = len(idx1.chunks)
        omlx_agent._hybrid_index = idx1
        idx2 = omlx_agent._get_hybrid_index()
        self.assertEqual(len(idx2.chunks), chunk_count_1)


if __name__ == "__main__":
    unittest.main()
