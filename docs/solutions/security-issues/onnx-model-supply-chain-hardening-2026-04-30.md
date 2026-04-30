---
title: ONNX Model Supply-Chain and Download Reliability Hardening
date: 2026-04-30
category: security-issues
module: hybrid-rag
problem_type: security_issue
component: tooling
symptoms:
  - ONNX model downloaded from mutable HuggingFace URL without SHA-256 hash verification
  - Partial download leaves corrupt file that permanently blocks embedding initialization
  - All RAG retrieval failures silently swallowed with bare except returning empty list
  - Type annotations missing on RAG pipeline functions hiding contract from static analysis
root_cause: missing_validation
resolution_type: code_fix
severity: critical
tags:
  - onnx
  - supply-chain
  - sha256
  - atomic-download
  - embedding
  - rag
  - huggingface
  - model-integrity
---

# ONNX Model Supply-Chain and Download Reliability Hardening

## Problem

The hybrid RAG system downloads an ONNX embedding model (~23MB) and tokenizer from HuggingFace on first use. Three critical issues were identified during code review: no integrity verification on downloaded files (supply-chain code execution risk via malicious ONNX custom ops), no atomic write pattern (partial downloads permanently block embedding loading), and silent error swallowing that hides all RAG failures from operators.

## Symptoms

- ONNX model fetched from `/resolve/main/` URL which tracks the latest mutable commit
- `onnxruntime.InferenceSession` loads any file at the cached path without verification
- Interrupted download leaves a partial `.onnx` file that passes `os.path.isfile()` check on next startup, causing `InferenceSession` to fail on corrupt data
- The except handler clears `_embedding_session` but does not delete the corrupt file, creating a permanent failure loop
- `_retrieve_relevant_solutions` catches all exceptions with `except Exception: return []`, making broken RAG indistinguishable from empty results

## What Didn't Work

- The initial implementation relied on HTTPS transport security alone for model integrity, which does not protect against compromised CDN, repo hijack, or cached file replacement
- Writing directly to the final file path during download meant any interruption left an unusable file at the trusted location
- The broad `except` was intended as a graceful degradation, but it also swallowed programming errors, disk-full conditions, and permission failures

## Solution

**1. SHA-256 pinned verification (SEC-001, SEC-002):**

```python
_ONNX_MODEL_COMMIT = "c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
_ONNX_MODEL_URL = f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_ONNX_MODEL_COMMIT}/onnx/model_qint8_arm64.onnx"
_ONNX_MODEL_SHA256 = "4278337fd0ff3c68bfb6291042cad8ab363e1d9fbc43dcb499fe91c871902474"
_TOKENIZER_SHA256 = "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"

def _verify_file_sha256(path: str, expected_hash: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest() == expected_hash
```

URL pinned to a specific commit (not `/resolve/main/`). Hash verified on every load, not just on download. Existing cached files with wrong hash trigger re-download.

**2. Atomic download pattern (REL-01, ADV-002, REL-03):**

```python
def _download_verified(url, dest_path, expected_hash, timeout=120):
    tmp_path = dest_path + ".tmp"
    try:
        # Download to temporary file
        req = urllib.request.Request(url, headers={"User-Agent": REALISTIC_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        # Verify before promoting
        if not _verify_file_sha256(tmp_path, expected_hash):
            raise ValueError(f"SHA-256 mismatch for {os.path.basename(dest_path)}")
        os.replace(tmp_path, dest_path)  # atomic on POSIX
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
```

**3. Self-healing on corrupt files:**

In the except handler of `_ensure_embedding_model`, corrupt files (hash mismatch) are deleted so the next invocation triggers a fresh download instead of permanently failing.

**4. Error logging (REL-02, KP-004):**

```python
def _retrieve_relevant_solutions(objective, top_n=5):
    try:
        idx = _get_hybrid_index()
        return idx.hybrid_retrieve(objective, top_k=top_n)
    except Exception as exc:
        import traceback
        sys.stderr.write(f"[RAG] retrieval failed: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        return []
```

**5. Type annotations (KP-001, KP-002, KP-005):**

All RAG pipeline functions annotated with concrete `list[RetrievedEntry]` return types. Parameter types fixed (`str | None` where `None` default was used). `batch_build` return changed from `-> dict` to `-> None` since the return value was never consumed.

## Why This Works

- **SHA-256 pinning** creates a tamper-evident seal: even if the upstream repo is compromised, the agent will refuse to load a model whose digest doesn't match the hardcoded value
- **Commit-pinned URLs** prevent silent upstream changes from being consumed automatically
- **Atomic write via `os.replace`** is a POSIX atomic operation — the file at the destination path is either the complete verified download or absent, never partial
- **Self-healing deletion** breaks the stuck-failure loop where a corrupt cached file blocks every subsequent startup
- **Explicit logging** makes RAG failures visible to operators while still degrading gracefully to BM25-only retrieval

## Prevention

- When downloading any external model or executable file, always verify against a pinned hash before loading
- Use the atomic download pattern (write to `.tmp`, verify, then `os.replace`) for any file that will be cached and reused across sessions
- Never write a bare `except: return default` without at minimum logging the exception — silent swallowing makes production issues invisible
- Pin dependency URLs to immutable references (commit SHAs, tagged releases) rather than mutable branches

## Related Issues

- [docs/solutions/tooling-decisions/no-api-web-research-2026-04-25.md](../tooling-decisions/no-api-web-research-2026-04-25.md) — related tooling decision for the same omlx_agent project
