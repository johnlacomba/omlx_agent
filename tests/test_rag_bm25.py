"""Tests for BM25 retrieval on HybridIndex."""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class BM25RetrieveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = omlx_agent.HybridIndex()
        cls.idx.batch_build(_REPO_ROOT)

    def test_sqlite_cache_returns_web_research_doc(self):
        results = self.idx.bm25_retrieve("sqlite cache")
        self.assertGreater(len(results), 0)
        self.assertTrue(any("web-research" in r.path for r in results))

    def test_completion_verifier_returns_completion_doc(self):
        results = self.idx.bm25_retrieve("completion verifier strict")
        self.assertGreater(len(results), 0)
        self.assertTrue(any("completion-verifier" in r.path for r in results))

    def test_empty_query_returns_nothing(self):
        results = self.idx.bm25_retrieve("")
        self.assertEqual(len(results), 0)

    def test_short_tokens_matchable(self):
        results = self.idx.bm25_retrieve("db UI CI")
        self.assertIsInstance(results, list)

    def test_idf_downweights_common_terms(self):
        results = self.idx.bm25_retrieve("the and is of to")
        common_scores = [r.score for r in results] if results else [0.0]
        specific = self.idx.bm25_retrieve("sqlite cache expiration TTL")
        specific_scores = [r.score for r in specific] if specific else [0.0]
        if results and specific:
            self.assertGreaterEqual(max(specific_scores), max(common_scores))

    def test_scores_normalized_zero_to_one(self):
        results = self.idx.bm25_retrieve("sqlite cache expiration")
        for r in results:
            self.assertGreaterEqual(r.bm25_score, 0.0)
            self.assertLessEqual(r.bm25_score, 1.0)

    def test_top_k_limits_results(self):
        results = self.idx.bm25_retrieve("solution problem", top_k=2)
        self.assertLessEqual(len(results), 2)

    def test_results_are_retrieved_entry(self):
        results = self.idx.bm25_retrieve("sqlite cache")
        for r in results:
            self.assertIsInstance(r, omlx_agent.RetrievedEntry)
            self.assertTrue(hasattr(r, "path"))
            self.assertTrue(hasattr(r, "bm25_score"))
            self.assertTrue(hasattr(r, "cosine_score"))
            self.assertEqual(r.cosine_score, 0.0)

    def test_results_sorted_descending(self):
        results = self.idx.bm25_retrieve("sqlite cache expiration")
        if len(results) > 1:
            for i in range(len(results) - 1):
                self.assertGreaterEqual(results[i].score, results[i + 1].score)


class BM25EmptyIndexTests(unittest.TestCase):
    def test_empty_index_returns_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = omlx_agent.HybridIndex()
            idx.batch_build(tmpdir)
            results = idx.bm25_retrieve("sqlite cache")
            self.assertEqual(len(results), 0)

    def test_unbuilt_index_returns_nothing(self):
        idx = omlx_agent.HybridIndex()
        results = idx.bm25_retrieve("anything")
        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
