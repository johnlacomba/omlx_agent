---
title: Proactive Context Management
type: feat
status: active
date: 2026-04-23
origin: docs/brainstorms/2026-04-23-proactive-context-management-requirements.md
---

# Proactive Context Management

## Overview

Add a proactive context-management system to `omlx_agent.py` that monitors
per-layer prompt-token usage, triggers a structured *checkpoint and
rehydrate* cycle before the model's window is exhausted, archives the
pre-wipe context to a tag-indexed on-disk store, and exposes a recall
tool so the model can pull back specific earlier slices on demand. Falls
back to the existing `emergency_trim` if the checkpoint operation itself
fails.

---

## Problem Frame

Today the agent only handles overflow reactively: oMLX returns HTTP 400
"Prompt too long", and `emergency_trim()` (omlx_agent.py around line
2535) drops the 20 oldest non-system messages. There is no proactive
monitoring, no preservation of dropped content, and no retrieval. Long
autonomous runs (notably `/ce:flow`) hit this wall repeatedly and lose
information that mattered to the active task — including, critically,
the record of which approaches were already tried and rejected. See
origin: docs/brainstorms/2026-04-23-proactive-context-management-requirements.md.

---

## Requirements Trace

- R1. Monitor per-layer `prompt_tokens` against each model's context window (origin FR2).
- R2. Trigger a structured checkpoint cycle at 60% with a clean-boundary guard and a pre-flight headroom check (origin FR3).
- R3. Generate a `CONTEXT_SNAPSHOT v1` document filling all required fields, with mandatory "Ruled Out / Don't Retry" (origin FR4).
- R4. Rebuild the layer's message list to: system prompt + first user prompt of the contextID + snapshot + tail snippet (origin FR5).
- R5. Archive each pre-wipe message list to `.currentContext/${date}/${contextID}/dump-${HHMMSS}.md` with section anchors and per-section tag headers (origin FR6).
- R6. Maintain `.currentContext/contextTags.md` master taxonomy and a contextID-scoped `index.md` mapping `tag -> [{file, anchor}]` (origin FR6, FR7).
- R7. Tag at the section level; require existing tags from the master list when applicable; record additions explicitly (origin FR7).
- R8. Run a tag normalization pass at the start of each new user prompt; merge near-duplicates; rewrite affected `index.md` references (origin FR7).
- R9. Mint a new contextID only on TUI-input-box user prompts; manager-generated messages do not mint new IDs (origin FR1).
- R10. Expose a model-side `recall_context(tags, contextID, limit)` tool to all non-manager layers, returning section excerpts capped to a per-call token budget (origin FR8).
- R11. On snapshot failure, log to `~/.omlx/last_dump_failure.json` and fall back to `emergency_trim` (origin FR9).
- R12. Surface dump events on the TUI activity line and add a `/context` slash command for per-layer usage (origin FR10).

---

## Scope Boundaries

- Cross-session restoration of a prior contextID is out of scope.
- No vector embeddings or semantic search — recall is exact-tag match only.
- `emergency_trim` is not removed; it remains the last-resort fallback.
- No compression of tool outputs beyond the existing 12KB read cap and 8KB command-output cap.
- No changes to the manager / brainstorm / plan / work / review / compound layer split.
- No changes to the `ce_run_agent` sub-agent context isolation model.

### Deferred to Follow-Up Work

- Recall ranking smarter than recency (e.g., tag-overlap scoring) — separate iteration once usage data exists.
- Auto-pruning of old `.currentContext/${date}` directories — separate housekeeping pass.
- Exposing `recall_context` to vendored sub-agents spawned via `ce_run_agent` — initial scope is the primary layer message lists only.

---

## Context & Research

### Relevant Code and Patterns

- Reactive trim: `emergency_trim()` and the `_context_overflow` short-circuit in [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L2535-L2570).
- Per-call API: `api_call(messages, model)` in [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L1980-L2050).
- Token reading: `usage.prompt_tokens` already read at [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L4737).
- Per-layer model selection: `MODEL_GROUPS` and `CE_MODE_TO_GROUP` in [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L3265-L3300) (approx).
- Tool registration: `TOOLS` list and `TOOL_DISPATCH` map in [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L40) onward; new tools follow the same pattern as `ce_scan_solution_headers`.
- Slash commands: `_handle_input` in [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L4215) and the no-TUI path around line 5273 — mirror both surfaces for `/context`.
- Tail-snippet boundary safety: tool-call/tool-response pairing is required by the OpenAI message format and already implicit in how `tool_calls` and `role:"tool"` messages are appended (see structured tool-call append in `_agent_turn_tui`).
- Prior tag-extraction pattern for solutions: `_extract_tags()` and `_update_temp_indexes()` already parse YAML-style and `**tags:**` markdown — reuse the parsing approach for section tag headers.

### Institutional Learnings

- Existing learnings live at `.compound-engineering/learnings.md`; check before implementation for any prior context-management notes (none expected based on origin's verification step).

### External References

- None required. The design is self-contained and uses the standard library only, matching the project's no-pip-dependencies constraint.

---

## Key Technical Decisions

- **Snapshot role:** inject as a `system` message. Local LLMs in this stack respect system messages most consistently for "ground truth" framing. Resolves an origin open question.
- **Section granularity:** one section per role-coherent message-pair (the assistant turn plus any preceding user/tool messages it answered), with a fallback split when a single message exceeds 2000 tokens. This keeps tag precision high without exploding anchor counts.
- **`contextID` format:** `${HHMMSS}-${4-hex}` derived from `os.urandom(2).hex()`. Sortable within a day, collision-resistant, short enough to fit in path segments.
- **Trigger threshold:** 60% with a pre-flight estimate (snapshot cost approximated as `len(messages) * 64 tokens` plus a 4096-token reserve for the model's snapshot output). Skip trigger if the estimate would push past 90%.
- **Per-call recall budget:** 4096 tokens of excerpt per `recall_context` call, distributed across returned hits.
- **`MODEL_CONTEXT_LIMITS`:** new module-level dict with conservative defaults (16384 tokens) and explicit overrides for known models. CLI flag `--context-limit MODEL=N` allows per-run override; persisted in `~/.omlx/model_context_limits.json` for re-use.
- **Normalization pass placement:** synchronous at the start of each new user prompt. Latency hit is small (file scan + diff), and synchronous keeps the failure mode simple. Resolves an origin open question.
- **Manager layer recall:** **not** exposed. Manager is JSON-only and must not call tools (existing rule). The manager gets *checkpoints* but not `recall_context`. Resolves an origin open question.
- **Active contextID scope:** module-level `_active_context_id` plus a per-layer dict `_layer_context_state[layer] = {contextID, dump_count, first_user_msg_idx}`. Layer name keys mirror `MODEL_GROUPS`.
- **Disk I/O posture:** snapshot write happens on the agent worker thread, not the TUI thread. The TUI shows a `dumping context...` activity tag while it runs.

---

## Open Questions

### Resolved During Planning

- Snapshot role (system vs assistant): system. See Key Technical Decisions.
- Section granularity: per role-coherent pair, with 2000-token split fallback. See Key Technical Decisions.
- Normalization synchronous vs background: synchronous. See Key Technical Decisions.
- Whether manager gets `recall_context`: no. See Key Technical Decisions.
- Default `MODEL_CONTEXT_LIMITS`: 16384 fallback with override map. See Key Technical Decisions.
- Per-call recall token budget: 4096. See Key Technical Decisions.

### Deferred to Implementation

- Exact set of model name -> context-limit overrides shipped in the default `MODEL_CONTEXT_LIMITS`. Driven by what models the user actually runs locally; will be discovered while running U7 verification.
- Whether `_extract_tags` can be reused as-is or needs a parallel parser specialized for the dump file's section-header format. Decide while implementing U3.
- Whether dump rendering should re-use a single shared markdown serializer or per-role helpers. Decide while implementing U2.

---

## Output Structure

```
.currentContext/
    contextTags.md                       # master tag taxonomy (created on first run)
    YYYY-MM-DD/
        HHMMSS-xxxx/                     # one directory per user-minted contextID
            dump-HHMMSS.md               # one per checkpoint cycle (multiple per contextID OK)
            dump-HHMMSS.md
            index.md                     # contextID-scoped tag -> [{file, anchor}] index
~/.omlx/
    last_dump_failure.json               # written only on dump failure
    model_context_limits.json            # persisted per-model overrides
```

The directory layout is a scope declaration. Implementer may adjust if a
different layout proves clearer during implementation, but the
contextID-scoped `index.md` is load-bearing for `recall_context`
performance and must remain.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
TUI input box -> mint contextID -> normalize tags -> begin layer turn(s)
                                                      |
                                                      v
                                          loop: api_call -> read prompt_tokens
                                                      |
                                  pct >= 60% AND clean boundary AND headroom OK?
                                                /             \
                                              no                yes
                                              |                  |
                                              v                  v
                                          continue       checkpoint cycle:
                                                          1. ask layer model to fill snapshot (strict template)
                                                          2. validate snapshot fields present
                                                          3. render full message list -> dump-HHMMSS.md with section anchors
                                                          4. extract per-section tags (prefer master); update contextTags.md and index.md
                                                          5. rebuild messages: [system, first_user, snapshot(system), tail_snippet]
                                                          6. continue loop
                                                          ON FAILURE -> log + emergency_trim + continue
```

Tail-snippet boundary rule: walk backwards from end of message list,
accumulating tokens (estimated at chars/4) until the budget is hit OR
the next-older message would orphan a `tool_calls`/`tool` pair. Always
end on a complete assistant turn.

---

## Implementation Units

- [ ] U1. **Token tracking and trigger detection scaffolding**

**Goal:** Land the per-layer token-tracking state, model context-limit lookup, trigger predicate, and clean-boundary guard. No checkpointing yet — just the decision plumbing wired into `_agent_turn_tui` so we can observe trigger events without acting on them.

**Requirements:** R1, R2, R9.

**Dependencies:** None.

**Files:**
- Modify: `omlx_agent.py` (add `MODEL_CONTEXT_LIMITS`, `_layer_context_state`, `_active_context_id`, helper functions, instrumentation hook in the agent loop).
- Create: `~/.omlx/model_context_limits.json` (auto-created on first override; not in repo).
- Test: `tests/test_context_trigger.py` (new file; see Patterns to follow).

**Approach:**
- Add module-level `MODEL_CONTEXT_LIMITS: dict[str, int]` with a `DEFAULT_CONTEXT_LIMIT = 16384` fallback and a loader that merges `~/.omlx/model_context_limits.json`.
- Add a layer-name resolver that maps `active_model` -> layer name using existing `CE_MODE_TO_GROUP` and `MODEL_GROUPS`.
- Add `_should_dump(layer, prompt_tokens, messages) -> bool` enforcing: pct >= 60%, last message is a complete assistant turn (no pending `tool_calls` without matching `tool` responses), and pre-flight estimate fits.
- In `_agent_turn_tui`, after the existing `usage` parse, call `_should_dump` and emit a TUI debug line (`[trigger ready: dump would fire]`) without acting. This is the observation harness for U7.
- Add a CLI flag `--context-limit MODEL=N` (repeatable) that updates the persisted overrides.

**Patterns to follow:**
- Module-level config dicts and loaders mirror the `_TEMP_FILES` / `CE_MODE_TO_GROUP` style already in `omlx_agent.py`.
- TUI debug print: use `tui_print` with `C_DIM` matching the existing `[thinking]` line.
- `tests/` directory does not yet exist; create it. Tests use `unittest` from stdlib only (no pytest), matching the no-pip-dependencies constraint. Document the test runner choice in the test file's module docstring.

**Test scenarios:**
- Happy path: `_should_dump` returns True when prompt_tokens / limit >= 0.60 and the message list ends on a clean assistant turn.
- Edge case: returns False when the last assistant message contains `tool_calls` whose responses have not yet been appended.
- Edge case: returns False when prompt_tokens is unknown (None or 0).
- Edge case: returns False when the pre-flight estimate predicts the dump itself would push past 90%.
- Edge case: layer resolver falls back to `DEFAULT_CONTEXT_LIMIT` for an unmapped model name.
- Happy path: `--context-limit gpt-foo=32000` persists to `~/.omlx/model_context_limits.json` and is read on next process start.

**Verification:**
- Running `/ce:flow` on a long-running task shows `[trigger ready: dump would fire]` in the TUI before any "Prompt too long" error occurs.
- New unit tests pass via `python3 -m unittest tests.test_context_trigger`.

---

- [ ] U2. **Snapshot generation and message-list rebuild**

**Goal:** Implement the snapshot prompt, parsing, validation, message-list rebuild (system + first user prompt + snapshot system message + tail snippet), and the orphan-safe tail slicer. Snapshot fires when U1's predicate trips.

**Requirements:** R3, R4.

**Dependencies:** U1.

**Files:**
- Modify: `omlx_agent.py` (add `SNAPSHOT_TEMPLATE`, `_request_snapshot`, `_validate_snapshot`, `_safe_tail_slice`, integration in `_agent_turn_tui`; mirror integration into the manager/sub-agent loops where they exist as separate message lists, gated to non-manager layers per Key Technical Decisions).
- Test: `tests/test_snapshot.py`.

**Approach:**
- `SNAPSHOT_TEMPLATE` is a constant string with the v1 fields from the origin's FR4. Required fields enforced by `_validate_snapshot` are: Active Task, Current Step, Decisions Made, Ruled Out / Don't Retry, Verified Facts, Unverified Assumptions, Open Questions, Files Touched, Next Intended Action. Empty sections must contain literal `(none)`.
- `_request_snapshot(layer, messages, model)` builds a snapshot-only prompt from the *current* messages plus a strict instruction, calls `api_call` with `tools=[]` semantics (pass an empty tools list to discourage tool use during the snapshot), and returns the parsed body.
- `_validate_snapshot(text)` returns the parsed snapshot or raises `SnapshotInvalid` with the missing-field list.
- `_safe_tail_slice(messages, budget_tokens)` walks backwards estimating tokens at `len(content) // 4`, stops when adding the next message would exceed the budget OR would slice between an assistant turn with `tool_calls` and its matching `tool` responses. Always returns a slice ending on a complete assistant turn.
- Rebuild order: `[original_system, first_user_prompt_of_contextID, {role: "system", content: snapshot_body}, *tail_slice]`.
- `first_user_prompt_of_contextID` is captured into `_layer_context_state[layer]["first_user_msg_idx"]` when the contextID is minted (U6 supplies the mint hook; U2 uses a temporary "first non-system message" fallback that U6 will replace).

**Test scenarios:**
- Happy path: snapshot containing all required fields validates and is injected as a `system` role message in the rebuilt list.
- Error path: snapshot missing "Ruled Out / Don't Retry" raises `SnapshotInvalid` with the missing-field name in the exception.
- Error path: snapshot with "Ruled Out / Don't Retry: (none)" passes validation (literal `(none)` is the empty-section signal).
- Edge case: `_safe_tail_slice` with a budget smaller than the last single message returns just the last complete assistant turn (cannot return less than one).
- Edge case: `_safe_tail_slice` refuses to start a slice in the middle of a `tool_calls`/`tool` group; rolls back to the prior assistant turn boundary.
- Integration: rebuilt message list always satisfies "every `tool_calls` id has a matching `tool` response".

**Verification:**
- A simulated checkpoint reduces a 12000-token message list to roughly the system + first-user + snapshot + ~10% tail size, with no orphaned tool-call ids.

---

- [ ] U3. **Tag taxonomy, dump file format, and tag extraction**

**Goal:** Implement the on-disk dump format with section anchors, the `.currentContext/contextTags.md` master taxonomy, the per-section tag header, and the tag-suggestion prompt that prefers existing tags. No retrieval yet.

**Requirements:** R5, R6, R7.

**Dependencies:** U2.

**Files:**
- Create: `.currentContext/contextTags.md` (auto-created on first run; not committed unless the repo elects to track it — add to `.gitignore` discussion in U7).
- Modify: `omlx_agent.py` (add `_render_dump_markdown`, `_anchor_for(idx)`, `_load_master_tags`, `_request_section_tags`, `_append_master_tags`, `_write_dump_file`).
- Test: `tests/test_dump_format.py`.

**Approach:**
- Dump rendering: each section is a fenced block prefixed with `<a id="s-NNN"></a>` plus a `### role: <role>` heading. Section split rule: one section per role-coherent pair as in Key Technical Decisions, with a >2000-token split fallback. The dump file's top header lists `## Section Tags` followed by `tag -> [s-001, s-014]` lines.
- `_load_master_tags()` reads `.currentContext/contextTags.md` and returns the set of tag strings. File format is one tag per line, optionally followed by ` # description`. Lines starting with `## added: <tag> -- <reason>` record additions.
- `_request_section_tags(sections, master_tags, layer_model)` calls the model with: (a) the section bodies, (b) the master tag list, (c) the strict rule "use existing tags when applicable; only propose new tags with a one-line justification". Returns a `dict[section_anchor, list[str]]` plus a list of newly proposed tags with reasons.
- `_append_master_tags(new_tags_with_reasons)` appends to `contextTags.md` atomically (write to `.tmp` then `os.replace`).
- `_write_dump_file(contextID, sections, tags_by_section, base_dir)` writes `dump-${HHMMSS}.md` and updates `index.md` (load-modify-save).

**Test scenarios:**
- Happy path: rendering N messages produces N (or fewer, after pairing) sections each with a unique `s-NNN` anchor.
- Happy path: a message larger than the 2000-token split threshold is broken into multiple sections, each anchor-tagged independently.
- Edge case: tag suggestion that proposes a near-duplicate of an existing tag (e.g., "auth" when "authentication" exists) is *accepted as proposed* — the normalization pass (U5) is responsible for merging. This unit must not silently rewrite suggestions.
- Edge case: `_load_master_tags` on a missing file returns an empty set without erroring; subsequent `_append_master_tags` creates the file.
- Integration: writing a dump updates `index.md` so a later read returns the new anchors under each tag.
- Error path: a malformed `contextTags.md` (e.g., mid-file binary garbage) is logged and treated as empty rather than crashing the dump.

**Verification:**
- After two synthetic dumps in the same contextID, `index.md` lists both files' anchors under shared tags.
- `contextTags.md` grows only via the explicit `## added:` annotation path.

---

- [ ] U4. **`recall_context` tool**

**Goal:** Add the `recall_context` tool to the worker tool surface, wire it through `TOOLS` and `TOOL_DISPATCH`, and make it return tag-matched section excerpts capped to the per-call token budget.

**Requirements:** R10.

**Dependencies:** U3.

**Files:**
- Modify: `omlx_agent.py` (`TOOLS` list, `TOOL_DISPATCH` map, new `tool_recall_context` and supporting `_load_index`, `_excerpt_section`).
- Test: `tests/test_recall_context.py`.

**Approach:**
- Tool schema: `{tags: [str] required, contextID: str default "current", limit: int default 5}`.
- `tool_recall_context(tags, contextID, limit)`: resolve `"current"` to `_active_context_id`; if no active id, return error string explaining no context is active. Load `index.md` for the resolved contextID. For each requested tag, look up matching sections, dedupe across tags, sort by recency (filename HHMMSS desc, anchor desc within file), take up to `limit`. For each hit, slice the section's body from the dump file and truncate so the cumulative excerpt total stays <= `RECALL_TOKEN_BUDGET = 4096` (estimated chars/4).
- If all requested tags miss the master taxonomy, return `{error: "...", suggested_tags: [...]}` listing nearest existing tags by simple substring or Levenshtein-by-prefix match (stdlib only — `difflib.get_close_matches`).
- Tool is **not** registered for the manager layer. Implementation: keep it in the global `TOOLS` (used by all worker layers) but the manager builds its own messages and never reads `TOOLS` for tool-calling decisions per existing manager rules.

**Patterns to follow:**
- Tool structure mirrors `tool_ce_scan_solution_headers`: a single function that returns a string (markdown formatted) the model can read directly.

**Test scenarios:**
- Happy path: recall by an existing tag returns the matching section excerpt(s) ordered most-recent-first.
- Happy path: recall with `limit=2` returns at most 2 hits even when more match.
- Edge case: requested tag does not exist in master taxonomy -> returns the error/suggestions response, never an empty-list-as-success.
- Edge case: requested tag exists in master taxonomy but no section is tagged with it in the contextID -> returns empty list (per origin FR9: "no matching tags returns empty list, not an error").
- Edge case: cumulative excerpt budget is enforced — last hit is truncated, never silently dropped without indication.
- Integration: tool registers in `TOOL_DISPATCH` and a model-issued `tool_calls` for `recall_context` resolves and returns a `role: tool` message that the agent loop appends correctly.

**Verification:**
- An agent run that triggers a checkpoint can later call `recall_context` and receive a section that was present in a prior dump.

---

- [ ] U5. **ContextID minting and synchronous tag normalization**

**Goal:** Mint a new `contextID` only on TUI-input-box prompts, capture the first user-message index per layer at mint time, and run the synchronous tag normalization pass before the first model call of the new contextID.

**Requirements:** R8, R9.

**Dependencies:** U3 (depends on `contextTags.md` and `index.md` existing).

**Files:**
- Modify: `omlx_agent.py` (`_handle_input` around line 4215 and the no-TUI input path around line 5273; new `_mint_context_id`, `_normalize_tags_pass`).

**Approach:**
- `_mint_context_id()` returns `${HHMMSS}-${urandom(2).hex()}`, sets `_active_context_id`, resets `_layer_context_state` first-message indices to "next message added per layer".
- Hook into `_handle_input` *only* on the chat-message branch (not on `/clear`, `/context`, or other slash commands). Manager-generated user messages (added by manager workflow code paths) must not pass through `_handle_input` — verified by the existing code structure where manager appends directly to its own list.
- `_normalize_tags_pass()` reads `contextTags.md`, finds entries marked `## added:` since the last normalization marker, uses `difflib.get_close_matches` against the canonical tag list to detect near-duplicates, and either: (a) merges into the canonical tag (rewriting all `index.md` files under `.currentContext/${date}/*/` that reference the duplicate) or (b) promotes the new tag to canonical when no close match exists. Writes a `## normalized: ${ISO}` marker line.
- Failure handling: on any I/O or parse error in normalization, log to stderr (or `tui_print` with `C_YELLOW`) and continue without blocking the user prompt.

**Test scenarios:**
- Happy path: typing a chat message in the TUI mints a new contextID; typing `/context` or `/clear` does not.
- Happy path: a `## added: authentication -- ...` entry is merged into existing `auth` if `auth` is canonical, and any `index.md` referencing `authentication` is rewritten to `auth`.
- Edge case: a newly added tag with no close match is promoted to canonical (no merge).
- Error path: corrupt `contextTags.md` causes normalization to log and skip without raising; the user prompt still proceeds.
- Integration: manager-added messages (simulated by directly appending to `manager_messages`) do not mint a new contextID.

**Verification:**
- Submitting two separate user prompts produces two distinct directories under `.currentContext/${date}/`.
- Submitting a user prompt after a tag was added in the prior contextID merges or promotes it before the first model call of the new contextID.

---

- [ ] U6. **Wire checkpoint cycle into the live agent loop with failure fallback**

**Goal:** Replace the U1 observation-only hook with a real checkpoint invocation. When the trigger fires, run the full cycle (snapshot -> dump -> rebuild) on the current layer's message list. On any failure inside the cycle, log to `~/.omlx/last_dump_failure.json` and call `emergency_trim` instead.

**Requirements:** R2, R4, R5, R11.

**Dependencies:** U2, U3, U5.

**Files:**
- Modify: `omlx_agent.py` (`_agent_turn_tui` around line 4720; the no-TUI agent loop if it has its own copy; manager loop if it gets the checkpoint cycle for its own message list — per Key Technical Decisions, the manager *does* get checkpointing but *not* `recall_context`).
- Test: `tests/test_checkpoint_cycle.py`.

**Approach:**
- New `run_checkpoint_cycle(layer, messages, model) -> tuple[list, dict | None]` returning the rebuilt message list and a result record (or `(messages, None)` on no-op).
- Wrap in `try/except` covering: `SnapshotInvalid`, `urllib.error.URLError`, `OSError` (disk write), `json.JSONDecodeError`. On exception: write `{contextID, layer, error, traceback, original_token_count}` to `~/.omlx/last_dump_failure.json`, call existing `emergency_trim`, return `(trimmed_messages, {fallback: True})`.
- After a successful cycle, set `self._activity = f"context dump: {old_pct}% -> {new_pct}%, {contextID}"` and `self.needs_redraw = True`. The activity line is the source of the user-facing dump notification.
- The cycle MUST happen before the next `api_call`, not after, so the upcoming call sees the rebuilt list.

**Patterns to follow:**
- Existing `emergency_trim` invocation site at line ~4724 is the placement model: detect condition, mutate `self.messages` in place, `continue` the loop.

**Test scenarios:**
- Happy path: trigger fires, snapshot succeeds, dump is written, message list shrinks below 30% of the model limit, next `api_call` proceeds normally.
- Error path: snapshot validation fails -> `~/.omlx/last_dump_failure.json` is written with the missing-field info, `emergency_trim` runs, agent continues.
- Error path: dump file write fails (e.g., directory permission) -> failure logged, `emergency_trim` runs, agent continues.
- Edge case: trigger fires twice in close succession (model still over threshold after rebuild) -> second cycle is allowed only after at least one successful `api_call` has happened in between, to prevent infinite dump loops.
- Integration: a long synthetic conversation that would have hit `emergency_trim` 3+ times completes with zero `emergency_trim` invocations and zero "Prompt too long" errors (origin Success Criteria 1).
- Integration: a forced malformed-snapshot response causes `emergency_trim` to run and the agent to continue without crashing (origin Success Criteria 5).

**Verification:**
- Running `/ce:flow` on a deliberately verbose objective shows TUI activity transitions: `dumping context...` -> `context dump: 62% -> 18%, HHMMSS-xxxx` -> normal activity.
- `~/.omlx/last_dump_failure.json` is absent on the happy path and populated only when a failure was actually exercised.

---

- [ ] U7. **`/context` slash command, TUI activity messaging, and end-to-end verification**

**Goal:** Surface per-layer context state to the user via a new `/context` slash command; finalize TUI activity strings; run end-to-end success-criteria verification; document the feature.

**Requirements:** R12, plus end-to-end coverage of R1-R11.

**Dependencies:** U6.

**Files:**
- Modify: `omlx_agent.py` (`_handle_input` around line 4215 and the no-TUI handler around line 5273; help-text strings around lines 2783 and 4969).
- Modify: `omlx_agent/README.md` (new "Proactive Context Management" section).
- Modify: `omlx_agent/.gitignore` (add `.currentContext/` if the repo elects not to track context dumps; planning recommends ignoring by default since dumps are session-local).

**Approach:**
- `/context` (no args): print, per layer, `layer | model | last_prompt_tokens / limit (pct%) | active_contextID | dumps_in_id`.
- `/context tags`: list canonical tags from `contextTags.md`.
- `/context recall <tag>`: dev convenience — call `tool_recall_context` with `tags=[tag]` and pretty-print the result. Useful for verifying U4 without needing the model to invoke it.
- README section explains the lifecycle, the `.currentContext/` layout, the `/context` command, and the failure-fallback guarantee.

**Test scenarios:**
- Happy path: `/context` after a few model calls shows non-zero `last_prompt_tokens` for the active layer.
- Happy path: `/context tags` lists tags from `contextTags.md`.
- Happy path: `/context recall <known-tag>` returns the same content the model would receive.
- Edge case: `/context` with no model calls yet shows zero counts and `active_contextID: (none)` for the contextID column when no user prompt has been submitted.
- Integration (manual): replay a `/ce:flow` run that previously hit `emergency_trim` 3+ times; confirm zero hard trims (Success Criteria 1).
- Integration (manual): induce a forced snapshot failure (e.g., temporarily monkey-patch `_request_snapshot` to return malformed text); confirm the agent recovers via `emergency_trim` (Success Criteria 5).
- Integration (manual): after a dump, ask the agent something that should require info from the dumped portion; confirm it calls `recall_context` and resumes correctly (Success Criteria 3).

**Verification:**
- All five origin success criteria documented as either passing or as a known-deferred-to-followup item.
- README section merged.
- New unit tests pass: `python3 -m unittest discover tests`.

---

## System-Wide Impact

- **Interaction graph:** The agent loop (`_agent_turn_tui` and the no-TUI counterpart), the manager workflow loop, and the slash-command dispatcher all change. `ce_run_agent` sub-agents are intentionally untouched (Scope Boundaries).
- **Error propagation:** Snapshot failure must never propagate to the user as a crash. The cycle catches all exceptions and routes to `emergency_trim`.
- **State lifecycle risks:** `_active_context_id`, `_layer_context_state`, and the on-disk `index.md` must agree. Two risks: (a) crash mid-dump leaves a dump file with no `index.md` entry — mitigated by writing the dump file first with a `.partial` suffix and renaming on success; (b) two layers triggering cycles concurrently. The agent runs one model at a time on local hardware, so concurrent triggers cannot occur in practice; document this assumption rather than adding locking.
- **API surface parity:** `recall_context` is the only new tool. It must appear in `TOOLS`, `TOOL_DISPATCH`, the help text, and the README.
- **Integration coverage:** The cleanest end-to-end test is a real `/ce:flow` run with a verbose objective; this is the integration scenario that mocks alone cannot prove.
- **Unchanged invariants:** `emergency_trim` keeps its current signature and behavior. The 6-layer `MODEL_GROUPS` split is unchanged. The `ce_run_agent` sub-agent isolation model is unchanged. Tool-call/tool-response pairing rules in the OpenAI message format are honored before and after rebuild.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Local LLM produces a malformed snapshot (missing required field). | `_validate_snapshot` raises, cycle falls back to `emergency_trim`, failure recorded for diagnosis. Validation is strict by design — better to fall back than to wipe context based on a half-formed snapshot. |
| Snapshot itself overflows context during generation. | Pre-flight headroom check in `_should_dump` reserves 4096 tokens for the snapshot output and refuses to fire if the estimate would exceed 90%. |
| Tag taxonomy explodes despite the "prefer existing" rule. | Synchronous normalization at every new user prompt merges duplicates; success criterion 4 (<= 2 net new tags per dump avg over 1 hour) is the early-warning signal. |
| Tool-call/tool-response orphaning in the tail slice causes oMLX to reject the next request. | `_safe_tail_slice` always ends on a complete assistant turn and never starts mid-pair; covered by U2 test scenarios. |
| Disk fills from accumulated dumps. | Out of scope for this plan; `.currentContext/` is recommended to be in `.gitignore`. Auto-pruning is in Deferred to Follow-Up Work. |
| Concurrent dump triggers from multiple layers. | Local-hardware single-model-at-a-time constraint precludes this; documented as an assumption, no locking added. |
| Re-trigger thrash if dump only marginally lowers token count. | U6 enforces "at least one successful `api_call` between cycles" to prevent infinite loops. |

---

## Documentation / Operational Notes

- README gets a "Proactive Context Management" section (U7) covering the lifecycle, the `.currentContext/` layout, the `/context` command, the snapshot template, and the failure-fallback guarantee.
- `.gitignore` recommendation: ignore `.currentContext/` by default; users who want to commit dumps for audit can override.
- No migration. The first run after merge auto-creates `.currentContext/contextTags.md` and `~/.omlx/model_context_limits.json` on demand.
- The new tests live in `tests/`. The project did not previously have a tests directory; document the `python3 -m unittest discover tests` command in the README "Development" section if one exists, otherwise add a brief note.

---

## Sources & References

- **Origin document:** [omlx_agent/docs/brainstorms/2026-04-23-proactive-context-management-requirements.md](omlx_agent/docs/brainstorms/2026-04-23-proactive-context-management-requirements.md)
- Reactive trim implementation: [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L2535-L2570)
- API call surface: [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L1980-L2050)
- Token usage read site: [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L4737)
- Tool registration pattern: [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L40)
- Slash command dispatcher (TUI): [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L4215)
- Slash command dispatcher (no-TUI): [omlx_agent/omlx_agent.py](omlx_agent/omlx_agent.py#L5273)
