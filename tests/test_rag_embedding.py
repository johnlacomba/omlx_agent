"""Tests for ONNX embedding layer and lifecycle management."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class EmbeddingFallbackTests(unittest.TestCase):
    """Tests that run regardless of whether ONNX deps are installed."""

    def test_release_noop_when_no_session(self):
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._embedding_session)
        self.assertIsNone(omlx_agent._embedding_tokenizer)

    def test_release_can_be_called_multiple_times(self):
        omlx_agent._release_embedding_session()
        omlx_agent._release_embedding_session()
        omlx_agent._release_embedding_session()

    def test_onnx_available_flag_matches_imports(self):
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
            import numpy  # noqa: F401
            self.assertTrue(omlx_agent._ONNX_AVAILABLE)
        except ImportError:
            self.assertFalse(omlx_agent._ONNX_AVAILABLE)


@unittest.skipUnless(omlx_agent._ONNX_AVAILABLE, "ONNX deps not installed")
class EmbeddingONNXTests(unittest.TestCase):
    """Tests that require onnxruntime + tokenizers + numpy."""

    @classmethod
    def setUpClass(cls):
        omlx_agent._ensure_embedding_model()

    @classmethod
    def tearDownClass(cls):
        omlx_agent._release_embedding_session()

    def test_make_embedding_returns_384_floats(self):
        result = omlx_agent._make_embedding("test query about sqlite caching")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 384)
        self.assertIsInstance(result[0], float)

    def test_similar_texts_high_cosine(self):
        emb1 = omlx_agent._make_embedding("sqlite cache expiration TTL")
        emb2 = omlx_agent._make_embedding("SQLite expiry settings not updating")
        self.assertIsNotNone(emb1)
        self.assertIsNotNone(emb2)
        cosine = sum(a * b for a, b in zip(emb1, emb2))
        self.assertGreater(cosine, 0.5)

    def test_dissimilar_texts_low_cosine(self):
        emb1 = omlx_agent._make_embedding("sqlite cache expiration")
        emb2 = omlx_agent._make_embedding("cooking recipe for pasta carbonara")
        self.assertIsNotNone(emb1)
        self.assertIsNotNone(emb2)
        cosine = sum(a * b for a, b in zip(emb1, emb2))
        self.assertLess(cosine, 0.5)

    def test_empty_string_returns_valid_embedding(self):
        result = omlx_agent._make_embedding("")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 384)

    def test_long_text_truncated_gracefully(self):
        long_text = "word " * 2000
        result = omlx_agent._make_embedding(long_text)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 384)

    def test_embedding_is_l2_normalized(self):
        result = omlx_agent._make_embedding("test normalization")
        self.assertIsNotNone(result)
        norm = sum(x * x for x in result) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_session_lifecycle(self):
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._embedding_session)
        result = omlx_agent._make_embedding("test after release")
        self.assertIsNotNone(result)
        self.assertIsNotNone(omlx_agent._embedding_session)
        omlx_agent._release_embedding_session()
        self.assertIsNone(omlx_agent._embedding_session)


@unittest.skipIf(omlx_agent._ONNX_AVAILABLE, "ONNX deps are installed — testing fallback")
class EmbeddingFallbackOnlyTests(unittest.TestCase):
    """Tests that only run when ONNX deps are NOT installed."""

    def test_make_embedding_returns_none_without_deps(self):
        result = omlx_agent._make_embedding("test without deps")
        self.assertIsNone(result)

    def test_ensure_model_returns_false_without_deps(self):
        self.assertFalse(omlx_agent._ensure_embedding_model())


if __name__ == "__main__":
    unittest.main()
