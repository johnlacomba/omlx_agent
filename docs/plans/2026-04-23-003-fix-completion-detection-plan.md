---
title: Fix: Improve CE completion detection to reduce unnecessary iterations
type: fix
status: active
date: 2026-04-23
origin: docs/brainstorms/2026-04-23-completion-detection-audit-requirements.md
---

# Fix: Improve CE completion detection to reduce unnecessary iterations

## Overview

This plan implements two complementary changes to reduce friction when completing CE workflows:

1. **Remove the JSON parsing retry loop** in `run_completion_verifier()` — eliminates wasted context when the verifier returns invalid JSON
2. **Add evidence-first verification** — requires verifiers to cite specific evidence when saying "incomplete", with validation that flips to "complete" if evidence is missing

These changes address the problem where the verifier rejects completion without clear evidence, forcing the user through extra iterations.

---

## How Completion Detection Works

### Current Flow

```
User says "done" → Manager evaluates completion
    ↓
Deterministic block: Work ran? Plan exists? (always runs)
    ↓
If passes, run verifier (only if rejected_completions < 1)
    ↓
Verifier returns verdict (complete/incomplete)
    ↓
If "incomplete": re-run suggested phase, increment rejected_completions
If "complete" or verifier skipped: accept completion
```

### Key Mechanism

The `rejected_completions` counter ensures the verifier only runs **once** per workflow:

```python
if self.workflow_state.rejected_completions >= 1:
    return None  # Skip verification, allow completion
```

**The problem:** When the verifier says "incomplete" without clear evidence:
1. User is forced to re-run a phase
2. User asserts completion again
3. Verification is now skipped (counter hit) → completion accepted

The user experiences "too many iterations" because the first verifier rejection feels arbitrary. The solution is to make "incomplete" verdicts require evidence, and flip to "complete" if evidence is missing.

---

## Problem Frame

The completion verifier defaults to "complete" via instruction but the model behaves as if strict, creating friction:

- User says work is done
- Verifier says "incomplete" (often without specific evidence)
- User re-asserts completion
- Cycle repeats 2-4 times before acceptance

**Root cause:** The "default to complete" instruction exists but isn't structurally enforced — the model can (and does) ignore it when uncertain.

---

## Requirements Trace

From the origin document:

- **R1.** Reduce unnecessary iterations before completion acceptance
- **R2.** Preserve safety — don't let genuinely incomplete work slip through
- **R3.** Minimize complexity — changes should be simple and maintainable
- **R4.** Reduce context waste from verifier retries

---

## Key Technical Decisions

- **Evidence-first with structural enforcement:** Prompt-only "default to complete" is insufficient — the model ignores instructions when uncertain. Structural enforcement (requiring evidence for "incomplete" verdicts, flipping to "complete" if evidence is missing) ensures the default is actually honored. This justifies R3: while we add ~100 lines, we could not achieve the goal with a simpler approach.
- **Exact substring match for evidence:** Simpler than fuzzy matching; fabrications are rare and easy to spot
- **Backward-compatible parsing:** Old verdicts without new fields still parse successfully
- **Validation flips to "complete":** Missing/invalid evidence defaults to complete, not rejection
- **No logging in this PR:** Keep changes minimal; add observability later

### Relevant Code and Patterns

- `omlx_agent.py` lines ~2476-2555: Current verifier implementation
- `_COMPLETION_VERIFY_PROMPT`: Prompt template for verification
- `_parse_completion_verdict()`: JSON parsing function
- `run_completion_verifier()`: Main verifier function with retry loop
- `_deterministic_completion_block()`: Hard logic that still protects critical cases

### Institutional Learnings

None specific — this is a new area of optimization.

---

## Scope Boundaries

### In Scope

- Focus on the verifier code in `omlx_agent.py` (~lines 2476-2555)
- Changes to prompt text, verdict schema, parsing, and validation
- Evidence-first verification logic
- Removal of retry loop

### Out of Scope

- **Phase-aware strictness (Hybrid C)** — different verification standards per phase
- **Logging/observability** — verdict distribution tracking, metrics collection
- **Artifact-based verification (Approach 7)** — requiring physical artifacts to prove completion
- **User-configurable strictness (Approach 5)** — config-based lenient/strict modes
- **Confidence-scored verification (Approach 3)** — returning confidence scores with verdicts

### Deferred to Follow-Up Work

- Phase-aware strictness levels — separate PR if needed after baseline improvement
- Logging/observability for verdict distribution — separate PR
- Artifact-based verification — future exploration if friction persists

---

## Open Questions

### Resolved During Planning

- **Should evidence validation be strict or fuzzy?** Strict (exact substring) for simplicity
- **What happens if parse fails?** Return None (skip verification), deterministic block still active
- **Should we add logging?** Not in this PR; separate work

### Deferred to Implementation

- Exact line numbers for changes (may shift during edits)

---

## Implementation Units

---

- [ ] U1. **Remove retry loop from verifier**

**Goal:** Eliminate the 3-iteration retry loop that wastes context and adds latency.

**Requirements:** R4

**Dependencies:** None — standalone change

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- Remove `for _ in range(3):` loop from `run_completion_verifier()`
- Single API call; if parsing fails, return None (skip verification)
- Deterministic block still protects critical cases

**Patterns to follow:**
- Follow existing single-pass patterns elsewhere in codebase

**Test scenarios:**
- Happy path: Verifier returns valid JSON → verdict accepted
- Edge case: Verifier returns invalid JSON → None returned, verification skipped
- Edge case: Verifier returns empty response → None returned
- Error path: API call fails → None returned

**Verification:**
- Code review: confirm loop is removed
- Manual test: trigger completion with verifier that returns bad JSON — should skip and allow completion

**Complexity:** Low (~10 lines removed)

---

- [ ] U2. **Extend verdict schema with evidence fields**

**Goal:** Add `evidence_quote` and `violation_type` fields to the verdict JSON schema.

**Requirements:** R1, R2

**Dependencies:** None — can be done in parallel with U1

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- Update `_COMPLETION_VERIFY_PROMPT` to include new fields in schema
- New fields:
  - `evidence_quote`: exact text from final_message proving incompleteness (empty for "complete")
  - `violation_type`: one of `none`, `work_not_run`, `admission_incomplete`, `discussion_only`

**Patterns to follow:**
- Mirror existing schema format with escaped JSON

**Test scenarios:**
- Happy path: Schema includes new fields
- Edge case: Model omits new fields → backward-compatible parsing handles it

**Verification:**
- Code review: prompt includes new fields
- Manual test: trigger verification, inspect JSON output for new fields

**Complexity:** Low (~5-10 lines)

---

- [ ] U3. **Update verifier prompt to require evidence**

**Goal:** Instruct the verifier model to provide evidence when saying "incomplete".

**Requirements:** R1, R2

**Dependencies:** U2 (schema must include new fields)

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- Add explicit instruction to quote exact text from final_message
- Define the three violation types with clear descriptions
- Emphasize that uncertainty defaults to "complete"
- Add instruction that missing evidence = "complete"

**Prompt additions:**
```
When replying 'incomplete', you MUST:
1. Quote the EXACT text from the final_message that proves incompleteness
2. Identify which violation type applies
3. If you cannot quote specific evidence, reply 'complete'

CRITICAL: If you cannot identify specific evidence matching one of the
three violation types above, reply 'complete'.
```

**Patterns to follow:**
- Match existing prompt style and formatting

**Test scenarios:**
- Happy path: Verifier provides evidence when saying "incomplete"
- Edge case: Verifier says "incomplete" without evidence → validation will catch it
- Edge case: Model uncertain → should say "complete"

**Verification:**
- Code review: prompt includes evidence requirements
- Manual test: trigger verification with ambiguous summary — should get "complete"

**Complexity:** Low (~20-30 lines added to prompt)

---

- [ ] U4. **Update verdict parser for new fields**

**Goal:** Parse the new `evidence_quote` and `violation_type` fields from verdict JSON.

**Requirements:** R1, R2

**Dependencies:** U2 (schema must include new fields)

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- Update `_parse_completion_verdict()` to extract new fields
- Backward compatible: old verdicts without new fields still parse
- New fields are optional in parsing (use `.get()` with defaults)

**Code changes:**
```python
return {
    "verdict": verdict,
    "missing": (data.get("missing") or "").strip(),
    "evidence_quote": (data.get("evidence_quote") or "").strip(),
    "violation_type": (data.get("violation_type") or "none").strip(),
    "next_phase": (data.get("next_phase") or "").strip(),
    "next_phase_input": (data.get("next_phase_input") or "").strip(),
}
```

**Patterns to follow:**
- Mirror existing parsing pattern with `.get()` defaults

**Test scenarios:**
- Happy path: New verdict with all fields parsed correctly
- Edge case: Old verdict without new fields → defaults applied
- Edge case: Partial new verdict (only one new field) → parses what exists

**Verification:**
- Code review: parser includes new fields with defaults
- Unit test: parse JSON with and without new fields

**Complexity:** Low (~5 lines)

---

- [ ] U5. **Add evidence validation function**

**Goal:** Validate that "incomplete" verdicts have proper evidence; flip to "complete" if not.

**Requirements:** R1, R2

**Dependencies:** U4 (parser must extract new fields)

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- New function `_validate_evidence(verdict_data, final_message)`
- Validation rules:
  1. If verdict is "complete", return as-is
  2. If no `evidence_quote`, flip to "complete"
  3. If `evidence_quote` not in `final_message`, flip to "complete"
  4. If `violation_type` not in valid set, flip to "complete"
- When flipping, also clear `missing`, `next_phase`, `next_phase_input`

**Code sketch:**
```python
def _validate_evidence(verdict_data: dict, final_message: str) -> dict | None:
    if verdict_data.get("verdict") != "incomplete":
        return verdict_data
    
    evidence = verdict_data.get("evidence_quote", "").strip()
    
    if not evidence:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    if evidence not in final_message:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    valid_violations = {"work_not_run", "admission_incomplete", "discussion_only"}
    if verdict_data.get("violation_type") not in valid_violations:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    return verdict_data
```

**Patterns to follow:**
- Defensive programming with `.get()` defaults

**Test scenarios:**
- Happy path: Valid "incomplete" with evidence → returned as-is
- Edge case: "incomplete" without evidence → flipped to "complete"
- Edge case: Evidence not in final_message → flipped to "complete"
- Edge case: Invalid violation_type → flipped to "complete"
- Edge case: "complete" verdict → returned as-is

**Verification:**
- Code review: validation covers all cases
- Unit test: test each validation path

**Complexity:** Low-Medium (~30 new lines)

---

- [ ] U6. **Integrate validation into verifier**

**Goal:** Call `_validate_evidence()` after parsing verdict in `run_completion_verifier()`.

**Requirements:** R1, R2

**Dependencies:** U5 (validation function must exist)

**Files:**
- Modify: `omlx_agent.py`

**Approach:**
- After parsing verdict, call `_validate_evidence(verdict, final_message)`
- Return validated verdict

**Code change:**
```python
verdict = _parse_completion_verdict(text)
if verdict:
    verdict = _validate_evidence(verdict, final_message)
    return verdict
```

**Patterns to follow:**
- Minimal integration change

**Test scenarios:**
- Happy path: Valid verdict passes through unchanged
- Edge case: Invalid "incomplete" flipped to "complete"
- Integration: Full flow from verifier call to return

**Verification:**
- Code review: validation called correctly
- Manual test: trigger verifier with bad evidence — should get "complete"

**Complexity:** Low (~2 lines)

---

## End-to-End Testing

- **Interaction graph:** Verifier is called from manager layer; changes are isolated to verifier functions
- **Error propagation:** Validation flips invalid verdicts to "complete" — safe default
- **State lifecycle risks:** None — no persistent state changes
- **API surface parity:** N/A — internal function only
- **Integration coverage:** Manual testing sufficient; no new integration points
- **Unchanged invariants:** Deterministic block unchanged; still protects critical cases

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Model ignores evidence requirement | Validation layer catches it and flips to "complete" |
| Legitimate incomplete work slips through | Deterministic block still active; verification is second layer |
| JSON parsing breaks with new schema | Backward-compatible parsing with `.get()` defaults |
| User complaints about less rigor | Document rationale; "uncertainty = complete" is intended behavior |

---

## Documentation / Operational Notes

- No user-facing documentation needed — this is a friction reduction
- Code comments explain validation logic
- No rollout considerations — immediate benefit

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-23-completion-detection-audit-requirements.md](docs/brainstorms/2026-04-23-completion-detection-audit-requirements.md)
- **Deepening analysis:** [docs/brainstorms/2026-04-23-completion-detection-deepening.md](docs/brainstorms/2026-04-23-completion-detection-deepening.md)
- **Current code:** `omlx_agent.py` lines ~2476-2555

---

## Implementation Checklist Summary

- [x] U1. Remove retry loop from verifier
- [x] U2. Extend verdict schema with evidence fields
- [x] U3. Update verifier prompt to require evidence
- [x] U4. Update verdict parser for new fields
- [x] U5. Add evidence validation function
- [x] U6. Integrate validation into verifier

**Total estimated effort:** ~80-100 lines of changes, all in `omlx_agent.py`
**Risk level:** Low
**Expected impact:** High — directly addresses the friction problem
