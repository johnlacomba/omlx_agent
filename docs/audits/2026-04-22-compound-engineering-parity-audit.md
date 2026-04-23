# Compound Engineering Parity Audit

**Date:** 2026-04-22
**Source of truth:** [EveryInc/compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin) — `plugins/compound-engineering/README.md` (50+ agents, 42+ skills)
**Vendored upstream SHA:** `7e83755acbb48a62accd3566def8c4adc7de451a` (see [compound-engineering/VENDOR_SHA](../../compound-engineering/VENDOR_SHA))
**Subject:** [omlx_agent.py](../../omlx_agent.py) Compound Engineering workflow

## Status (2026-04-22 update)

The vendor-and-loader pass landed: omlx_agent now ships **all 51 upstream agents** and **all 36 upstream skills** verbatim under [compound-engineering/](../../compound-engineering/), and loads them at runtime instead of paraphrasing. Refresh with [sync-ce-vendor.sh](../../sync-ce-vendor.sh).

| Layer | Implemented | Partial | Missing (in-scope) | Out of scope |
|---|---:|---:|---:|---:|
| Skills | **36** | 0 | 0 | 0 |
| Agents | **51** | 0 | 0 | 0 |

Every `/ce:<mode>` now resolves through `get_ce_prompt(mode)` which loads the upstream `SKILL.md` body and appends a host-adaptation footer that translates Claude Code tool names (`Read`, `Bash`, `Grep`, `AskUserQuestion`, `Task`) into omlx_agent's tool surface. Sub-agent dispatch goes through the new `ce_run_agent` tool, which spawns a vendored agent in an isolated conversation up to depth 3.

### What changed in omlx_agent.py

- New section: **CE Vendor Loader** — `_parse_ce_frontmatter`, `_load_ce_doc`, `load_ce_skill`, `load_ce_agent`, `list_ce_agents`, `list_ce_skills`, `get_ce_prompt`, `all_ce_modes`.
- New section: **CE Sub-agent Dispatch** — `run_ce_subagent`, `tool_ce_run_agent`, `tool_ce_list_agents`. Depth-capped at 3 to prevent runaway recursion.
- New tools registered in `TOOLS` and `TOOL_DISPATCH`: `ce_run_agent`, `ce_list_agents`.
- `CE_MODE_TO_GROUP` extended with all new modes (code-review, doc-review, compound-refresh, commit, pr-description, commit-push-pr, setup, test-browser, worktree, clean-gone-branches, optimize, sessions, demo-reel, report-bug, resolve-pr-feedback, polish-beta, lfg).
- Slash-command handler iterates `all_ce_modes()` (sorted longest-first so prefix matching picks `/ce:commit-push-pr` before `/ce:commit`).
- Help text refreshed in both `print_help` and `HELP_TEXT`.
- Legacy `CE_PROMPTS` dict retained as a fallback if a vendor file goes missing mid-refresh.

### Host adaptation footer

Appended to every vendored prompt at load time. Tells the model:

- `Read`/`Write`/`Edit`/`Bash`/`Grep`/`Glob`/`Task`/`AskUserQuestion`/`TodoWrite` → omlx equivalents.
- "Spawn parallel subagents" runs **sequentially** in omlx (one model on local hardware), aggregating JSON outputs.
- `WebSearch`/`WebFetch` are unavailable; the model should report the limitation rather than fake the call.
- Doc storage convention (`docs/{brainstorms,plans,reviews,solutions,todos}/`, `compound-engineering.local.md`) matches upstream.

### Browser tools (`/ce:test-browser`)

The vendored skill drives `playwright` via `Bash`, which translates to `run_command` here. **No new omlx tools were needed** — the host adaptation footer is sufficient. If the user lacks `playwright` on PATH, the skill itself surfaces the install instructions because that's how it's authored upstream.

The `omlx_agent` should expose the same agents and skills as the official Compound Engineering plugin, adapted to a single-file local-LLM (oMLX) runtime. This audit captures the current delta and tracks parity work.

## Architectural mapping

| Concept | Official plugin | omlx_agent today |
|---|---|---|
| Skill invocation | Slash command (`/ce-brainstorm`) routed by Claude Code/Codex | `/ce:<mode>` switch sets a system prompt for the active model |
| Agent invocation | Subagent spawned by host (Claude/Codex) with isolated context | Single-process model swap; "manager" model orchestrates phases via `/ce:flow` |
| Tool surface | Host-provided MCP/native tools per platform | 20 built-in Python tools (`read_file`, `write_file`, `replace_in_file`, `run_command`, git, `ce_*` doc helpers) |
| Persistence | `compound-engineering.local.md`, `docs/{brainstorms,plans,reviews,solutions,todos}/` | Same convention (matches plugin) |

omlx_agent's "modes" cover the **skill** axis. It does not yet have a separate **agent** layer (specialist reviewer/researcher personas spawned with isolated context). The closest analog is the manager-model handoff in `/ce:flow`.

---

## Skills audit

Legend: ✅ implemented · 🟡 partial / different shape · ❌ missing · ➖ host-specific (likely out of scope)

### Core Workflow

| Skill | Status | Notes |
|---|---|---|
| `/ce-ideate` | ✅ | `/ce:ideate` — produces ranked ideas; does not auto-route winner into brainstorm |
| `/ce-brainstorm` | ✅ | `/ce:brainstorm` — saves requirements doc via `ce_save_doc` |
| `/ce-plan` | ✅ | `/ce:plan` — checkbox plan, resumable via `ce_mark_step` |
| `/ce-work` | ✅ | `/ce:work` |
| `/ce-debug` | ✅ | `/ce:debug` |
| `/ce-code-review` | 🟡 | `/ce:review` exists but is a single-pass multi-perspective prompt. No tiered persona agents, no confidence gating, no dedup pipeline |
| `/ce-compound` | ✅ | `/ce:compound` — writes to `compound-engineering.local.md` |
| `/ce-compound-refresh` | ❌ | No mechanism to age out / replace / archive stale learnings |
| `/ce-optimize` | ❌ | No iterative optimization loop with parallel experiments + LLM-as-judge |

### Research & Context

| Skill | Status | Notes |
|---|---|---|
| `/ce-sessions` | ❌ | No cross-session history search across Claude/Codex/Cursor logs |
| `/ce-slack-research` | ❌ | No Slack integration |

### Git Workflow

| Skill | Status | Notes |
|---|---|---|
| `ce-pr-description` | ❌ | No PR title/body generator |
| `ce-clean-gone-branches` | ❌ | Tool surface supports it, no skill prompt |
| `ce-commit` | 🟡 | `git_commit_and_push` tool exists; no value-first commit-message skill |
| `ce-commit-push-pr` | ❌ | Commit + push present, no PR creation |
| `ce-worktree` | ❌ | No worktree management |

### Workflow Utilities

| Skill | Status | Notes |
|---|---|---|
| `/ce-demo-reel` | ❌ | No GIF/screen capture pipeline |
| `/ce-report-bug` | ❌ | Plugin-specific; a generic local equivalent could file to `docs/issues/` |
| `/ce-resolve-pr-feedback` | ❌ | Requires GitHub API integration |
| `/ce-test-browser` | ❌ | In scope — shell out to user-installed `playwright` via `run_command`. See implementation sketch below. |
| `/ce-test-xcode` | ➖ | macOS-only; out of scope unless XCode workflows requested |
| `/ce-setup` | ❌ | No environment diagnostic / bootstrap |
| `/ce-update` | ➖ | Plugin-update specific |
| `/ce-release-notes` | ➖ | Plugin-update specific |

### Development Frameworks

| Skill | Status | Notes |
|---|---|---|
| `ce-agent-native-architecture` | ❌ | No skill prompt for agent-native design |
| `ce-dhh-rails-style` | ❌ | No Ruby/Rails style skill |
| `ce-frontend-design` | ❌ | No frontend-design skill |

### Review & Quality

| Skill | Status | Notes |
|---|---|---|
| `ce-doc-review` | ❌ | No parallel persona doc-review skill |

### Content & Collaboration / Automation & Tools

| Skill | Status | Notes |
|---|---|---|
| `ce-proof` | ➖ | Proof editor integration; out of scope |
| `ce-gemini-imagegen` | ➖ | External API; out of scope unless requested |

### Beta / Experimental

| Skill | Status | Notes |
|---|---|---|
| `/ce-polish-beta` | ❌ | Human-in-the-loop polish phase after review |
| `/lfg` | 🟡 | `/ce:flow` is the closest analog — autonomous brainstorm → plan → work → review → compound loop. Differences: no stacked-PR seeds, no launch.json dev-server bootstrap |

**Skill totals:** ~8 implemented, ~3 partial, ~22 missing, ~7 host-specific / out of scope.

---

## Agents audit

omlx_agent has **no dedicated agent layer**. All work today happens in the active phase model with the corresponding skill prompt. To reach parity, each agent below would become either (a) a sub-prompt invoked by a parent skill via the manager handoff, or (b) a parallel pass coordinated by the manager model.

### Review agents (24)

Missing: `ce-agent-native-reviewer`, `ce-api-contract-reviewer`, `ce-cli-agent-readiness-reviewer`, `ce-cli-readiness-reviewer`, `ce-architecture-strategist`, `ce-code-simplicity-reviewer`, `ce-correctness-reviewer`, `ce-data-integrity-guardian`, `ce-data-migration-expert`, `ce-data-migrations-reviewer`, `ce-deployment-verification-agent`, `ce-dhh-rails-reviewer`, `ce-julik-frontend-races-reviewer`, `ce-kieran-rails-reviewer`, `ce-kieran-python-reviewer`, `ce-kieran-typescript-reviewer`, `ce-maintainability-reviewer`, `ce-pattern-recognition-specialist`, `ce-performance-oracle`, `ce-performance-reviewer`, `ce-reliability-reviewer`, `ce-schema-drift-detector`, `ce-security-reviewer`, `ce-security-sentinel`, `ce-swift-ios-reviewer`, `ce-testing-reviewer`, `ce-project-standards-reviewer`, `ce-adversarial-reviewer`.

Today the `/ce:review` mode prompt asks the model to consider correctness, security, performance, etc. as personas in one pass. Parity goal: split into discrete reviewer prompts that the manager dispatches sequentially or in parallel and then dedups.

### Document Review agents (7)

Missing: `ce-coherence-reviewer`, `ce-design-lens-reviewer`, `ce-feasibility-reviewer`, `ce-product-lens-reviewer`, `ce-scope-guardian-reviewer`, `ce-security-lens-reviewer`, `ce-adversarial-document-reviewer`.

These would back a future `ce-doc-review` skill (also missing).

### Research agents (9)

Missing: `ce-best-practices-researcher`, `ce-framework-docs-researcher`, `ce-git-history-analyzer`, `ce-issue-intelligence-analyst`, `ce-learnings-researcher`, `ce-repo-research-analyst`, `ce-session-historian`, `ce-slack-researcher`, `ce-web-researcher`.

Note: `ce-learnings-researcher` is partially present implicitly — every mode reads `compound-engineering.local.md` at startup. A dedicated agent would do focused query-driven retrieval rather than blanket preload.

### Design agents (3)

Missing: `ce-design-implementation-reviewer`, `ce-design-iterator`, `ce-figma-design-sync`. All require Figma / browser tooling not present.

### Workflow agents (2)

Missing: `ce-pr-comment-resolver`, `ce-spec-flow-analyzer`.

### Docs agents (1)

Missing: `ce-ankane-readme-writer`.

**Agent totals:** 0 / ~46 implemented (3 design agents counted as out-of-scope blockers).

---

## Notable omlx_agent capabilities not in the official plugin

These are things the plugin gets for free from the host (Claude Code/Codex) but omlx_agent had to build itself. Worth preserving as parity work proceeds.

- **Multi-model orchestration:** `/ce:flow` swaps models per phase, unloading the manager before loading a specialist phase model. Maps to per-agent model selection in a way the plugin does not expose.
- **Curses TUI** with steer/queue/stop while agent runs.
- **Built-in oMLX server lifecycle** (auto-restart on connection refused).
- **Resumability via `ce_mark_step`** — checkbox state is the durable cursor across plans, reviews, and compound passes.
- **`ce_scan_solution_headers` tag-based retrieval** for surfacing relevant prior solutions during planning.
- **Emergency context trim + auto-continue** on token-limit and prompt-too-long errors.

---

## Parity gap summary

| Layer | Implemented | Partial | Missing (in-scope) | Out of scope |
|---|---:|---:|---:|---:|
| Skills | 8 | 3 | ~22 | ~7 |
| Agents | 0 | 0 | ~43 | ~3 |

## Recommended next steps (priority order)

1. **Split `/ce:review` into a dispatcher + reviewer-agent prompts.** Highest leverage; the official plugin's review pipeline is its core differentiator. Start with `correctness`, `security`, `performance`, `maintainability`, `testing`, `adversarial`. Add confidence calibration + dedup as a second pass.
2. **Add a reviewer-agent registry** (dict of `name -> system_prompt`) and a `ce_run_agent(agent_name, context)` mechanism so the manager model can dispatch them. This becomes the substrate for all future agents.
3. **Implement `ce-doc-review`** with the 7 document-review agents — pairs naturally with brainstorm/plan output we already produce.
4. **Implement `/ce-compound-refresh`** — keeps `compound-engineering.local.md` from rotting; small surface area, high return.
5. **Add git-workflow skills:** `ce-commit` (value-first message), `ce-pr-description`, `ce-commit-push-pr`. Tool surface already exists.
6. **Add research agents:** `ce-learnings-researcher`, `ce-repo-research-analyst`, `ce-git-history-analyzer`. All implementable with current `read_file` / `search_files` / `git_log` tools, no external services.
7. **Implement `/ce-setup`** for environment diagnostics — natural fit for a single-file agent.
8. **Implement `/ce-test-browser`** (see sketch below) — high value for the user's frontend repos (XGame, ChaosChess, Game-SpaceTradingSim).
9. **Defer:** Slack, Proof, Gemini imagegen, Figma, Xcode, demo-reel — require external services or non-fitting project types and are not on the critical path for the local-LLM workflow.

## Implementation sketch: `/ce-test-browser`

**Why it was wrongly deferred:** I initially listed this out of scope because (1) omlx_agent has a no-pip-deps rule, (2) the agent itself is a curses TUI with no frontend to dogfood against, and (3) browser output (screenshots, DOM) eats local-LLM context fast. None of those hold up:

- The no-pip-deps rule applies to omlx_agent's own runtime, not optional external CLIs the user already has installed. `/ce-test-xcode` follows the same shell-out pattern with `xcodebuild`.
- The user runs omlx_agent against many repos with frontends (XGame, ChaosChess, Game-SpaceTradingSim).
- Screenshots can be written to disk and referenced by path; only failure summaries need to enter the model's context.

**Tool additions** (stdlib-only, shell out to user-installed tools):

| Tool | Behavior |
|---|---|
| `browser_test(url, script_path?)` | If `playwright` is on PATH, run `playwright test` (or a single spec at `script_path`). Capture pass/fail counts, failing test names, and stderr. Truncate output. |
| `screenshot(url, output_path, viewport?)` | Use `playwright screenshot` (or `chromium --headless --screenshot=...` fallback). Returns saved path, not bytes. |
| `browser_console(url)` | Headless page load that captures console errors/warnings only. Useful for smoke checks without a full test suite. |

All three should detect the missing CLI and return an actionable error ("install with `npm i -g playwright && playwright install chromium`") instead of failing opaquely — same pattern as the existing oMLX server auto-restart.

**Skill prompt (`/ce:test-browser`):**

1. Run `git_diff` to identify changed routes / components.
2. Map changed files to affected pages (heuristic: `src/pages/foo.tsx` → `/foo`, `src/routes/bar` → `/bar`; for unknown layouts, ask the user once and cache the mapping in `compound-engineering.local.md`).
3. For each affected page, call `browser_console` first (cheap smoke check), then `browser_test` if a matching spec exists.
4. On failure, save a screenshot via `screenshot()` to `docs/test-artifacts/<date>-<page>.png` and reference it in the review output.
5. Write a summary to `docs/reviews/` via `ce_save_doc(doc_type="review", ...)` so the result joins the normal review/compound flow.

**Context budget rules:**

- Never inline screenshot bytes — only paths.
- Cap stderr/stdout per test at ~2KB (matches existing `read_file` 12KB truncation philosophy).
- Failure summaries only; passing tests get a single-line tally.

**Sequencing:** ship after the reviewer-agent registry (step 2) so `/ce:test-browser` can register itself as a `runtime-behavior` reviewer and slot into `/ce:review` automatically.

## Tracking

Update this document as items move from missing to implemented. Pair each implemented skill or agent with a brainstorm or plan under `docs/brainstorms/` or `docs/plans/`.

## Outstanding follow-ups

The vendor pass shipped parity by lift, not by validation. These items remain open:

1. **End-to-end smoke test of `/ce:review`** against a real diff. The vendored skill is large (74KB) and was authored for Claude Code's deferred-tool semantics; we need to confirm a local model actually follows the dispatch flow and that `ce_run_agent` returns clean JSON the parent can merge.
2. **Verify the doc-review pipeline** spawns the 7 doc-review agents sequentially and dedups findings.
3. **Tool restrictions per agent.** Vendored agents declare `tools: Read, Grep, Glob, Bash` in their frontmatter. We currently expose the full omlx tool surface to every sub-agent. A future pass should honor `tools:` to prevent reviewer agents from mutating files.
4. **`AskUserQuestion` adaptation.** When the upstream skill calls `AskUserQuestion`, the host adaptation tells the model to "respond with a plain-text question and stop." That works for interactive use, but `mode:headless` and `mode:autofix` paths assume the question is suppressed entirely. Verify the flag-driven branches behave correctly with our footer.
5. **`WebSearch`/`WebFetch` agents.** `ce-web-researcher`, `ce-framework-docs-researcher`, `ce-best-practices-researcher`, `ce-issue-intelligence-analyst` declare web tools. Without them they degrade to "report the limitation." If we want real research, add a `web_fetch` tool that shells out to `curl` (stdlib `urllib` already in the file).
6. **Sub-agent token accounting.** Sub-agents inherit MAX_TOKENS from the parent model. A 74KB skill prompt + sub-agent transcripts will eat the local KV cache fast. Consider per-agent token budgets and aggressive trimming inside `run_ce_subagent`.
7. **Slack/Proof/Gemini/Figma/Xcode skills** are vendored but call host-only services. They will fail loudly via the adaptation footer rather than silently — verify the failure messages are readable.
