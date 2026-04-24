# Proactive Context Management — Requirements

**Date:** 2026-04-23
**Status:** Requirements (ready for `/ce:plan`)
**Scope:** Deep — feature
**Target:** `omlx_agent.py`

## Problem

Today the agent only handles context overflow *reactively*: when the oMLX
server returns HTTP 400 "Prompt too long", `emergency_trim()` drops the 20
oldest non-system messages. There is no proactive monitoring, no
preservation of dropped content, and no way to recall earlier context
once it has been trimmed. Long autonomous runs (e.g., `/ce:flow`) hit
this wall repeatedly and lose information that mattered to the active
task.

## Goal

Add a proactive context management system that:

1. Monitors per-layer prompt-token usage against each model's context window.
2. Triggers a structured *checkpoint and rehydrate* cycle before the
   window is exhausted.
3. Preserves enough working state to resume the active task without
   re-deriving prior decisions or re-running ruled-out approaches.
4. Archives the full pre-wipe context to disk in a tag-indexed structure
   so the model can selectively recall earlier slices on demand.
5. Falls back safely to existing emergency trim behavior if the
   checkpoint operation itself fails.

## Non-Goals

- Cross-session context restoration. The archive persists for audit and
  intra-session recall; resuming a wholly new session from a prior
  contextID is out of scope.
- Vector embeddings or semantic similarity search. Recall is tag-based
  and exact-match.
- Replacing `emergency_trim`. It remains as the last-resort fallback.
- Compressing or post-processing tool outputs (file reads, command
  output) beyond what already happens.
- Changing the manager / brainstorm / plan / work / review / compound
  layer split. Each layer keeps its own message list; this feature
  manages each independently.

## User Stories

- As a user running `/ce:flow` for hours, I want the agent to never get
  bricked by context overflow and to never silently forget that it
  already tried and rejected an approach.
- As a user, I want each new prompt I submit to start a clean
  `contextID` so the audit trail is grouped by what I actually asked
  for.
- As the agent itself (mid-task), I want a way to recall a specific
  slice of earlier reasoning when I notice I need it, without reloading
  the entire prior conversation.

## Approach Decision

**A-enriched: full checkpoint / wipe / rehydrate**, with a structured
snapshot template and a tag-indexed archive that the model can query via
a new `recall_context` tool.

Rejected: rolling-summarization-only (option B) and the staged hybrid
(option C). The user explicitly wants the wipe-and-resume mechanic so
that long autonomous runs can sustain effectively unbounded runtime.
The risks of A are mitigated by the structured snapshot, clean-boundary
trigger, and emergency fallback.

## Functional Requirements

### FR1. ContextID lifecycle

- A new `contextID` is minted **only** when the user submits a prompt
  from the TUI input box (the existing user-input path). Manager-
  generated messages do not mint new IDs.
- All artifacts produced during that user prompt's lifetime live under
  `.currentContext/${YYYY-MM-DD}/${contextID}/...`.
- `contextID` format: short, sortable, collision-resistant within a day
  (e.g., `HHMMSS-<4char>`). Exact format is a planning decision.

### FR2. Per-layer token tracking

- Each model layer (manager, brainstorm, plan, work, review, compound)
  tracks its own `prompt_tokens` from the most recent API response.
- A new `MODEL_CONTEXT_LIMITS` config dict maps model name → max
  context tokens. A safe default is used when a model is not listed.
- Each layer maintains its own dump cycle and its own contextID-scoped
  archive subdirectory.

### FR3. Trigger conditions

The dump cycle fires when **all** of these are true:

- Current `prompt_tokens` for the layer >= 60% of that model's context limit.
- The agent is at a clean turn boundary: the last assistant turn is
  complete, no `tool_calls` are awaiting matching `tool` responses, and
  no `_context_overflow` signal is currently being handled.
- A pre-flight estimate indicates the dump operation itself can fit in
  the remaining headroom.

If any condition fails the trigger waits. If `prompt_tokens` reaches
the hard ceiling before a clean boundary, `emergency_trim` runs as
today.

### FR4. Structured snapshot (CONTEXT_SNAPSHOT v1)

Before wipe, the layer's model fills a strict template (free-form prose
inside fields is allowed; the field set is fixed):

```
## CONTEXT_SNAPSHOT v1
contextID: <id>
layer: <manager|brainstorm|plan|work|review|compound>
generated_at: <iso>

### Active Task
### Current Step
### Decisions Made (this contextID)
### Ruled Out / Don't Retry
### Verified Facts
### Unverified Assumptions
### Open Questions / Pending User Input
### Files Touched
### Next Intended Action
```

The "Ruled Out / Don't Retry" section is mandatory and must be
populated even with "(none)" when empty — this directly addresses the
known failure mode where post-wipe agents repeat already-failed
approaches.

### FR5. What survives the wipe

After a successful snapshot, the layer's message list is rebuilt as:

1. Original system prompt.
2. The current contextID's first user prompt (verbatim).
3. The CONTEXT_SNAPSHOT (as a system or assistant message — planning
   decides which the local LLMs handle better).
4. A tail snippet: the last ~10% of the model's context budget worth
   of recent messages, sliced on a clean turn boundary so no
   `tool_calls` are orphaned from their `tool` responses.

In-flight todos are not stored in the snapshot itself; they are
re-read from disk on demand via the existing `ce_manage_todos` /
`ce_mark_step` tools, which already persist to
`.compound-engineering/`.

### FR6. Archive format

For each dump under
`.currentContext/${date}/${contextID}/`:

- `dump-${HHMMSS}.md` — the full pre-wipe message list rendered as
  readable markdown, with each section anchored by an explicit ID
  (e.g., `<a id="s-001"></a>`) so recall can return precise slices.
- A header at the top of each dump file lists the tags applied to
  each section: `tag → [section_anchor, ...]`.
- `index.md` (one per `contextID`, updated on each dump) — global
  index of `tag → [{file, section_anchor}, ...]` across all dumps in
  the contextID.

### FR7. Tag taxonomy

- Master taxonomy lives at `.currentContext/contextTags.md`. Created
  on first run if missing.
- When tagging a new dump, the layer's model **must** prefer existing
  tags from `contextTags.md`. New tags require an explicit "added: <tag>
  — reason" line appended to `contextTags.md` in the same operation.
- Tags are applied at the **section** level (per anchor), not the
  whole-file level. Granularity for what counts as a section is a
  planning decision (default: each top-level heading or each
  user/assistant turn pair, whichever is finer).
- A normalization pass runs at the **start of each new user prompt**
  (before the first model call of that contextID): scans tags added
  during the previous contextID, merges near-duplicates against the
  master list, and rewrites affected `index.md` references.

### FR8. Recall tool

A new model-side tool, available to all non-manager layers:

```
recall_context(
  tags: [str],          # required, must match contextTags.md entries
  contextID: str = "current",
  limit: int = 5
) -> list of { tag, file, section_anchor, excerpt }
```

- Returns matching section excerpts ordered by recency.
- Excerpts are truncated to a per-call token budget (planning sets the
  number) so a single recall cannot itself blow context.
- If a requested tag does not exist in `contextTags.md`, returns an
  error suggesting nearest existing tags.

### FR9. Failure handling

- If snapshot generation errors, overflows, or is malformed: log to
  `~/.omlx/last_dump_failure.json`, call existing `emergency_trim`,
  and continue. The agent must never brick on a dump failure.
- If tag normalization fails: log and skip; do not block the new
  user prompt from running.
- If `recall_context` is called with no matching tags: return empty
  list, not an error.

### FR10. Observability

- Dump events surface in the TUI activity line:
  `[context dump: 60% -> 18%, contextID HHMMSS-xxxx]`.
- A new `/context` slash command shows current per-layer usage as
  percentage of each model's window, plus the active contextID.

## Success Criteria

1. A `/ce:flow` run that previously hit `emergency_trim` 3+ times
   completes with zero hard trims and zero "Prompt too long" errors.
2. After a dump, the resumed agent does not retry an approach that
   appears in the snapshot's "Ruled Out" section. (Manually verified
   against a representative replay.)
3. `recall_context` called with a known tag from a previous dump in
   the same contextID returns the expected section.
4. Tag taxonomy growth over a 1-hour session is bounded: <= 2 net new
   tags per dump on average (verifies the "prefer existing tags" rule
   is being followed).
5. Inducing a snapshot failure (e.g., forced malformed response)
   results in `emergency_trim` running and the agent continuing — no
   crash, no stuck state.

## Open Questions for Planning

- Exact `contextID` format and collision strategy.
- Whether the snapshot is injected as a `system` or `assistant` role
  message (local LLMs vary in how they weight each).
- Section granularity for tagging: per-heading vs per-turn-pair.
- Per-call token budget for `recall_context` excerpts.
- Default value and per-model overrides for `MODEL_CONTEXT_LIMITS`.
- Whether the normalization pass should run synchronously at prompt
  start (simpler, slight latency hit) or in a background thread
  (faster perceived response, more complexity).
- Whether the manager layer also gets `recall_context`, or only the
  worker layers.

## Dependencies

- Existing `emergency_trim` (kept as fallback).
- Existing `ce_manage_todos` / `ce_mark_step` for in-flight todo
  persistence.
- Existing per-layer model selection and message-list separation.
- Existing TUI activity-line surface for the dump notification.

## Assumptions

- oMLX `usage.prompt_tokens` is reliable enough to drive the 60%
  trigger. (Verified: code already reads it at line ~4737.)
- The model can be trusted to fill the snapshot template correctly
  *most* of the time; the failure-handling path covers the rest.
- Local LLMs can follow a strict template better than they can
  produce a useful free-form summary. (Empirical, accepted risk.)
- File I/O under `.currentContext/` is fast enough that dumps do not
  noticeably stall the TUI.
