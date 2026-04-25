---
title: "Completion verifier too strict despite 'default to complete' instructions"
date: 2026-04-23
problem_type: logic_error
module: ce-workflow-manager
tags:
  - completion-detection
  - verification
  - prompt-engineering
  - structural-enforcement
category: logic-errors
---

# Completion Verifier Too Strict Despite "Default to Complete" Instructions

## Problem

The completion verifier was rejecting valid completion claims and pushing for 2-4 unnecessary iterations before finally accepting completion, despite explicit instructions to "default to complete unless there is clear evidence the work was not done."

## Symptoms

- Manager would say "Work appears complete" and propose a final summary
- Completion verifier would respond with `verdict: incomplete` without specific justification
- Manager would push for "one more iteration" to address the missing work
- This cycle would repeat 2-4 times
- Eventually the manager would accept completion after multiple rejections

## What Didn't Work

**Prompt-only "default to complete" instruction:** The original prompt contained extensive language about defaulting to complete, but the model still rejected completions frequently. Simply telling the model what to do was insufficient.

## Investigation

The root cause analysis revealed two issues:

1. **Missing structural enforcement:** The verifier could say "incomplete" without providing evidence. The prompt asked for it, but there was no mechanism enforcing that "incomplete" verdicts must be backed up with concrete justification.

2. **Contradictory system prompt:** The system prompt said "You are a **strict** completion auditor" while the user prompt said "**default to complete**". This contradiction may have been contributing to overly strict behavior.

## Solution

### 1. Evidence Requirements for "Incomplete" Verdicts

Added new schema fields requiring the verifier to provide evidence when saying "incomplete":

```python
'evidence_quote': 'exact text from final_message proving incompleteness, else empty'
'violation_type': 'none | work_not_run | admission_incomplete | discussion_only'
```

The prompt now requires:
- Quoting exact text from the final message that proves incompleteness
- Identifying which of three violation types applies
- If no specific evidence can be quoted, replying "complete"

### 2. Structural Validation Layer

Added `_validate_evidence()` function that structurally enforces the default-to-complete policy:

```python
def _validate_evidence(verdict_data: dict, final_message: str) -> dict | None:
    """Validate that 'incomplete' verdicts have proper evidence."""
    if verdict_data.get("verdict") != "incomplete":
        return verdict_data
    
    evidence = verdict_data.get("evidence_quote", "").strip()
    
    # If no evidence provided, default to complete
    if not evidence:
        verdict_data["verdict"] = "complete"
        return verdict_data
    
    # If evidence doesn't appear in final_message, default to complete
    if evidence not in final_message:
        verdict_data["verdict"] = "complete"
        return verdict_data
    
    # If violation_type is not recognized, default to complete
    valid_violations = {"work_not_run", "admission_incomplete", "discussion_only"}
    if verdict_data.get("violation_type") not in valid_violations:
        verdict_data["verdict"] = "complete"
        return verdict_data
    
    return verdict_data
```

This validation flips "incomplete" verdicts to "complete" when:
- No evidence_quote is provided
- The evidence_quote doesn't appear in the actual final_message (fabricated evidence)
- The violation_type is not one of the three recognized types

### 3. Removed Wasteful Retry Loop

The original code had a 3-iteration retry loop for JSON parsing:

```python
for _ in range(3):
    response = api_call(messages, model)
    # ... parsing logic
```

Changed to single-pass:

```python
# Single API call — no retry loop
response = api_call(messages, model)
if not response:
    return None
```

Failed parsing returns `None`, which upstream treats as "skip verification, allow completion". This is safe because failed verification doesn't mean "incomplete".

### 4. Aligned System Prompt

Changed system prompt from:
```python
"You are a strict completion auditor. Output JSON only."
```

To:
```python
"You are a completion verifier. Default to 'complete' unless there is clear evidence of incompleence. Output JSON only."
```

## Why This Works

**Prompt-only instructions are insufficient for behavioral changes.** The model ignores "default to complete" when uncertain because it has no structural consequence for ignoring it. By adding:

1. A schema requirement for evidence (making it harder to say "incomplete")
2. A validation layer that flips invalid "incomplete" verdicts to "complete" (making it costly to get it wrong)

The behavior actually changes. The model now either provides real evidence or the verdict gets flipped anyway.

## Prevention

**When implementing "default to X" behavior in LLM-based systems:**

1. **Add structural enforcement, not just prompts.** Require evidence or justification for the non-default case.

2. **Add a validation layer.** Programmatically check that non-default responses meet criteria, and flip to default if they don't.

3. **Align system and user prompts.** Contradictions between them can undermine desired behavior.

4. **Test the actual behavior, not just the prompt.** A prompt saying "default to complete" means nothing if the verifier still rejects completions 80% of the time.

## Verification

After the fix:
- Completion verifier accepts completions on first attempt in most cases
- "Incomplete" verdicts now include specific evidence quotes
- No more 2-4 iteration cycles before completion acceptance

## Related

- `.compound-engineering/learnings.md` - Entry on structural enforcement for verifier behavior
- Plan: `docs/plans/fix-completion-detection-too-many-iterations.md`
