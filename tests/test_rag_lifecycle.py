"""Tests for ONNX embedding session lifecycle integration with model swapping."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class SessionLifecycleTests(unittest.TestCase):
    def test_release_noop_when_no_session(self):
        omlx_agent._embedding_session = None
        omlx_agent._embedding_tokenizer = None
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._embedding_session)
        self.assertIsNone(omlx_agent._embedding_tokenizer)

    def test_release_clears_hybrid_index(self):
        omlx_agent._hybrid_index = omlx_agent.HybridIndex()
        omlx_agent._hybrid_index.batch_build(_REPO_ROOT)
        self.assertTrue(omlx_agent._hybrid_index.is_built)
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._hybrid_index)

    def test_rapid_create_release_cycles(self):
        for _ in range(10):
            omlx_agent._hybrid_index = omlx_agent.HybridIndex()
            omlx_agent._hybrid_index.batch_build(_REPO_ROOT)
            omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._hybrid_index)
        self.assertIsNone(omlx_agent._embedding_session)

    def test_retrieval_works_after_release(self):
        omlx_agent._hybrid_index = None
        omlx_agent._release_embedding_session()
        results = omlx_agent._retrieve_relevant_solutions("sqlite cache")
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    def test_index_rebuilds_after_release(self):
        idx1 = omlx_agent._get_hybrid_index()
        count1 = len(idx1.chunks)
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._hybrid_index)
        idx2 = omlx_agent._get_hybrid_index()
        self.assertEqual(len(idx2.chunks), count1)
        self.assertTrue(idx2.is_built)

    def test_multiple_retrievals_same_session(self):
        omlx_agent._hybrid_index = None
        r1 = omlx_agent._retrieve_relevant_solutions("sqlite cache")
        idx_after_first = omlx_agent._hybrid_index
        r2 = omlx_agent._retrieve_relevant_solutions("completion verifier")
        self.assertIs(omlx_agent._hybrid_index, idx_after_first)
        self.assertIsInstance(r1, list)
        self.assertIsInstance(r2, list)


if __name__ == "__main__":
    unittest.main()
