---
title: "feat: Upgrade Manager to proper hybrid RAG retrieval"
type: refactor
status: active
date: 2026-10-17
deepened: 2026-04-30
---

# feat: Upgrade Manager to proper hybrid RAG retrieval

## Overview

Upgrade the Manager agent's prior-solution retrieval subsystem from a flat keyword search to a proper hybrid RAG pipeline.

The current system (`_retrieve_relevant_solutions()`, ~130 lines at `omlx_agent.py:42-176`) scores documents by unweighted word-overlap and returns a freeform text block. It fails on semantic matches (e.g. `"sqlite TTL cache expiration"` vs `"SQLite expiry settings not updating"`), truncates bodies to 800 chars (losing 90%+ of long docs), drops tokens under 3 characters, and rebuilds the full index on every invocation.

The upgrade produces:
- A prebuilt hybrid retriever (BM25 + dense ONNX vectors)
- ONNX embeddings via `onnxruntime` + `tokenizers` (no PyTorch)
- Optional cross-encoder reranking of top-k (deferred until corpus >50 docs)
- SQLite-persisted embedding cache with mtime-based staleness
- Structured output so the Manager can see scores, confidence, and metadata
- Explicit embedding session lifecycle (unload during specialist phases)

**Est. effort:** 4-5 hours

---

## Problem Frame

Manager uses a rudimentary lexical search — `re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower())` followed by token-set overlap — to surface prior solutions from `docs/solutions/` and `.compound-engineering/learnings.md`. The retrieved docs are dumped as a freeform block into the Manager prompt. This system:

1. **No semantic similarity** — unrelated solutions share vocabulary and relevant ones don't.
2. **No chunking** — the entire file is scored as one unit (truncated to 800 chars), so it can't pick the right section from long docs.
3. **Full rebuild every invocation** — walks the entire `docs/solutions/` tree, reads every file, on every call.
4. **Unstructured output** — the Manager gets a text dump and must parse it to use it.
5. **Drops short tokens** — queries for "db", "UI", "CI" produce zero matches.

---

## Requirements Trace

| # | Requirement |
|---|---|
| R1 | Solution docs are indexed as dense vectors using a local ONNX embedding model — no external API, no PyTorch. |
| R2 | Hybrid retriever combines BM25 (exact keyword) with vector cosine similarity, both normalized to [0,1], reweighted. |
| R3 | Cross-encoder reranker is architecturally supported but deferred until corpus exceeds ~50 docs. |
| R4 | Retrieval output is structured (ranked entries with confidence, metadata) so downstream logic can reason about results. |
| R5 | Index is persisted in SQLite and rebuilt lazily (mtime-based stale detection) so per-call latency is small. |
| R6 | Embedding ONNX session is explicitly unloaded during specialist phases so the memory is freed for LLM inference. |
| R7 | The agent continues to work without ML deps installed — graceful fallback to keyword search. |

---

## Scope Boundaries

**In scope:** The Manager's prior-solution retrieval subsystem. `omlx_agent.py` lines 42-176 are replaced by a well-scoped retrieval module.

**Out of scope:** RAG for specialist phases (work, review, plan), full-doc retrieval, RAG for chat history, external API keys.

### Deferred to Follow-Up Work

- Cross-encoder reranking: architecturally supported in U5 but not wired until corpus exceeds ~50 docs
- Query expansion / synonym normalization: validate on real usage before adding complexity
- Module extraction to `omlx_rag.py`: consider when the RAG code exceeds ~200 lines and `omlx_agent.py` surpasses 8,500 lines

---

## Context & Research

### Existing Code

- `omlx_agent.py` lines 42-176: 6 functions totaling ~135 lines (`_parse_solution_frontmatter`, `_build_solution_index`, `_tokenize_for_matching`, `_score_solution`, `_retrieve_relevant_solutions`, `_format_rag_context`)
- **5 call sites** across TUI and plain-mode paths:
  1. TUI `_start_managed_flow()` (~L7097) — initial flow start
  2. TUI flow-phase completion (~L7312) — refresh after each phase
  3. Plain-mode `/ce:flow` start (~L8126) — initial flow start
  4. Plain-mode flow-phase completion (~L8213) — refresh after each phase
  5. Plain-mode continuation loop (~L8304) — refresh after user responds during flow
- All call sites use the same 2-function interface: `_retrieve_relevant_solutions(objective) -> list[dict]` and `_format_rag_context(list[dict]) -> str`
- Constants: `RAG_MAX_SOLUTION_CHARS = 800`, `RAG_TOP_N = 5`
- Error pattern: `except Exception: return []`, graceful degradation when knowledge base is empty

### Corpus Size

| Source | Count | Size |
|--------|-------|------|
| `docs/solutions/*.md` | 2 files | ~17.5 KB |
| `.compound-engineering/learnings.md` | 2 entries | ~2.2 KB |
| **Total** | **4 entries** | **~19.7 KB** |

Growth path: other projects may have dozens to hundreds of solution docs. The design must scale to ~500+ docs while remaining proportionate for small corpora.

### Platform State

The agent runs as a single file (`omlx_agent.py`, 8,388 lines) with **zero third-party dependencies** — only stdlib. Two optional deps exist behind try/except: `readability` and `bs4` (for web-fetch tool). This plan follows the same pattern: ML deps are optional with graceful fallback.

### SQLite Cache Infrastructure

`~/.omlx/search_cache.db` already exists with per-call connect pattern, idempotent table creation, TTL-based expiry, and BLOB storage. This infrastructure is directly reusable for embedding persistence. A new `embedding_cache` table in the same DB file would store chunk embeddings (384 dims * 4 bytes = 1.5KB per entry; even 1,000 docs = ~3MB).

### Apple Silicon Considerations

All options share unified memory on Apple Silicon. The ONNX CPUExecutionProvider avoids GPU compute contention with oMLX models. The CoreMLExecutionProvider (routing to ANE) was evaluated but adds first-run compilation overhead (1-5 seconds) that isn't worthwhile for a ~22MB model where CPU inference takes 5-10ms. MLX was evaluated but has immature embedding support and runs on GPU (competing with LLM inference).

### Institutional Learnings

- Solution doc `no-api-web-research-2026-04-25.md`: established the pattern for optional deps with graceful fallback, URL validation, SQLite caching, and TTL-based expiry. The embedding cache should follow the same patterns.
- Solution doc `completion-verifier-default-to-complete-2026-04-23.md`: established that structured output is more reliable than freeform text for downstream reasoning — supports R4 (structured retrieval output).

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Embedding model | `all-MiniLM-L6-v2` ONNX quantized (~22 MB, 384-dim output) | Well-aligned for code/problem domains. Widely adopted. Note: the model outputs raw `last_hidden_state` — mean pooling + L2 normalization must be applied in application code to produce sentence embeddings. |
| ONNX model variant | `onnx/model_qint8_arm64.onnx` for Apple Silicon | The repo contains 9 ONNX variants (90MB unquantized, 22MB quantized per-arch). The ARM64 int8 variant is correct for Apple Silicon. Download URL: `https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_arm64.onnx`. Also download `tokenizer.json` from repo root. |
| ONNX model delivery | Download on first run, cache at `~/.omlx/embedding_cache/` | Keeps repo lean; first run is one-time. |
| Dependency chain | `pip install onnxruntime tokenizers numpy` | **No PyTorch.** sentence-transformers was rejected because it transitively pulls torch (~2GB install, 150-300MB permanent RSS on import, 2-4 second import time). The `tokenizers` package (3.1MB, Rust-based) provides the same WordPiece tokenizer directly. Note: `tokenizers` pulls `huggingface-hub` and ~14 transitive deps — still far lighter than PyTorch but more than 3 packages. |
| Install environment | Recommend venv; document `--break-system-packages` as fallback | macOS Homebrew Python 3.13 is PEP 668 externally-managed. `pip install` fails without isolation. README documents both options. |
| Execution provider | `CPUExecutionProvider` | Avoids GPU compute contention with oMLX LLM models. CoreML EP evaluated but first-run compilation overhead not worthwhile for this model size. |
| Fallback | If deps not installed, fall back to existing `_retrieve_relevant_solutions` | No outage from missing ML deps; user sees `[RAG] keyword fallback`. Follows established readability/bs4 try/except pattern. |
| Embedding lifecycle | Session created before retrieval, destroyed after. Explicitly released when manager unloads for specialist phase. | The ONNX InferenceSession must not persist across the manager-to-specialist transition. When `unload_omlx_model(manager_model)` is called, the ONNX session is also released. This preserves the "one model at a time" principle. ~100-200ms cold-start per retrieval is acceptable (retrieval runs 3-8 times per workflow). |
| Hybrid weight | `0.7 * bm25 + 0.3 * cosine` (configurable via `RAG_ALPHA` env var) | BM25 anchors on exact match; cosine adds semantic signal. Tunable empirically. |
| Chunking | Split by `##` headers into chunks; each chunk is ~2-4 paragraphs | Keeps semantically coherent units for embedding. Replaces the 800-char truncation. |
| Cross-encoder | Architecturally supported (placeholder in hybrid pipeline) but **not wired until corpus >50 docs** | At current corpus sizes, reranking top-5 from a pool of 4-50 adds no measurable quality. The code path exists but the model is not downloaded or loaded. When corpus grows, enable with `cross-encoder/ms-marco-MiniLM-L-6-v2` ONNX (~25MB). |
| Output format | `RetrievedEntry` dataclass with path, score, chunk_id, score_breakdown, source_meta | Structured, not freeform. Downstream logic can filter by confidence. |
| Index persistence | SQLite table `embedding_cache` in `~/.omlx/search_cache.db` | Reuses existing cache infrastructure. Mtime-based staleness: if source file mtime > cached mtime, re-embed that file's chunks. Avoids re-embedding unchanged docs on every session start. |
| Module location | Inline in `omlx_agent.py` initially; extract to `omlx_rag.py` if RAG code exceeds ~200 lines | The file is already 8,388 lines. Avoid premature extraction but plan for it. |

---

## Open Questions

### Resolved During Planning

- **Dependency strategy?** `onnxruntime` + `tokenizers` + `numpy` (plus ~14 transitive deps from `tokenizers` via `huggingface-hub` — still far lighter than PyTorch's ~2GB). If user opts out, fall back to keyword search. No PyTorch in the dependency chain.
- **Model bundles vs. runtime download?** Runtime download. 22 MB is small for a one-time fetch.
- **Single-file vs. module?** Inline initially. Extract when RAG code exceeds ~200 lines.
- **What does the Manager receive now vs. after?** Currently a text dump from `_format_rag_context()`. After: structured `RetrievedEntry` objects rendered into a scored, annotated format.
- **PyTorch vs. no-PyTorch?** No PyTorch. `sentence-transformers` pulls torch (~2GB install, 150-300MB permanent RSS, 2-4s import). The `tokenizers` package provides the same WordPiece tokenizer at 3.1MB. ONNX Runtime handles inference.
- **Apple Silicon optimization?** CPUExecutionProvider is sufficient. CoreML EP adds cold-start overhead for minimal gain. MLX has immature embedding support and competes for GPU. All options share unified memory; CPU avoids GPU compute contention.
- **Embedding model lifecycle?** Session created per-retrieval, destroyed after. Explicitly released alongside manager model unload. ~100-200ms cold-start is acceptable for 3-8 retrievals per workflow.

### Deferred to Implementation

- Exact BM25 weights / alpha tuning. Start with 0.7 / 0.3, adjust empirically.
- Whether to chunk by headings, by token count, or by paragraph. Validate on real docs.
- Cross-encoder activation threshold (currently set at >50 docs but may need tuning).
- Module extraction timing — monitor file length during implementation.

---

## Implementation Units

<!-- U1 was a design doc unit in the original plan. Its decisions have been folded into
     Key Technical Decisions and Open Questions above. U-IDs are stable and never renumbered. -->

- U2. **Implement the HybridIndex data model and streaming builder**

**Goal:** Create the `HybridIndex` class that represents the per-session index, plus a streaming `_stream_solutions_for_index` generator that yields individual document chunks without loading whole files into memory.

**Requirements:** R5

**Dependencies:** None

**Files:**
- Modify: `omlx_agent.py` — add `HybridIndex` class and `_stream_solutions_for_index` function
- Test: `tests/test_rag_streaming.py`

**Approach:**
1. Define `DocumentChunk` dataclass with fields: `text`, `path`, `chunk_id`, `section_title`, `source_meta`
2. Build `_stream_solutions_for_index` to yield chunks via a generator, splitting by `##` headers
3. `HybridIndex` exposes `add_chunk(chunk)` and `batch_build()` for caching
4. Include SQLite persistence: `embedding_cache` table in `~/.omlx/search_cache.db` with columns `chunk_id`, `doc_path`, `doc_mtime`, `section_title`, `chunk_text`, `embedding_blob`

**Patterns to follow:**
- `CEWorkflowState` dataclass naming convention
- Existing `_build_solution_index` walk-and-parse logic as reference
- `_init_search_cache()` idempotent table creation pattern
- `except Exception: return []` error pattern

**Test scenarios:**
- Happy path: `_stream_solutions_for_index("docs/solutions")` yields chunks from solution files, split by `##` headers
- Happy path: `HybridIndex.batch_build()` returns a dict with chunk entries matching document count
- Edge case: empty `docs/solutions/` directory returns no chunks
- Edge case: solution file with no `##` headers produces a single chunk for the whole file
- Error path: unreadable file is skipped (no crash), remaining files still indexed
- Integration: output chunks have all required fields (`text`, `path`, `chunk_id`, `section_title`, `source_meta`)
- Integration: SQLite `embedding_cache` table created on first build, entries match indexed chunks

**Verification:**
- Streaming builder works on current knowledge base
- `HybridIndex` holds indexed chunks in memory and persists to SQLite
- Empty doc tree returns empty index (no crash)
- Chunks are split at `##` boundaries with correct section titles

---

- U3. **Implement the ONNX embedding layer with lifecycle management**

**Goal:** A self-contained `_make_embedding(text: str) -> list[float]` function backed by ONNX MiniLM using `onnxruntime` + `tokenizers` (no PyTorch), with explicit session lifecycle tied to the manager model's load/unload cycle.

**Requirements:** R1, R6, R7

**Dependencies:** U2

**Files:**
- Modify: `omlx_agent.py` — add `_ensure_embedding_model()`, `_make_embedding()`, `_release_embedding_session()`
- Test: `tests/test_rag_embedding.py`

**Approach:**
1. `_ensure_embedding_model()` checks `~/.omlx/embedding_cache/` for downloaded ONNX model + `tokenizer.json`. Downloads if missing (one-time, ~22MB).
2. Load ONNX model with `onnxruntime.InferenceSession(providers=["CPUExecutionProvider"])`.
3. Load tokenizer via `tokenizers.Tokenizer.from_file("tokenizer.json")` — the `tokenizers` package (3.1MB) provides the same fast WordPiece tokenizer as sentence-transformers without pulling PyTorch.
4. `_make_embedding()` tokenizes input, runs `InferenceSession.run()` to get raw `last_hidden_state` (shape: `batch, seq_len, 384`), then applies mean pooling using the attention mask and L2 normalization to produce a 384-dim sentence embedding vector. This post-processing is mandatory — the ONNX model only contains the transformer, not the pooling/normalize stages.
5. `_release_embedding_session()` deletes the InferenceSession and tokenizer references so memory is freed. Called when `unload_omlx_model(manager_model)` fires.
6. Wrap all imports in `try/except ImportError` — if `onnxruntime` or `tokenizers` not installed, set a module-level flag and fall back to keyword search.

**Patterns to follow:**
- `readability`/`bs4` try/except import pattern (existing in omlx_agent.py lines 478-499)
- File operations use standard `os.path`
- `except Exception` with `return None` for graceful degradation

**Test scenarios:**
- Happy path: `_make_embedding("test query about sqlite caching")` returns a list of 384 floats
- Happy path: two semantically similar texts produce cosine similarity > 0.7
- Edge case: empty string returns a zero vector (or a valid embedding, not a crash)
- Edge case: very long text (>512 tokens) is truncated gracefully by the tokenizer
- Error path: `_make_embedding` returns `None` when onnxruntime is not installed (fallback flag set)
- Error path: corrupted model file triggers fallback, not crash
- Integration: `_release_embedding_session()` frees the InferenceSession (memory decreases)
- Unit: vector dimension is exactly 384

**Verification:**
- Similar queries produce similar embeddings (cosine > 0.7)
- Dissimilar queries produce low similarity (cosine < 0.3)
- Session can be created, used, and destroyed multiple times without leaks
- Graceful fallback when onnxruntime is not installed
- Cold-start (create session) takes <500ms on Apple Silicon

---

- U4. **Implement the BM25 retriever**

**Goal:** A lightweight BM25 retriever that scores text chunks on exact keyword match, returning a BM25 score per chunk, normalized to [0,1].

**Requirements:** R2

**Dependencies:** U2

**Files:**
- Modify: `omlx_agent.py` — add `_build_bm25_index()` and `_bm25_retrieve(query, top_k)`
- Test: `tests/test_rag_bm25.py`

**Approach:**
1. Implement Okapi BM25 scoring using only `math.log` and `collections.Counter` (stdlib, no numpy required)
2. `_build_bm25_index()` tokenizes chunks (improved tokenizer that handles short tokens like "db", "CI"), builds per-chunk term-frequency index and document-frequency counts
3. `_bm25_retrieve()` runs BM25 scoring against a query, returns ranked list
4. Scores normalized to [0,1] by dividing by the maximum score in the result set (or 1.0 if empty)

**Patterns to follow:**
- Existing `_tokenize_for_matching` as starting point (but fix the 3-char minimum)
- Same style as stdlib-only search cache functions

**Test scenarios:**
- Happy path: query "sqlite cache" returns the web-research solution doc at top
- Happy path: query "completion verifier strict" returns the completion-verifier solution at top
- Edge case: query with no matching terms returns empty list
- Edge case: single-character tokens like "db" and "UI" are indexed and matchable
- Edge case: query matching all docs returns all with differentiated scores
- Integration: BM25 scores are in [0,1] range
- Unit: BM25 handles Unicode and mixed-case text correctly
- Unit: IDF weighting correctly downweights common terms ("problem", "solution")

**Verification:**
- Relevant docs rank above irrelevant ones for known queries
- Empty query returns nothing (no crash)
- Fast: <10ms on 100 chunks

---

- U5. **Wire up hybrid retrieval with structured output**

**Goal:** Implement the end-to-end pipeline: query → retrieve via BM25 → retrieve via embedding → combine scores → structured output. Replace the existing `_retrieve_relevant_solutions()` and `_format_rag_context()`.

**Requirements:** R1–R5, R7

**Dependencies:** U3, U4

**Files:**
- Modify: `omlx_agent.py` — replace `_retrieve_relevant_solutions()` and `_format_rag_context()` with hybrid versions
- Modify: `MANAGER_SYSTEM_PROMPT` to describe the new structured RAG context format
- Remove: old `_build_solution_index()`, `_tokenize_for_matching()`, `_score_solution()`, `_parse_solution_frontmatter()` (superseded)
- Remove: `RAG_MAX_SOLUTION_CHARS` and `RAG_TOP_N` constants (no longer needed)
- Test: `tests/test_rag_hybrid.py`

**Approach:**
1. `hybrid_retrieve(query, top_k)` merges BM25 + embedding results using the `RAG_ALPHA` weight (default 0.7 BM25 / 0.3 cosine)
2. `RetrievedEntry` dataclass with: `path`, `score`, `chunk_id`, `section_title`, `bm25_score`, `cosine_score`, `source_meta`
3. New `_format_rag_context()` renders `RetrievedEntry` list into structured text with scores and metadata
4. Manager prompt updated to describe the new format and how to interpret confidence scores
5. Cross-encoder placeholder: the pipeline has a reranking slot that is a no-op until activated. When `CROSS_ENCODER_ENABLED` flag is set and model is present, it reranks top-k before returning
6. Mtime-based index freshness: on each retrieval call, check if any source file's mtime is newer than its cached embeddings. Re-embed only changed files.
7. Fallback path: if ONNX deps not available, `hybrid_retrieve` falls back to keyword-only BM25 (still an improvement over the current set-overlap scorer)

**Patterns to follow:**
- Existing `_workflow_artifact_snapshot()` for structured output consumed by the manager
- Existing call sites use `_retrieve_relevant_solutions(objective) -> list[dict]` — maintain compatible signature
- TUI and plain-mode paths must both work (5 call sites)
- `except Exception: return []` fallback

**Test scenarios:**
- Happy path: hybrid retrieval returns the completion-verifier doc when queried with "verifier too strict" (semantic match via embedding)
- Happy path: hybrid retrieval returns the web-research doc when queried with "sqlite cache TTL" (exact keyword match via BM25)
- Edge case: query with zero BM25 matches but semantic similarity still returns results (embedding-only)
- Edge case: query with zero embedding similarity but keyword matches still returns results (BM25-only)
- Edge case: unknown query returns empty list, not crash
- Edge case: empty corpus returns empty list gracefully
- Error path: ONNX deps missing → falls back to BM25-only retrieval (still works)
- Integration: output `RetrievedEntry` objects have all required fields
- Integration: `_format_rag_context` produces valid text with score annotations
- Unit: `RAG_ALPHA=1.0` produces BM25-only results; `RAG_ALPHA=0.0` produces embedding-only results
- Unit: mtime check correctly identifies stale embeddings
- Unit: cross-encoder placeholder slot exists in the pipeline; with `CROSS_ENCODER_ENABLED=False` (default), reranking is a no-op passthrough

**Verification:**
- Hybrid retrieval produces plausible, ranked results with score breakdowns
- New structured output replaces old freeform text in all 5 call sites
- Index rebuilds only changed docs (mtime-based)
- Empty corpus returns empty result (no crash)
- Fallback to BM25-only when ONNX not installed works correctly

---

- U6. **Integrate embedding lifecycle with manager model swapping**

**Goal:** Wire the ONNX embedding session into the existing model load/unload lifecycle so it is explicitly released when the manager model unloads for a specialist phase, and re-created when the manager reloads.

**Requirements:** R6

**Dependencies:** U3, U5

**Files:**
- Modify: `omlx_agent.py` — update `_run_phase_from_flow()` (~L7218-7226) and the equivalent plain-mode paths to call `_release_embedding_session()` alongside `unload_omlx_model()`
- Modify: `omlx_agent.py` — update `_run_workflow_manager_turn()` to ensure embedding session is available before retrieval
- Test: `tests/test_rag_lifecycle.py`

**Approach:**
1. In `_run_phase_from_flow()`, after `unload_omlx_model(manager_model)`, also call `_release_embedding_session()` to free the ONNX session memory
2. In `_run_workflow_manager_turn()`, the retrieval call in the post-phase message builder will call `_ensure_embedding_model()` internally (lazy init)
3. The lifecycle is: manager loads → retrieval runs (embedding session created lazily) → manager makes decision → manager unloads + embedding session released → specialist loads and uses full RAM → specialist completes → manager reloads → retrieval runs again (embedding session re-created)
4. Apply the same pattern in all 3 plain-mode paths (~L8126, ~L8213, ~L8304)
5. ~100-200ms cold-start per retrieval is acceptable (3-8 times per workflow run)

**Patterns to follow:**
- Existing `unload_omlx_model()` call pattern at line 7224-7226
- Manager model check: `if self._current_model_name == manager_model and manager_model != target_model`
- TUI and plain-mode paths must both be updated

**Test scenarios:**
- Happy path: embedding session is created on first retrieval call, persists across multiple retrievals within a manager turn
- Happy path: embedding session is released when manager unloads for specialist phase
- Happy path: embedding session is re-created when manager reloads for next turn
- Edge case: calling `_release_embedding_session()` when no session exists is a no-op
- Edge case: rapid create/release cycles don't leak memory
- Integration: full flow (start → phase 1 → phase 2 → complete) shows correct session lifecycle via logging
- Integration: memory usage decreases after `_release_embedding_session()` call

**Verification:**
- No ONNX session persists during specialist phases
- Retrieval works correctly after session re-creation
- Memory is freed between phases (observable via process RSS)
- Plain-mode and TUI paths both release correctly

---

## System-Wide Impact

- **Interaction graph:** All 5 call sites that use `_retrieve_relevant_solutions()` / `_format_rag_context()` — TUI `_start_managed_flow` (~L7097), TUI flow-phase completion (~L7312), plain-mode `/ce:flow` start (~L8126), plain-mode flow-phase completion (~L8213), plain-mode continuation loop (~L8304).
- **Error propagation:** Three-tier fallback: (1) hybrid RAG if ONNX deps installed, (2) BM25-only if ONNX deps missing, (3) empty result if no docs exist. All tiers use `except Exception: return []`.
- **State lifecycle risks:** ONNX InferenceSession must be released during specialist phases to avoid memory competition. SQLite embedding cache must handle concurrent writes safely (SQLite's default serialized mode is sufficient for single-process use).
- **Unchanged invariants:** The Manager prompt still receives a `"[Retrieved prior solutions]"` block. The 5 call sites continue to use the same 2-function interface. The oMLX model swap lifecycle is preserved — embedding session lifecycle is additive, not modifying.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| ONNX model download fails (no internet) | Three-tier fallback: BM25-only if deps installed but model missing, keyword search if deps not installed |
| `onnxruntime` not available on user's Python | try/except import with module-level flag, same pattern as readability/bs4 |
| Embedding session memory leak | Explicit `_release_embedding_session()` called alongside `unload_omlx_model()`. Test with RSS monitoring. |
| Embedding cold-start too slow | ~100-200ms per session creation on Apple Silicon. 3-8 times per workflow = 0.3-1.6 seconds total. Acceptable. |
| SQLite embedding cache corruption | Idempotent table creation. Cache is disposable — delete and rebuild. Follow existing `search_cache.db` patterns. |
| First-run latency: download + initial embed | ~22MB download + embedding 4 docs at ~10ms each = <30 seconds first time. Subsequent sessions: mtime check + cached embeddings = <100ms. |
| BM25-only fallback quality | BM25 with TF-IDF weighting is already a significant upgrade over the current set-overlap scorer. The semantic gap only matters for vocabulary-disjoint queries. |
| File exceeds 8,500 lines | Monitor during implementation. Extract to `omlx_rag.py` if RAG code exceeds ~200 lines. |

---

## Documentation / Operational Notes

- README updated with optional dependency install instructions — recommend venv setup (`python3 -m venv .venv && source .venv/bin/activate && pip install onnxruntime tokenizers numpy`), document `pip install --break-system-packages onnxruntime tokenizers numpy` as fallback for users who prefer not to use a venv
- Note PEP 668 externally-managed environment: macOS Homebrew Python 3.13 requires either venv or `--break-system-packages` flag
- Document `~/.omlx/embedding_cache/` location (ONNX model files) and `~/.omlx/search_cache.db` (embedding cache table)
- The structured output format is the primary contract between the retriever and the Manager
- Note that `sentence-transformers` is NOT needed — it pulls PyTorch (~2GB). The `tokenizers` package provides the same WordPiece tokenizer

---

## Sources & References

- Existing retrieval code: `omlx_agent.py` lines 42-176
- Model swap lifecycle: `omlx_agent.py` lines 4499-4527 (`unload_omlx_model`), 5718-5738 (`_ensure_model`)
- SQLite cache infrastructure: `omlx_agent.py` lines 204-308
- `MANAGER_SYSTEM_PROMPT`: system prompt with RAG context instructions
- Apple Silicon inference research: ONNX Runtime CPUExecutionProvider avoids GPU compute contention; CoreML EP and MLX evaluated but not selected
- [ONNX Runtime PyPI](https://pypi.org/project/onnxruntime/) (~18MB)
- [tokenizers PyPI](https://pypi.org/project/tokenizers/) (~3.1MB, Rust-based)
- [all-MiniLM-L6-v2 on HuggingFace](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
