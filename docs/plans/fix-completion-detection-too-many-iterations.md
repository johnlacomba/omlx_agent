# Plan: Fix Completion Detection - Too Many Iterations

## Problem Analysis

The completion verifier is too strict despite instructions to default to "complete". This causes the manager to:
1. Reject valid completion claims
2. Push for more iterations
3. Eventually accept only after multiple cycles

## Root Causes

1. **Contradictory instructions**: System prompt says "strict completion auditor" but user prompt says "default to complete"
2. **Excessive retry loop**: 3 retries for JSON parsing adds latency without value
3. **Missing evidence requirements**: "incomplete" verdicts don't require citing specific problems
4. **Unclear violation criteria**: The conditions for "incomplete" aren't structurally enforced

## Proposed Fixes

### Fix 1: Remove Retry Loop
- The 3-iteration retry loop for JSON parsing is wasteful
- If parsing fails once, return None (treat as "skip verification")
- This is safe because failed verification doesn't mean "incomplete"

### Fix 2: Strengthen "Default to Complete" Instruction
- Remove "strict" from system prompt
- Add stronger language about defaulting to complete
- Explicitly state that uncertainty = complete

### Fix 3: Require Evidence for "Incomplete" Verdicts
- Add new schema fields:
  - `evidence_quote`: Exact text from final_message proving incompleteness
  - `violation_type`: One of `none`, `work_not_run`, `admission_incomplete`, `discussion_only`
- Instruction: "When saying incomplete, you MUST quote exact text proving it"

### Fix 4: Add Validation Layer
- New function `_validate_evidence()` that:
  - Returns verdict as-is if "complete"
  - Flips to "complete" if "incomplete" but no evidence_quote
  - Flips to "complete" if evidence_quote not found in final_message
  - Flips to "complete" if violation_type is invalid

## Implementation Steps

1. Update `_COMPLETION_VERIFY_PROMPT` with stronger default-to-complete language
2. Add new schema fields for evidence tracking
3. Add `_validate_evidence()` function
4. Update `_parse_completion_verdict()` to extract new fields
5. Update `run_completion_verifier()` to:
   - Remove retry loop
   - Call `_validate_evidence()` after parsing

## Expected Impact

- Verifier will default to "complete" much more often
- "Incomplete" verdicts will have concrete justification
- Validation will catch arbitrary "incomplete" verdicts
- Reduced friction when completing CE workflows
