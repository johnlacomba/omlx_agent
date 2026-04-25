# Deepening: Completion Detection Approaches

**Date:** 2026-04-23  
**Origin:** `docs/brainstorms/2026-04-23-completion-detection-audit-requirements.md`  
**Status:** Analysis complete, ready for approach selection

---

## Executive Summary

After detailed analysis of the codebase and each recommended approach, here are the findings:

| Approach | Complexity | Expected Impact | Risk | Recommendation |
|----------|------------|-----------------|------|----------------|
| **Hybrid A** (Evidence-First + Stronger Default) | Low-Medium | High | Low | **PRIMARY RECOMMENDATION** |
| **Approach 2** (Remove Retry Loop) | Low | Medium | Low | **IMMEDIATE WIN — combine with Hybrid A** |
| **Hybrid C** (Phase-Aware + Lenient) | Medium | Medium-High | Low | Optional enhancement |

**Key Insight:** The retry loop (Approach 2) is low-hanging fruit that can be removed independently. Hybrid A addresses the core problem of model over-cautiousness. These two can be implemented together with minimal interaction between them.

---

## 1. Deep Dive: Hybrid A (Evidence-First + Stronger Default)

### What It Solves
The model is being "over-cautious" despite the instruction to default to complete. The instruction exists but isn't structurally enforced — the model can (and does) ignore it when uncertain.

### Required Changes

#### 1.1 Modified Verdict Schema

Current:
```json
{
  "verdict": "complete" | "incomplete",
  "missing": "what is still missing if incomplete, else empty string",
  "next_phase": "...",
  "next_phase_input": "..."
}
```

Proposed:
```json
{
  "verdict": "complete" | "incomplete",
  "missing": "what is still missing if incomplete, else empty string",
  "evidence_quote": "exact text from final_message proving incompleteness, else empty",
  "violation_type": "none | work_not_run | admission_incomplete | discussion_only",
  "next_phase": "...",
  "next_phase_input": "..."
}
```

#### 1.2 Modified Prompt (key additions)

```text
DEFAULT TO 'complete' UNLESS THERE IS CLEAR EVIDENCE THE WORK WAS NOT DONE.

When replying 'incomplete', you MUST:
1. Quote the EXACT text from the final_message that proves incompleteness
2. Identify which violation type applies
3. If you cannot quote specific evidence, reply 'complete'

Reply 'incomplete' ONLY when one of these is true:
  - work_not_run: The objective explicitly required code changes but the work 
    phase never ran (work is NOT in the completed phases list).
  - admission_incomplete: The final summary itself admits the task is 
    unfinished, blocked, or only partially done.
  - discussion_only: The summary describes only planning/discussion when the 
    user asked for a real change.

Do NOT reply 'incomplete' just because:
  - You cannot personally see test results or run the code.
  - You think more polishing, review, or extra phases would be nice.
  - The summary is brief.
  - You did not personally inspect the diff.
  - You are simply uncertain — uncertainty defaults to 'complete'.

If work has run and the summary claims the fix or feature is done, trust it.

CRITICAL: If you cannot identify specific evidence matching one of the 
three violation types above, reply 'complete'.
```

#### 1.3 Validation Layer

New function to validate verdicts:

```python
def _validate_evidence(verdict_data: dict, final_message: str) -> dict | None:
    """Validate that 'incomplete' verdicts have proper evidence.
    
    Returns the verdict_data if valid, or a modified version with verdict='complete'
    if evidence is missing/invalid.
    """
    if verdict_data.get("verdict") != "incomplete":
        return verdict_data
    
    evidence = verdict_data.get("evidence_quote", "").strip()
    
    # If no evidence provided, default to complete
    if not evidence:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    # If evidence doesn't appear in final_message, default to complete
    if evidence not in final_message:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    # If violation_type is not recognized, default to complete
    valid_violations = {"work_not_run", "admission_incomplete", "discussion_only"}
    if verdict_data.get("violation_type") not in valid_violations:
        verdict_data["verdict"] = "complete"
        verdict_data["missing"] = ""
        verdict_data["next_phase"] = ""
        return verdict_data
    
    return verdict_data
```

#### 1.4 Integration Points

**File:** `omlx_agent.py`

**Changes:**
1. Update `_COMPLETION_VERIFY_PROMPT` constant (~line 2480)
2. Update `_parse_completion_verdict()` to handle new fields (~line 2508)
3. Add `_validate_evidence()` function (~line 2530)
4. Update `run_completion_verifier()` to call `_validate_evidence()` (~line 2534)

---

### Edge Cases and Failure Modes

| Scenario | Behavior | Acceptable? |
|----------|----------|-------------|
| Model returns `evidence_quote` that doesn't match final_message | Validation catches it, defaults to complete | ✅ Yes |
| Model returns empty `evidence_quote` with `verdict=incomplete` | Validation catches it, defaults to complete | ✅ Yes |
| Model returns invalid JSON without new fields | Falls back to old parsing, works as before | ✅ Yes (backward compatible) |
| Evidence is subtle/implicit (e.g., "I tried but hit an issue") | Model should quote this; if it doesn't, complete | ✅ Yes (correct behavior) |
| Model fabricates evidence not in final_message | Validation catches it, defaults to complete | ✅ Yes |
| Model returns partial evidence (substring that exists but taken out of context) | Hard to detect; relies on model honesty | ⚠️ Risk — low probability |

### Measurement Criteria

**Quantitative:**
- % of verdicts that are "complete" (should increase significantly)
- Average number of verifier retries before completion (should decrease)
- % of "incomplete" verdicts with valid evidence (target: 100%)

**Qualitative:**
- User reports of "stuck in loop" decrease
- User satisfaction with completion speed

---

## 2. Deep Dive: Approach 2 (Remove Retry Loop)

### What It Solves
The 3-iteration retry loop wastes context when JSON parsing fails. Each retry adds:
- One API call
- The model's failed response to context
- An error message to context

### Required Changes

#### 2.1 Current Code (~line 2534)

```python
def run_completion_verifier(workflow_state: CEWorkflowState, final_message: str, model: str) -> dict | None:
    """Ask the manager model to confirm the work matches the original objective."""
    artifacts = _format_workflow_artifacts()
    completed = ", ".join(workflow_state.completed_phases) or "none"
    prompt = _COMPLETION_VERIFY_PROMPT.format(
        objective=workflow_state.objective or "(no objective recorded)",
        completed=completed,
        artifacts=artifacts,
        final_message=final_message or "(no final message)",
    )
    messages = [
        {"role": "system", "content": "You are a strict completion auditor. Output JSON only."},
        {"role": "user", "content": prompt},
    ]
    for _ in range(3):  # ← RETRY LOOP HERE
        response = api_call(messages, model)
        if not response:
            return None
        parsed = normalize_api_response(response)
        text = parsed.get("text") or ""
        verdict = _parse_completion_verdict(text)
        if verdict:
            return verdict
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": "Reply with valid JSON only using the required schema.",
        })
    return None  # ← Falls back to None after 3 failures
```

#### 2.2 Proposed Change

```python
def run_completion_verifier(workflow_state: CEWorkflowState, final_message: str, model: str) -> dict | None:
    """Ask the manager model to confirm the work matches the original objective.
    
    Single-pass verification: one API call, no retries. If parsing fails,
    returns None (which upstream treats as 'skip verification, allow completion').
    """
    artifacts = _format_workflow_artifacts()
    completed = ", ".join(workflow_state.completed_phases) or "none"
    prompt = _COMPLETION_VERIFY_PROMPT.format(
        objective=workflow_state.objective or "(no objective recorded)",
        completed=completed,
        artifacts=artifacts,
        final_message=final_message or "(no final message)",
    )
    messages = [
        {"role": "system", "content": "You are a strict completion auditor. Output JSON only."},
        {"role": "user", "content": prompt},
    ]
    
    # Single API call — no retry loop
    response = api_call(messages, model)
    if not response:
        return None
    parsed = normalize_api_response(response)
    text = parsed.get("text") or ""
    verdict = _parse_completion_verdict(text)
    
    # Return verdict if valid, else None (skip verification)
    return verdict
```

### Why This Is Safe

The existing deterministic block (`_deterministic_completion_block`) catches the most important cases:
- Work required but never ran
- No plan exists when implementation needed

The verifier is a *second* layer of defense, not the primary one. If it fails to parse, the deterministic block still protects against obvious misses.

### Edge Cases and Failure Modes

| Scenario | Current Behavior | New Behavior | Risk |
|----------|------------------|--------------|------|
| Model returns invalid JSON | Retry up to 3 times | Return None (skip) | Low — deterministic block catches critical cases |
| Model returns partial JSON | Retry up to 3 times | Return None (skip) | Low |
| Model returns empty response | Retry up to 3 times | Return None (skip) | Low |
| API timeout/error | Retry up to 3 times | Return None (skip) | Low — same as before |
| Genuine "incomplete" verdict lost due to parse failure | Retry might recover it | Lost | Low — rare, and deterministic block helps |

### Measurement Criteria

**Quantitative:**
- API calls per workflow completion (should decrease by ~0.5-1 per workflow)
- Context tokens consumed by verifier (should decrease)
- % of verifier calls that fail to parse (to monitor if this is a real problem)

---

## 3. Deep Dive: Hybrid C (Phase-Aware + Lenient Default)

### What It Solves
Different phases have different completion criteria. Brainstorming is exploratory by nature — it doesn't need the same rigor as, say, a migration. Yet the current verifier treats all phases identically.

### Required Changes

#### 3.1 Phase Strictness Map

```python
# Completion verification strictness per phase
_COMPLETION_STRICTNESS = {
    "brainstorm": "lenient",      # Exploratory, trust the user
    "plan": "lenient",            # Plans can be iterated
    "work": "lenient",            # Trust the work, deterministic block protects
    "deepen_plan": "lenient",     # Optional refinement
    "review": "medium",           # Quality gate, but still flexible
    "compound": "medium",         # Final synthesis, flexible
    "doc_review": "medium",       # Documentation polish
    "compound_refresh": "lenient",# Refresh is optional
}

_DEFAULT_STRICTNESS = "lenient"
```

#### 3.2 Conditional Prompt Selection

```python
def _get_completion_prompt(strictness: str) -> str:
    """Return the completion verification prompt for the given strictness level."""
    
    if strictness == "lenient":
        return _COMPLETION_VERIFY_PROMPT_LENTIENT
    elif strictness == "medium":
        return _COMPLETION_VERIFY_PROMPT_MEDIUM
    else:  # strict
        return _COMPLETION_VERIFY_PROMPT_STRICT
```

#### 3.3 Prompt Variants

**Lenient (work, brainstorm, plan):**
```text
DEFAULT TO 'complete' UNLESS THERE IS CLEAR EVIDENCE THE WORK WAS NOT DONE.

Uncertainty defaults to 'complete'. Brief summaries are fine. 
Trust the work if work phase has run.

Reply 'incomplete' ONLY when:
  - Work was required but never ran, OR
  - The summary explicitly admits incompleteness

Everything else defaults to 'complete'.
```

**Medium (review, compound):**
```text
DEFAULT TO 'complete' UNLESS THERE IS EVIDENCE THE WORK WAS NOT DONE.

Reply 'incomplete' when:
  - Work was required but never ran, OR
  - The summary explicitly admits incompleteness, OR
  - The output appears to lack expected structure/content

Uncertainty defaults to 'complete'.
```

**Strict (fallback):**
```text
VERIFY COMPLETION CAREFULLY. The user's objective should be fully satisfied.

Reply 'incomplete' when:
  - Work was required but never ran, OR
  - The summary explicitly admits incompleteness, OR
  - The output appears to lack expected structure/content, OR
  - You have concerns about whether the objective was met

When in doubt, ask for clarification rather than assuming completion.
```

#### 3.4 Integration

```python
def run_completion_verifier(workflow_state: CEWorkflowState, final_message: str, model: str) -> dict | None:
    """Ask the manager model to confirm the work matches the original objective."""
    
    # Get strictness based on the last completed phase
    last_phase = workflow_state.completed_phases[-1] if workflow_state.completed_phases else "work"
    strictness = _COMPLETION_STRICTNESS.get(last_phase, _DEFAULT_STRICTNESS)
    
    prompt_template = _get_completion_prompt(strictness)
    
    artifacts = _format_workflow_artifacts()
    completed = ", ".join(workflow_state.completed_phases) or "none"
    prompt = prompt_template.format(
        objective=workflow_state.objective or "(no objective recorded)",
        completed=completed,
        artifacts=artifacts,
        final_message=final_message or "(no final message)",
    )
    # ... rest same as before
```

### Edge Cases and Failure Modes

| Scenario | Behavior | Acceptable? |
|----------|----------|-------------|
| Non-standard phase sequence | Falls back to default strictness | ✅ Yes |
| Empty completed_phases list | Defaults to "work" strictness | ✅ Yes |
| User expects strictness but gets lenient | They can request re-verification | ✅ Yes (acceptable tradeoff) |
| Phase name changes in future | Dict lookup fails, uses default | ✅ Yes (safe fallback) |

### Measurement Criteria

**Quantitative:**
- Completion acceptance rate by phase (should vary by phase as expected)
- User-requested re-verification frequency (should be low)

**Qualitative:**
- User satisfaction with phase-appropriate rigor

---

## 4. Combining Approaches

### Recommended Combination: Hybrid A + Approach 2

**Why this combination:**
1. They address different problems (over-cautiousness vs. wasted retries)
2. They have minimal interaction — removing the retry loop doesn't affect evidence validation
3. Both are low-risk changes
4. Together they maximize friction reduction

**Implementation order:**
1. Remove retry loop (Approach 2) — simplest, standalone
2. Add evidence-first validation (Hybrid A) — addresses core problem

**Code flow after combination:**
```
User says "complete"
    ↓
Deterministic block checks (work ran? plan exists?)
    ↓
If passes, run verifier (SINGLE PASS, no retry)
    ↓
Parse verdict
    ↓
Validate evidence (if incomplete, requires quote + violation type)
    ↓
If validation fails → flip to "complete"
    ↓
Return verdict
```

### Optional Enhancement: Add Hybrid C Later

Hybrid C (phase-aware) can be added as a third step if needed. It doesn't interfere with A or 2.

---

## 5. Measurement and Success Criteria

### Primary Metric: Completion Friction Score

```
Friction Score = (Avg verifier retries per workflow) × (1 - % complete verdicts)
```

**Baseline (current):**
- Avg retries: ~1.5 (many workflows need 2nd or 3rd attempt)
- % complete: ~60% (rough estimate based on problem description)
- Score: 1.5 × 0.4 = **0.60**

**Target (after changes):**
- Avg retries: 1.0 (no retry loop)
- % complete: ~85% (stronger default enforced)
- Score: 1.0 × 0.15 = **0.15**

**Expected improvement:** ~75% reduction in friction score

### Secondary Metrics

| Metric | Baseline | Target | Measurement Method |
|--------|----------|--------|--------------------|
| API calls per workflow | ~4-6 | ~3-4 | Count API calls in session |
| Context tokens for verifier | ~2000-4000 | ~1000-1500 | Prompt token tracking |
| User re-engagement after "false" incomplete | Frequent | Rare | User feedback |

### Logging for Measurement

Add simple logging to track:
```python
# Pseudocode
verdict = run_completion_verifier(...)
if verdict:
    log_verdict(verdict["verdict"], verdict.get("violation_type"), len(verdict.get("evidence_quote", "")))
```

This helps monitor:
- Distribution of verdicts
- Whether evidence is actually being provided
- Which violation types are most common

---

## 6. Risk Assessment

### Overall Risk: LOW

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Model ignores evidence requirement | Medium | Low | Validation layer catches it |
| Legitimate incomplete work slips through | Low | Medium | Deterministic block still active |
| JSON parsing breaks with new schema | Low | Low | Backward-compatible parsing |
| User complaints about less rigor | Low | Low | Document rationale, offer strict mode |

### Rollback Plan

If issues arise:
1. Remove evidence validation (keep lenient prompt) — partial rollback
2. Restore retry loop if parse failures spike — independent change
3. Both changes are small enough for quick reversion

---

## 7. Final Recommendation

### Proceed with: Hybrid A + Approach 2

**Implementation Plan:**

| Step | Change | Lines Affected | Risk |
|------|--------|----------------|------|
| 1 | Remove retry loop | ~10 lines | Very Low |
| 2 | Update verdict schema | ~5 lines | Very Low |
| 3 | Update prompt | ~30 lines | Low |
| 4 | Add evidence validation | ~25 new lines | Low |
| 5 | Integrate validation | ~5 lines | Very Low |

**Total estimated effort:** ~75 lines of changes, all in `omlx_agent.py`

**Verification approach:**
1. Manual testing with known completion scenarios
2. Monitor first few workflows for any regressions
3. Collect qualitative feedback from user

---

## 8. Open Questions

1. **Should evidence validation be strict (exact substring match) or fuzzy?** Recommendation: exact match for simplicity; fabrications are rare.

2. **Should we log verdicts for analytics?** Recommendation: yes, simple stdout logging for now.

3. **Should "medium" strictness include evidence requirements?** Recommendation: no, keep evidence requirement only for lenient (to flip false negatives to complete).

---

**Status:** Analysis complete. Ready to proceed to planning phase for implementation.
