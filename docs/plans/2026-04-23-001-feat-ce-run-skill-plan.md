# Plan: ce_run_skill primitive for omlx_agent

- Date: 2026-04-23
- Status: Proposed
- Owner: omlx_agent
- Related: docs/audits/2026-04-22-compound-engineering-parity-audit.md

## Objective

Add a `ce_run_skill` dispatcher to omlx_agent that runs a vendored Compound Engineering skill (`compound-engineering/skills/<name>/SKILL.md`) in an isolated sub-conversation, mirroring the existing `ce_run_agent` dispatcher. This unblocks `/ce:lfg`, lets agents invoke skills (e.g. review agents triggering `test-browser`), and lets `/ce:flow` route an `lfg` phase end-to-end.

## Non-goals

- No per-skill model swapping in v1. Sub-skills inherit the caller's model.
- No change to the loader, registry, or depth-guard infrastructure -- they already cover skills via `load_ce_skill` and `list_ce_skills`.
- No streaming of sub-agent output to the TUI in v1 (parent only sees the final blob). Revisit only if `lfg` quality suffers.

## Deliverables

1. `run_ce_skill(skill_name, arguments, model=None)` function.
2. `CE_HOST_ADAPTATION_SKILL` footer string (skill-flavored host translation).
3. `CE_HOST_ADAPTATION` updated to also advertise `ce_run_skill` to agents.
4. Two new tool registrations: `ce_run_skill`, `ce_list_skills`.
5. Argument-hint validation when caller passes empty arguments.
6. `/ce:flow` manager extended with an `lfg` phase that routes through `run_ce_skill`.
7. Smoke tests covering each new primitive.

## Implementation steps

### Step 1: Build CE_HOST_ADAPTATION_SKILL footer

Location: `omlx_agent.py`, immediately after the existing `CE_HOST_ADAPTATION` constant in the CE Vendor Loader section.

Required translations:
- `Skill("name", "args")` -> `ce_run_skill(skill_name="name", arguments="args")`.
- `Task(subagent_type="x", prompt="...")` -> `ce_run_agent(agent_name="x", task="...")`.
- `AskUserQuestion(...)` -> "You are running headlessly inside ce_run_skill. If a question is required, return it as plain text in your final response and stop."
- `ToolSearch(...)` -> "No-op. All tools are already loaded."
- `Read("references/foo.md")` -> Inject the absolute path: `<CE_VENDOR_DIR>/skills/<skill_name>/references/foo.md`. The footer must include the current skill's directory so relative reads resolve.
- Default-to-headless directive: "You are invoked via ce_run_skill. Treat this as headless mode. Do not block on user input. Return a single structured response when complete."

Implementation note: the skill_name and skill_dir must be interpolated per-call, so the footer is built inside `run_ce_skill` rather than as a module-level constant. Keep a `_CE_HOST_ADAPTATION_SKILL_TEMPLATE` constant with `{skill_name}` and `{skill_dir}` placeholders.

### Step 2: Implement run_ce_skill

Location: same file, immediately after `run_ce_subagent`.

Behavior (mirrors `run_ce_subagent`):
- Share the existing `_ce_subagent_depth` global. Refuse if `>= _CE_SUBAGENT_MAX_DEPTH`.
- Call `load_ce_skill(skill_name)`. On miss, return `[ce_run_skill error: skill 'X' not found in <dir>/skills/. Available (first 20): ...]`.
- Resolve model via the same caller > `_tui_instance.active_model` > error chain.
- Validate `arguments` against the skill's `argument-hint` frontmatter when present:
  - If hint exists, arguments is empty/whitespace, AND the hint contains a non-optional token (anything not wrapped in `[...]`), return an error citing the hint.
  - Otherwise allow empty arguments through.
- Build sub_messages:
  - `system`: `skill["body"] + CE_HOST_ADAPTATION_SKILL_TEMPLATE.format(skill_name=name, skill_dir=...)`
  - `user`: `f"Skill arguments: {arguments}"` (or `"Skill arguments: (none)"` when empty).
- Log entry/exit via `tui_print`/`print` with `[ce_run_skill -> {name} (depth N)]` prefix using `C_MAGENTA`.
- Increment/decrement `_ce_subagent_depth` in try/finally.
- Return `agent_turn(sub_messages, model)`.

### Step 3: Tool wrappers and registrations

Add to `TOOLS` list:
- `ce_run_skill` with parameters `{skill_name: str (required), arguments: str (optional, default "")}` and a description that mentions it inherits the parent model and runs headlessly.
- `ce_list_skills` with no parameters.

Add to `TOOL_DISPATCH`:
- `"ce_run_skill": tool_ce_run_skill`
- `"ce_list_skills": tool_ce_list_skills`

Implement the two `tool_*` wrappers next to the existing `tool_ce_run_agent` / `tool_ce_list_agents`. Mirror their input-validation patterns.

### Step 4: Cross-advertise in CE_HOST_ADAPTATION (agent footer)

Add a paragraph telling agents that `ce_run_skill(skill_name, arguments)` is available, with a one-line example: a review agent could spawn `ce_run_skill("test-browser", "")` after a frontend diff. Keep the existing agent-translation lines intact.

### Step 5: Wire /ce:flow lfg phase

In `omlx_agent.py`:
- Add `lfg` to the manager-decision JSON enum in both `MANAGER_SYSTEM_PROMPT` and the `_parse_manager_decision` whitelist.
- Document `lfg` in MANAGER_SYSTEM_PROMPT as: "ce-lfg: end-to-end feature delivery skill that internally runs brainstorm + plan + work + review + compound. Use only when the user explicitly says 'lfg' or 'ship it' and the request is a fully scoped feature."
- Extend `_failsafe_phase_input` with an `lfg` branch.
- In both `_run_phase_from_flow` and `_run_plain_phase_from_flow`, special-case `phase == "lfg"`: instead of loading the skill body as a system prompt, call `run_ce_skill("lfg", phase_input)` and append the returned text as the phase result. (Reason: `lfg` is meta-orchestration; loading its body inline would tangle with the manager prompt.)
- Confirm `CE_MODE_TO_GROUP["lfg"]` is still set (it already is).

### Step 6: Smoke tests

Run the same import-and-call pattern as the previous `/ce:flow` smoke test:

1. Import omlx_agent. Confirm no syntax errors.
2. `run_ce_skill("ce-doc-review", "mode:headless docs/plans/2026-04-23-001-feat-ce-run-skill-plan.md")` -> expect findings text, not an error.
3. `tool_ce_list_skills()` -> expect a list of 36 skills.
4. `_parse_manager_decision('{"action":"run_phase","phase":"lfg","phase_input":"add a foo widget"}')` -> expect a parsed dict.
5. Verify `ce_run_skill` shows up in `TOOL_DISPATCH` and `TOOLS`.
6. Verify depth guard: monkey-patch `_ce_subagent_depth = 3` then call `run_ce_skill(...)` -> expect refusal.

### Step 7: Update audit doc

In `docs/audits/2026-04-22-compound-engineering-parity-audit.md`:
- Move "Build ce_run_skill primitive" from outstanding follow-ups to completed.
- Note `/ce:lfg` is now functional.
- Add a follow-up entry for "Stream sub-skill output to TUI" if v1 ships without it.

## Risks and mitigations

- **Risk:** `lfg` returns one large blob and the parent loses intermediate progress. **Mitigation:** v1 accepts this. If problematic, add a streaming hook in `agent_turn` that forwards assistant deltas to `tui_print` while a sub-context is active.
- **Risk:** Skills that recursively call `Skill(...)` blow past depth 3. **Mitigation:** Depth ceiling is shared with agents; keep ceiling at 3, monitor logs, raise to 4 only with evidence.
- **Risk:** `references/*.md` paths in skills are inconsistent (some absolute, some relative). **Mitigation:** Footer instructs the skill to use the supplied absolute `skill_dir` for any `references/` reads. If a skill ignores the instruction, the read tool will fail loudly and the skill author can fix.
- **Risk:** Argument-hint parser misclassifies optional vs required. **Mitigation:** Treat presence of any non-`[...]` token as required. Default to permissive (allow empty) when in doubt to avoid false rejections.

## Acceptance criteria

- `python3 -c "import omlx_agent; print(omlx_agent.tool_ce_list_skills())"` lists 36 skills.
- `/ce:lfg add a hello-world widget` runs end-to-end without raising NotImplemented or routing to the legacy fallback prompt.
- `/ce:flow` can choose `lfg` when the user request matches and the lfg skill executes via `run_ce_skill`.
- A reviewer agent invoked via `ce_run_agent` can call `ce_run_skill("test-browser", "")` and receive the skill output.
- All depth-3 recursion attempts return the documented refusal string.
