# Directory-First File Discovery and Structured Solution Pipeline

**Date:** 2026-04-17
**Status:** Requirements complete
**Scope:** omlx_agent.py (system prompts + tool implementations)

## Problem Statement

The omlx_agent's local LLM wastes tool rounds by guessing file paths that don't exist (e.g., fabricating `src/features/interview/interviewSlice.ts` when the actual project structure has no `features/` directory). With a 40-round tool limit, each failed guess costs progress. Additionally, the solution-discovery workflow lacks structure: the LLM has tools like `ce_scan_solution_headers` and `ce_list_docs` available but no systematic pipeline to find, filter, and reference prior solutions efficiently.

## Requirements

### R1. Directory-first file discovery discipline

When the LLM needs to locate or reference a file whose path it does not already know, it must use `list_directory` to discover what exists before attempting `read_file` or `search_files`. Guessing or fabricating file paths is explicitly prohibited.

This rule applies universally across all CE modes and general chat. It does not mean listing directories on every prompt -- only when the LLM needs to find something it hasn't already confirmed exists.

### R2. Temp file rotation on agent startup

When `omlx_agent.py` starts, it must check for existing `.temp_solutionDirs`, `.temp_tagList`, `.temp_relevantTags`, and `.temp_relevantSolutions` files in the working directory. If any exist, rename them by appending `.old` (overwriting any previous `.old` files). This preserves prior session data for review while clearing the workspace for a fresh session.

### R3. Structured solution-discovery pipeline

When the LLM enters work, review, plan, or compound mode, and a `docs/solutions/` or `referenceDocs/solutions/` directory exists in the repo, it must execute this pipeline:

- **Step 2a (Directory scan):** List the full directory tree of the solutions directory. Identify all subdirectories and files. Store the list of directories and filenames in `.temp_solutionDirs`.

- **Step 2b (Header scan):** Read the first ~20 lines of each solution file to extract tags. Support both YAML frontmatter format (`tags:` followed by `- tag` list items) and markdown bold format (`**tags:** comma, separated, tags`). Store the complete tag list (filename-to-tags mapping) in `.temp_tagList`.

- **Step 2c (Tag relevance selection):** From the collected tags, select any number that are relevant to the current problem or task at hand. Store selected tags in `.temp_relevantTags`.

- **Step 2d (Cross-reference search):** Search through all solution file headers for files that match any of the selected relevant tags. Store the matching filenames (full repo-relative paths) in `.temp_relevantSolutions`.

- **Step 2e (Reference loading):** Read the files listed in `.temp_relevantSolutions` and use their content as reference material for the ongoing work.

### R4. Temp file maintenance during session

When new solution files are created during a session (via `ce_save_doc` with `doc_type="solution"`), the relevant temp files (`.temp_solutionDirs`, `.temp_tagList`) must be updated to include the new file's information. This keeps the solution index current without requiring a full rescan.

### R5. Gitignore temp and old files

Add `.temp_*` and `*.old` patterns to the omlx_agent repo's `.gitignore` so these transient files are not committed.

### R6. Temp file location

All `.temp_*` and `.old` files are written to the current working directory (repo root), alongside `compound-engineering.local.md`.

## Non-Goals

- No changes to Copilot skills (`~/.copilot/skills/ce-*`).
- No changes to repo-level instruction files (`.github/instructions/`).
- No changes to any project outside `omlx_agent.py` and its repo-local files.
- No new tools for tag parsing -- the pipeline uses existing `ce_scan_solution_headers`, `list_directory`, and `read_file` tools, with tag extraction handled by the LLM's text comprehension.
- No enforcement of a specific tag format in solution files. The LLM is expected to recognize both YAML and markdown tag formats.

## Success Criteria

- The LLM never fabricates a file path when a directory listing would reveal the actual structure.
- On startup, stale temp files from the previous session are rotated to `.old`.
- In work/review/plan/compound modes, relevant prior solutions are systematically discovered and loaded as context when a solutions directory exists.
- New solution files created during a session are reflected in the temp file index.
- `.temp_*` and `*.old` files are not committed to git.

## Affected Code Areas

- `omlx_agent.py`:
  - Startup logic (temp file rotation)
  - `CE_PROMPTS` dict (all mode prompts need the directory-first rule; work, review, plan, compound prompts need the solution pipeline instructions)
  - `tool_ce_save_doc` function (temp file maintenance on new solution creation)
  - Potentially `tool_ce_scan_solution_headers` (may benefit from returning tag data more explicitly)
- `.gitignore` (add patterns)

## Open Questions

### Resolved During Brainstorm

- Should cleanup run at CE mode start or agent startup? Resolution: agent startup only.
- Should cleanup also run after compound completion? Resolution: no, startup only.
- Should the rule apply to all modes or specific ones? Resolution: the directory-first rule applies universally; the solution pipeline applies to work, review, plan, and compound modes.
- Where do temp files live? Resolution: repo root (current working directory).
- Should temp files be gitignored? Resolution: yes.
- Should existing tools be modified or new tools added? Resolution: modify existing tools as needed.

### Deferred to Planning

- Exact prompt wording for the directory-first rule across each CE mode prompt.
- Whether `ce_scan_solution_headers` should be enhanced to return structured tag data or whether the LLM's text parsing is sufficient.
- Whether the solution pipeline steps should be individual tool calls or orchestrated within a single tool invocation.
