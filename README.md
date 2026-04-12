# omlx_agent

A single-file coding agent powered by local LLMs via [oMLX](https://github.com/nickshouse/oMLX). Features a curses TUI, agentic tool calling, git integration, and a built-in **Compound Engineering** workflow for structured software development.

## Requirements

- Python 3.10+
- An [oMLX](https://github.com/nickshouse/oMLX) server running locally (default: `http://localhost:8000/v1`)
- No pip dependencies -- uses only the Python standard library (`curses`, `subprocess`, `json`, `urllib`, `threading`, `termios`, etc.)

## Quick Start

```bash
# Point it at a repo and go
python3 omlx_agent.py --repo /path/to/your/project

# Use current directory
python3 omlx_agent.py

# Skip the model picker (use one model for everything)
python3 omlx_agent.py --model my-model-name

# Plain text mode (no curses)
python3 omlx_agent.py --no-tui
```

On launch you pick two models:
- **Thinking model** -- used for brainstorm, plan, and ideate modes
- **Work model** -- used for work, review, compound, debug, and general chat

The agent auto-configures oMLX concurrency and memory settings per model.

## Features

### Curses TUI
Full terminal UI with split output/input panes, soft-wrapped text, scrollback, multi-line input editing, and persistent input history (`~/.omlx/input_history.json`).

**Input controls:**
| Key | Action |
|---|---|
| `Ctrl+S` | Submit message (also works as stop-and-send when agent is running) |
| `Enter` | Insert newline |
| `Up/Down` | Input history |
| `Ctrl+A/E` | Home / End of line |
| `Ctrl+U/K` | Kill before / after cursor |
| `PageUp/PageDn` | Scroll output |
| `Ctrl+L` | Clear output |

**While agent is running:**
| Key | Action |
|---|---|
| `Ctrl+S` | Stop agent and send your queued message |
| `Ctrl+Q` | Queue message to send after agent finishes |
| `Ctrl+T` | Steer -- inject a message without stopping the agent |
| `Ctrl+C` | Cancel current operation |

### Tool Calling

The agent has 20 built-in tools. Models call them via OpenAI-format `tool_calls` or `<tool_call>` XML tags (both parsed automatically).

**File operations:** `read_file`, `write_file`, `replace_in_file`, `list_directory`, `search_files`, `run_command`

**Git:** `git_status`, `git_diff`, `git_commit_and_push`, `git_pull`, `git_log`

**Compound Engineering:** `ce_read_learnings`, `ce_write_learning`, `ce_manage_todos`, `ce_create_plan`, `ce_save_doc`, `ce_mark_step`, `ce_list_docs`, `ce_scan_solution_headers`

Safety: dangerous commands (`rm -rf /`, `mkfs`, `dd if=`, fork bombs) are blocked. Shell commands timeout after 120s. File reads are truncated at 12KB.

### Compound Engineering Modes

Enter a mode with `/ce:<mode>` and exit with `/ce:done`. Each mode has a structured prompt that guides the agent through a specific workflow.

| Command | Mode | Purpose |
|---|---|---|
| `/ce:brainstorm` | Brainstorm | Explore requirements, propose approaches, save a requirements doc |
| `/ce:plan` | Plan | Create a step-by-step implementation plan with checkboxes |
| `/ce:work` | Work | Execute the plan step by step, marking progress as you go |
| `/ce:review` | Review | Multi-perspective code review (correctness, security, perf, etc.) |
| `/ce:compound` | Compound | Document learnings from recent work into `compound-engineering.local.md` |
| `/ce:debug` | Debug | Systematic root cause analysis and fix |
| `/ce:ideate` | Ideate | Discover high-impact improvements for the project |

**Resumability**: Plans and reviews use checkbox format (`[ ]` / `[x]`). The agent marks steps complete via `ce_mark_step` as it goes, so interrupted sessions can resume where they left off.

**Learnings file**: All modes read `compound-engineering.local.md` at the start of non-trivial tasks. The compound mode keeps it updated as the single source of truth for project knowledge -- patterns, gotchas, root causes, and solutions extracted from past work.

**Document storage**: CE documents (brainstorms, plans, reviews, solutions, todos) are saved to `docs/` or `referenceDocs/` subdirectories with auto-generated dated filenames.

### Context Management

- **Dual model support**: Thinking-heavy modes (brainstorm, plan, ideate) use one model; execution modes (work, review, compound, debug) use another. Concurrency and memory limits are switched automatically.
- **Emergency trim**: When the context window fills up, the agent drops the oldest non-system messages and retries automatically.
- **Auto-continue**: If generation hits the token limit mid-response, the agent prompts the model to continue where it left off.
- **Learnings preloaded**: The first 3000 chars of `compound-engineering.local.md` are injected as a system message at startup.

## Configuration

| Env Variable | Default | Description |
|---|---|---|
| `OMLX_API_KEY` | `omlx-...` | API key for the oMLX server |

Constants at the top of the file:

| Constant | Default | Description |
|---|---|---|
| `API_URL` | `http://localhost:8000/v1` | oMLX server endpoint |
| `MAX_TOKENS` | `131072` | Max generation tokens |
| `MAX_TOOL_ROUNDS` | `40` | Max consecutive tool calls per turn |
| `EMERGENCY_TRIM_DROP` | `20` | Messages dropped on context overflow |
| `LEARNINGS_FILE` | `compound-engineering.local.md` | Project learnings filename |
| `MAX_INPUT_HISTORY` | `200` | Saved input history entries |

## License

MIT
