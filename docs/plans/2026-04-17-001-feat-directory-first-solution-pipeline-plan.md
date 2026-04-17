---
title: "feat: Directory-First File Discovery and Structured Solution Pipeline"
type: feat
status: active
date: 2026-04-17
origin: docs/brainstorms/2026-04-17-directory-first-solution-discovery-requirements.md
---

# feat: Directory-First File Discovery and Structured Solution Pipeline

## Overview

Modify omlx_agent.py to (1) prevent the LLM from guessing file paths by adding a directory-first rule to the system prompt, (2) rotate `.temp_*` session files on startup, (3) guide the LLM through a structured solution-discovery pipeline when entering work/review/plan/compound modes, (4) auto-maintain temp file indexes when new solutions are created, and (5) gitignore transient files.

## Problem Frame

The omlx_agent's local LLM wastes tool rounds (out of a hard limit of 40) by fabricating file paths that don't exist. Additionally, the solution-discovery workflow is ad hoc -- the LLM has `ce_scan_solution_headers` and `ce_list_docs` available but no systematic pipeline to find, filter, and reference prior solutions before starting work. (see origin: docs/brainstorms/2026-04-17-directory-first-solution-discovery-requirements.md)

## Requirements Trace

- R1. Directory-first file discovery discipline (universal, all modes)
- R2. Temp file rotation on agent startup (Python code in `main()`)
- R3. Structured solution-discovery pipeline (prompt instructions in work/review/plan/compound prompts)
- R4. Temp file maintenance during session (Python code in `tool_ce_save_doc`)
- R5. Gitignore `.temp_*` and `*.old` patterns
- R6. Temp files written to repo root (current working directory)

## Scope Boundaries

- Only `omlx_agent.py` and `.gitignore` are modified
- No changes to Copilot skills, repo-level instruction files, or other projects
- No new tools -- pipeline uses existing `ce_scan_solution_headers`, `list_directory`, `read_file`, `write_file`
- Brainstorm and ideate modes are excluded from the solution pipeline (they explore new approaches rather than referencing prior implementations)
- No enforcement of tag format in solution files; the LLM reads both YAML frontmatter and markdown bold tag formats

## Context & Research

### Relevant Code and Patterns

- `SYSTEM_PROMPT`: Global system prompt with guidelines. Directory-first rule goes here.
- `CE_PROMPTS` dict: Mode-specific prompts. Pipeline instructions go in work/review/plan/compound entries.
- `tool_ce_save_doc()`: Saves CE docs to `docs/` or `referenceDocs/` subdirectories. R4 temp maintenance goes here.
- `tool_ce_scan_solution_headers()`: Already supports two modes -- no-args returns directory tree, with-directory returns first 20 lines of each file (headers contain tags).
- `_read_head()`: Reads first N lines of a file. Used by `ce_scan_solution_headers`.
- `_find_ce_dir()`: Locates CE subdirectories under `docs/` or `referenceDocs/`.
- `main()`: Startup flow -- `set_work_dir()` is called first, then model selection, then TUI launch. R2 rotation goes after `set_work_dir()`.
- `resolve()` helper: Joins paths against `WORK_DIR`.

### Institutional Learnings

- No existing `docs/solutions/` directory in omlx_agent repo. Solutions exist in Game-SpaceTradingSim-3 (50 files, 8 subdirs), ChaosChess (11 files, 3 subdirs), and XGame (1 file).
- Tag formats found across repos: YAML frontmatter (`tags:\n  - tag`) and markdown bold (`**tags:** tag1, tag2, tag3`).
- `compound-engineering.local.md` sits at repo root -- temp files follow the same convention.

## Key Technical Decisions

- **R2 is Python code, R3 is prompt instructions, R4 is Python code:** R2 (startup rotation) and R4 (temp maintenance on save) are deterministic operations that should not consume tool rounds or rely on LLM compliance. R3 (the pipeline) is inherently LLM-driven -- it requires judgment about which subdirectories are relevant and which tags match the current task. This uses existing tools via prompt guidance.
- **ce_scan_solution_headers is sufficient without code changes:** The tool already returns directory trees (no args) and headers with tags (with directory arg). The LLM extracts tags from the header text. No structured tag parsing needed.
- **Skip pipeline if `.temp_relevantSolutions` exists:** When the user switches modes within a session (e.g., work -> review -> work), the pipeline should not re-run if temp files are already populated. The prompt instructs the LLM to check for existing temp files and skip if present.
- **Per-mode relevance anchoring:** Step 3 (tag selection) matches against different context per mode: work=plan goal, review=diff topics, plan=brainstorm problem, compound=recent changes.
- **Tool round budget is acknowledged:** The pipeline costs 5-10 rounds in typical use. This is acceptable because it prevents more expensive downstream errors (wrong file reads, duplicated solutions). The prompt guides the LLM to scan only subdirectories whose names suggest relevance.

## Open Questions

### Resolved During Planning

- **Code vs prompt boundary (P0 from review):** R2=Python, R3=prompt, R4=Python. Rationale: deterministic operations should be code, judgment operations should be prompt.
- **Tool round budget (P1 from review):** The existing `ce_scan_solution_headers` combines directory listing with header scanning, keeping rounds manageable. Prompt guidance limits subdirectory scanning to relevant ones.
- **Re-execution on mode switch (P1 from review):** Skip if `.temp_relevantSolutions` exists. Re-run only on next startup (rotation clears temp files).
- **Step numbering (P1 from review):** Pipeline steps are numbered 1-5 in the prompt text.
- **Brainstorm exclusion (P2 from review):** Explicitly excluded in scope boundaries above.

### Deferred to Implementation

- Exact prompt wording -- the plan provides the semantic content; the implementer writes natural prose the LLM will follow.
- Whether `ce_scan_solution_headers` tool description should be updated for clarity (currently says "omit to scan all" but actually returns the tree). Minor wording fix during implementation.

## Temp File Formats

All temp files use simple text, one entry per line:

| File | Format | Example |
|------|--------|---------|
| `.temp_solutionDirs` | One repo-relative path per line | `docs/solutions/auth/session-token-rotation.md` |
| `.temp_tagList` | `path: tag1, tag2, tag3` per line | `docs/solutions/auth/session-token-rotation.md: auth, security, tokens` |
| `.temp_relevantTags` | One tag per line | `auth` |
| `.temp_relevantSolutions` | One repo-relative path per line | `docs/solutions/auth/session-token-rotation.md` |

## Implementation Units

- [x] **Unit 1: Startup temp file rotation (R2)**

**Goal:** Rotate `.temp_*` files to `.old` when the agent starts, preserving prior session data while clearing for a fresh session.

**Requirements:** R2, R6

**Dependencies:** None

**Files:**
- Modify: `omlx_agent.py` (add `_rotate_temp_files()` function, call from `main()`)

**Approach:**
- Add a new function `_rotate_temp_files()` near the other helper functions (`_find_ce_dir`, `_read_head`, etc.)
- The function iterates over the four known temp file names, checks if each exists in `WORK_DIR`, and renames to `<name>.old` using `os.replace()` (atomic, overwrites existing `.old`)
- Call `_rotate_temp_files()` in `main()` immediately after `set_work_dir(args.repo)`, before model selection or TUI launch

**Patterns to follow:**
- `set_work_dir()` style: global state initialization, uses `resolve()` for path joining
- `os.replace()` for atomic rename (already used elsewhere in Python stdlib patterns)

**Test scenarios:**
- Happy path: Agent starts with `.temp_solutionDirs` and `.temp_tagList` present -> both renamed to `.old`, originals removed
- Happy path: Agent starts with no temp files -> no errors, no files created
- Edge case: `.temp_solutionDirs.old` already exists -> overwritten by new rotation
- Edge case: Only some temp files exist (e.g., `.temp_solutionDirs` but not `.temp_tagList`) -> only existing ones rotated

**Verification:**
- After startup, no `.temp_*` files exist (only `.temp_*.old` if they existed before)
- Prior session data is accessible in `.old` files

---

- [x] **Unit 2: Directory-first rule in SYSTEM_PROMPT (R1)**

**Goal:** Add a universal rule to SYSTEM_PROMPT that instructs the LLM to use `list_directory` before accessing unknown file paths.

**Requirements:** R1

**Dependencies:** None (independent of Unit 1)

**Files:**
- Modify: `omlx_agent.py` (edit `SYSTEM_PROMPT` string, line ~1093)

**Approach:**
- Add a section to the Guidelines block in `SYSTEM_PROMPT` titled "File Discovery Rule" or similar
- The rule states: when you need to locate a file whose path you don't already know, use `list_directory` first to discover what exists. Never guess or fabricate file paths. This does not mean listing directories on every prompt -- only when the path is unknown.
- Keep it concise (2-3 sentences) so it doesn't bloat the system prompt. The LLM's context window is limited.

**Patterns to follow:**
- Existing guideline format in SYSTEM_PROMPT: short imperative sentences, bullet points

**Test scenarios:**
- Happy path: LLM receives the prompt and calls `list_directory` before `read_file` when navigating to an unfamiliar directory
- Integration: The rule text appears in the system message injected at startup (`SYSTEM_PROMPT.format(work_dir=WORK_DIR)`)

**Verification:**
- `SYSTEM_PROMPT` contains the directory-first rule
- The rule is phrased as a clear directive, not a suggestion

---

- [x] **Unit 3: Solution pipeline in CE mode prompts (R3)**

**Goal:** Add structured solution-discovery pipeline instructions to the work, review, plan, and compound mode prompts in `CE_PROMPTS`.

**Requirements:** R3, R6

**Dependencies:** Unit 1 (pipeline references `.temp_*` files that rotation manages)

**Files:**
- Modify: `omlx_agent.py` (edit `CE_PROMPTS["work"]`, `CE_PROMPTS["review"]`, `CE_PROMPTS["plan"]`, `CE_PROMPTS["compound"]`, lines ~1357-1567)

**Approach:**
- Add a "Solution Context" section near the beginning of each mode's workflow (before the main work steps). The pipeline has two paths:
  - **Fast path:** If `.temp_relevantSolutions` already exists, read it and load the listed files as reference. Skip the rest of the pipeline.
  - **Full pipeline (5 steps):**
    1. Call `ce_scan_solution_headers()` with no args to get the full solutions directory tree. Transform the tree output into one repo-relative path per line and write to `.temp_solutionDirs`.
    2. For subdirectories whose names suggest relevance to the task, call `ce_scan_solution_headers(directory=<subdir>)` to get headers with tags. Extract tags from the headers and write filename-to-tags mappings in `path: tag1, tag2` format to `.temp_tagList`.
    3. From the collected tags, select those relevant to the current task. Write to `.temp_relevantTags`. **Relevance anchor varies by mode:**
       - work: match against the plan's goal and current step
       - review: match against the diff's changed file paths and topics
       - plan: match against the brainstorm's problem statement
       - compound: match against the git log of recent changes
    4. Identify solution files matching any selected tag. Write matching paths to `.temp_relevantSolutions`.
    5. Read the matched solution files as reference material for the ongoing work.
  - **No solutions directory:** If neither `docs/solutions/` nor `referenceDocs/solutions/` exists, skip the pipeline entirely.
- Each mode's prompt includes the pipeline instructions tailored with its specific relevance anchor
- Pipeline instruction text should be concise -- the LLM needs direction, not a lecture

**Patterns to follow:**
- Existing CE_PROMPTS workflow step format: numbered steps with brief descriptions
- Existing references to `ce_scan_solution_headers` in the brainstorm prompt's learnings step

**Test scenarios:**
- Happy path: work mode entered, `docs/solutions/` exists with tagged files, pipeline runs and loads relevant solutions
- Happy path: work mode entered, `.temp_relevantSolutions` already exists from prior mode entry, pipeline skipped
- Edge case: No `docs/solutions/` or `referenceDocs/solutions/` directory exists, pipeline skipped gracefully
- Edge case: Solutions directory exists but no tags match the current task, `.temp_relevantSolutions` is written empty, no solutions loaded
- Integration: Mode switch (work -> review -> work) does not re-run pipeline because temp files persist

**Verification:**
- All four mode prompts (work, review, plan, compound) contain the pipeline instructions
- Brainstorm, debug, and ideate prompts do NOT contain pipeline instructions
- Each mode's prompt includes its specific relevance anchor

---

- [x] **Unit 4: Temp file maintenance in ce_save_doc (R4)**

**Goal:** When a new solution file is saved via `ce_save_doc`, automatically append its information to `.temp_solutionDirs` and `.temp_tagList` so the solution index stays current without a full rescan.

**Requirements:** R4

**Dependencies:** Unit 1 (establishes temp file lifecycle), Unit 3 (defines temp file formats)

**Files:**
- Modify: `omlx_agent.py` (edit `tool_ce_save_doc()` function, line ~851)

**Approach:**
- After the existing file write logic in `tool_ce_save_doc()`, add a conditional block: if `doc_type == "solution"` and the corresponding temp files exist in `WORK_DIR`:
  - Append the new file's repo-relative path to `.temp_solutionDirs`
  - Extract tags from the first 20 lines of the saved content (reuse `_read_head` logic or parse `content` directly since we already have it in memory). Support both YAML frontmatter and markdown bold tag formats.
  - Append `<path>: <tag1>, <tag2>, ...` to `.temp_tagList`
- Only append if the temp file exists -- if no pipeline has run this session, don't create temp files from scratch (that's the pipeline's job)
- Tag extraction from `content` string: scan first 20 lines for `tags:` YAML block or `**tags:**` markdown bold line. This is Python string parsing, not LLM text comprehension.

**Patterns to follow:**
- `_read_head()` for first-N-lines pattern
- `os.path.relpath(path, WORK_DIR)` for repo-relative paths (already used in `tool_ce_save_doc`)

**Test scenarios:**
- Happy path: Solution saved while `.temp_solutionDirs` and `.temp_tagList` exist -> new entry appended to both
- Happy path: Solution saved with YAML frontmatter tags -> tags correctly extracted and appended
- Happy path: Solution saved with `**tags:**` markdown format -> tags correctly extracted and appended
- Edge case: Solution saved but no temp files exist (no pipeline has run) -> no temp file writes, no errors
- Edge case: Solution has no tags -> path appended to `.temp_solutionDirs`, path with empty tags appended to `.temp_tagList`
- Edge case: Non-solution doc_type (brainstorm, review, todo) -> no temp file maintenance

**Verification:**
- After saving a solution, `.temp_solutionDirs` and `.temp_tagList` contain the new file's information (if they existed before the save)
- Non-solution saves do not trigger temp file maintenance

---

- [x] **Unit 5: Gitignore and cleanup (R5)**

**Goal:** Add `.temp_*` and `*.old` patterns to `.gitignore` so transient files are never committed.

**Requirements:** R5

**Dependencies:** None (independent)

**Files:**
- Modify: `.gitignore`

**Approach:**
- Add a section to the existing `.gitignore` with a comment header (e.g., `# CE session temp files`)
- Add patterns: `.temp_*` and `.temp_*.old`

**Test expectation:** none -- pure config change; verify by inspection

**Verification:**
- `git status` does not show `.temp_*` or `*.old` files after they are created

## System-Wide Impact

- **Interaction graph:** Startup (`main()`) -> `_rotate_temp_files()` runs once. Mode entry -> LLM reads pipeline instructions from CE_PROMPTS -> LLM calls existing tools (`ce_scan_solution_headers`, `write_file`, `read_file`). `tool_ce_save_doc` -> auto-appends to temp files.
- **Error propagation:** Temp file rotation uses `os.replace()` which is atomic. If rotation fails (permissions), it should log a warning but not block startup. If temp file append fails in `ce_save_doc`, the save itself should still succeed -- temp maintenance is best-effort.
- **State lifecycle risks:** Temp files persist across mode switches within a session but are rotated on next startup. No risk of unbounded growth within a session (bounded by number of solutions).
- **Unchanged invariants:** All existing CE tools continue to work as before. No tool signatures change. No new tools added. The system prompt gains one rule; mode prompts gain pipeline instructions.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Pipeline consumes too many tool rounds (5-10 of 40) | Prompt guides LLM to scan only relevant subdirectories; fast-path skip via existing temp files reduces repeat cost to 1 round |
| LLM ignores directory-first rule | Prompt placement in SYSTEM_PROMPT (read on every turn) makes it persistent; this is a best-effort improvement, not a hard guarantee |
| Tag extraction in ce_save_doc misparses edge cases | Simple regex for both formats; if parsing fails, append path with empty tags -- the solution is still indexed even if tags are missed |
| Temp file write failures in ce_save_doc | Best-effort: catch exceptions, log warning, do not fail the save operation |

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-17-directory-first-solution-discovery-requirements.md](docs/brainstorms/2026-04-17-directory-first-solution-discovery-requirements.md)
- Related code: `omlx_agent.py` — `tool_ce_save_doc()`, `tool_ce_scan_solution_headers()`, `SYSTEM_PROMPT`, `CE_PROMPTS`, `main()`
