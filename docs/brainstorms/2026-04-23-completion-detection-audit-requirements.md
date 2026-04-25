# Completion Detection Audit: Brainstorm

**Date:** 2026-04-23  
**Author:** User request via brainstorm workflow  
**Context:** Audit of how the manager layer determines when a CE step is complete

---

## Problem Statement

The manager seems to push for too many iterations before accepting a model's response indicating work "has been completed already." This creates friction and wasted context by forcing unnecessary follow-up turns.

---

## Current Mechanism (as inferred from code scan)

### `_COMPLETION_VERIFY_PROMPT`
A verification prompt asks the manager model to confirm work matches the original objective. Key characteristics:

**Default behavior:** `"DEFAULT TO 'complete' UNLESS THERE IS CLEAR EVIDENCE THE WORK WAS NOT DONE."`

**Triggers for "incomplete":**
- Objective required code changes but work phase never ran
- Final summary admits task is unfinished/blocked/partially done
- Summary describes only planning/discussion when user asked for real change

**Explicitly NOT triggers:**
- Cannot see test results or run the code personally
- Thinking more polishing/review would be nice
- Brief summary
- Not personally inspecting the diff

### `_deterministic_completion_block`
Hard logic that rejects completion if:
- Implementation was required AND work phase never ran → forces work phase
- No plan exists AND implementation required → forces plan phase

**Notable caveat:** Once work has executed at least once, the manager's completion claim is trusted. This prevents thrashing on small fixes.

### Retry loop
The verifier retries up to 3 times if JSON parsing fails, appending error context each time.

---

## Root Cause Hypotheses

| Hypothesis | Description | Likelihood |
|------------|-------------|------------|
| H1: Model over-cautious | The verifier model is being overly conservative despite the "default to complete" instruction | Medium |
| H2: Context dilution | Original objective gets diluted in context window, making verification harder | Low-Medium |
| H3: Ambiguous responses | Model says "done" but the language doesn't clearly match the verification criteria | Medium |
| H4: Multiple criteria | Different paths for rejection (deterministic + verifier) create inconsistent behavior | Low |

---

## Alternative Approaches

### Approach 1: Stronger "Default to Complete" Enforcement

**What it involves:**
- Move from instruction-level defaulting to structural enforcement
- Verifier must explicitly cite a violation of the "NOT triggers" list to return incomplete
- Add a secondary confirmation: "On second review, is there STILL clear evidence of incompleteness?"

**Pros:**
- Directly addresses the symptom
- Minimal code change
- Aligns with stated design intent

**Cons:**
- Might let genuinely incomplete work slip through
- Requires careful prompt engineering to avoid instruction drift

**Complexity:** Low (prompt change only)

**Edge cases:**
- What if work ran but produced nothing meaningful?
- User-reported bugs after completion

---

### Approach 2: Single-Pass Verification (No Retry Loop)

**What it involves:**
- Remove the 3-iteration retry loop in `run_completion_verifier`
- Accept "incomplete" verdict immediately, OR fall back to "complete" if parsing fails

**Pros:**
- Reduces context waste when parsing fails
- Prevents retry storms
- Faster completion path

**Cons:**
- Could miss valid "incomplete" verdicts due to bad JSON generation
- Less robust

**Complexity:** Low (remove retry loop)

**Edge cases:**
- What if verifier genuinely needs a second look?
- Transient API issues vs. genuine uncertainty

---

### Approach 3: Confidence-Scored Verification

**What it involves:**
- Verifier returns `"verdict": "complete" | "incomplete" | "uncertain"` plus `"confidence": 0-1`
- If confidence < 0.8, auto-default to "complete"
- Log low-confidence verdicts for later analysis

**Pros:**
- Captures nuance the model might have
- Enables tuning the confidence threshold
- Generates observability data

**Cons:**
- More complex prompt and parsing
- Confidence calibration is notoriously unreliable

**Complexity:** Medium (new schema, calibration needed)

**Edge cases:**
- How to calibrate initial threshold?
- Model might game confidence values

---

### Approach 4: EVIDENCE-First Verification

**What it involves:**
- Require the verifier to point to SPECIFIC text from the final_message that indicates incompleteness
- "incomplete" without a quoted excerpt defaults to "complete"
- Similar to citation requirements in research workflows

**Pros:**
- Forces concrete reasoning
- Easy to audit why completion was rejected
- Reduces vague "gut feeling" incompleteness

**Cons:**
- Adds prompt complexity
- Model might fabricate quotes under pressure

**Complexity:** Medium (new schema + verification)

**Edge cases:**
- What if the excerpt is subtle/implicit?
- Model might skip the excerpt and return invalid JSON

---

### Approach 5: User-Configurable Strictness

**What it involves:**
- Add `COMPLETION_STRICTNESS: "lenient" | "strict"` to `config.local.yaml`
- Lenient: current behavior but with stronger "default to complete" bias
- Strict: current behavior as-is
- Default: lenient

**Pros:**
- Respects different user preferences
- Lenient can be tuned without affecting strict users
- Easy to A/B test

**Cons:**
- Adds config surface
- Requires user to know they need to adjust it

**Complexity:** Low-Medium (config + conditional prompt)

**Edge cases:**
- What's the right default?
- Users might not understand the setting

---

### Approach 6: Phase-Aware Strictness

**What it involves:**
- Apply different verification standards based on what's completing:
  - Brainstorm: lenient (it's exploratory by nature)
  - Plan: medium (requires structure but details can refine)
  - Work: lenient (trust the work, verify deterministically)
  - Review/Compound: medium (these are quality gates)

**Pros:**
- Matches the intent of each phase
- Reduces friction where it matters most
- Phase-boundary is clear in code

**Cons:**
- Adds conditional logic
- Might create confusing behavior at phase boundaries

**Complexity:** Medium (phase-aware prompt selection)

**Edge cases:**
- What about non-standard phase sequences?
- Work->Review->Work loops?

---

### Approach 7: Artifact-Based Verification

**What it involves:**
- Require ARTIFACTS to prove completion, not just text:
  - Brainstorm: requires `docs/brainstorms/*.md`
  - Plan: requires `docs/plans/*.md`
  - Work: requires git diff OR new/modified files
  - Review: requires `docs/reviews/*.md`

**Pros:**
- Objective criteria
- Cannot be faked with verbose language
- Aligns with "show, don't tell"

**Cons:**
- Overkill for quick iterations
- Might break valid completion scenarios (e.g., "nothing to change here")
- Requires per-phase artifact definitions

**Complexity:** Medium-High (artifact detection + per-phase rules)

**Edge cases:**
- Work that doesn't produce new files?
- Documentation-only changes?
- "Nothing to do here" scenarios?

---

## Hybrid Approaches

### Hybrid A: Evidence-First + Stronger Default

Combine Approach 1 and Approach 4:
- Require specific evidence for "incomplete"
- Strong structural default to "complete"
- Minimal complexity, maximum friction reduction

**Estimated impact:** High
**Complexity:** Low-Medium

---

### Hybrid B: Artifact + Confidence Fallback

Combine Approach 7 and Approach 3:
- Primary: check for expected artifacts
- Secondary: if artifacts present, confidence > 0.5 → complete
- If no artifacts, use text-based verification

**Estimated impact:** Medium
**Complexity:** Medium-High

---

### Hybrid C: Phase-Aware + Lenient Default

Combine Approach 6 and Approach 5:
- Phase-aware strictness levels
- Overall lenient default (configurable to strict)
- Work phase is always lenient (trust the work)

**Estimated impact:** Medium-High
**Complexity:** Medium

---

## Tradeoff Analysis

| Dimension | Speed (lenient) | Accuracy (strict) | Friction (middle) |
|-----------|-----------------|-------------------|-------------------|
| User satisfaction | High (fast) | Medium (thorough) | Variable |
| Completion quality | Variable | High | Medium-High |
| Context efficiency | High | Medium | Medium |
| Development cost | Low | Low | Medium |
| Maintenance burden | Low | Low | Medium |

**Key tension:** The current prompt *says* "default to complete" but the model *behaves* as if strict. This suggests either:
1. The instruction isn't strong enough structurally
2. The model has conflicting objectives
3. The context contains signals that override the default

---

## Recommendations (Ranked)

| Rank | Approach | Why |
|------|----------|-----|
| 1 | **Hybrid A (Evidence-First + Stronger Default)** | Directly targets the problem, minimal complexity, preserves safety |
| 2 | **Approach 2 (Remove Retry Loop)** | Quick win, reduces context waste, can be combined with others |
| 3 | **Hybrid C (Phase-Aware + Lenient)** | Respects phase intent, still simple to implement |
| 4 | **Approach 7 (Artifact-Based)** | Most robust long-term, but higher complexity |

---

## Questions for Clarification

1. **What does "too many iterations" mean quantitatively?** Is this 2-3 extra turns, or 5+?

2. **Are there specific examples where completion was rejected inappropriately?** If so, what was the pattern?

3. **Is the goal to optimize for speed, or to match the *intent* of "default to complete"?** These might lead to different solutions.

4. **Should "work has run at least once" remain the hard trust boundary?** Or should we verify more deeply?

---

## Next Steps

1. Get clarification on questions above
2. Select an approach (or hybrid)
3. Move to planning phase for implementation details
4. Consider logging changes to measure impact

---

**Scope assessment:** **Standard** — bounded problem with clear scope and some design decisions to make. Not trivial (multiple approaches), not deep (doesn't redefine the product).
