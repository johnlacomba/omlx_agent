#!/usr/bin/env python3
"""
oMLX Coding Agent - Local LLM agent with tool calling, GitHub sync,
and Compound Engineering workflows.

Usage: python3 ~/omlx_agent.py [--repo /path/to/repo]
"""

import base64
import hashlib
import curses
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime
from dataclasses import dataclass, field

API_URL = "http://localhost:8000/v1"
API_KEY = os.environ.get("OMLX_API_KEY", "omlx-80ktncu2cdui9fal")
MAX_TOKENS = 49152  # Cap output tokens to prevent KV cache OOM on 48GB systems
MAX_TOOL_ROUNDS = 40
EMERGENCY_TRIM_DROP = 20  # Messages to drop when oMLX returns "Prompt too long"
LEARNINGS_FILE = ".compound-engineering/learnings.md"
INPUT_HISTORY_FILE = os.path.expanduser("~/.omlx/input_history.json")
MALFORMED_DEBUG_FILE = os.path.expanduser("~/.omlx/last_malformed_response.json")
MAX_INPUT_HISTORY = 200

# ── Tool Definitions (OpenAI format) ─────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the working directory. Returns stdout and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents. Use line_start/line_end for partial reads.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to repo root)"},
                    "line_start": {"type": "integer", "description": "First line to read (1-based, optional)"},
                    "line_end": {"type": "integer", "description": "Last line to read (1-based, optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to repo root)"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace an exact string occurrence in a file with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to repo root)"},
                    "old_string": {"type": "string", "description": "Exact text to find (must match exactly once)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (relative to repo root, default '.')"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a pattern across files using grep. Returns matching lines with file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
                    "path": {"type": "string", "description": "Directory to search in (default '.')"},
                    "include": {"type": "string", "description": "File glob pattern, e.g. '*.py' or '*.tsx'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git status including branch, staged/unstaged changes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff of current changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "description": "Show staged changes only (default false)"},
                    "file": {"type": "string", "description": "Specific file to diff (optional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_and_push",
            "description": "Stage all changes, commit with a message, and push to the current branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "files": {"type": "string", "description": "Specific files to stage (space-separated). Default: all changes."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": "Pull latest changes from remote.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Branch to pull (default: current branch)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent git commits with diffs for context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of commits to show (default 10)"},
                    "file": {"type": "string", "description": "Show history for a specific file"},
                    "since": {"type": "string", "description": "Show commits since date, e.g. '3 days ago'"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_read_learnings",
            "description": "Read the compound engineering learnings file for this project. Contains documented solutions, patterns, and gotchas from past work.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_write_learning",
            "description": "Append a new learning entry to the compound engineering learnings file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title for the learning"},
                    "problem": {"type": "string", "description": "What was the problem?"},
                    "solution": {"type": "string", "description": "What was the solution?"},
                    "tags": {"type": "string", "description": "Comma-separated tags for categorization"},
                },
                "required": ["title", "problem", "solution"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_manage_todos",
            "description": "Manage a todo list for the current work session. Use this for ALL multi-step tasks. Actions: list, add, complete, clear. NEVER remove individual items -- mark them complete instead. Only use clear when starting an entirely new task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "One of: list, add, complete, clear. Use 'complete' to mark items done (keeps them visible). Use 'clear' ONLY when starting a fresh task."},
                    "item": {"type": "string", "description": "Todo text (for add) or index number (for complete)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_create_plan",
            "description": "Create or read a plan document. Plans are stored in docs/plans/ or referenceDocs/plans/. Auto-generates a dated filename if name is omitted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "One of: create, read, list"},
                    "name": {"type": "string", "description": "Plan filename (optional for create - auto-generates dated name)"},
                    "content": {"type": "string", "description": "Plan content (for create)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_save_doc",
            "description": "Save a compound engineering document (brainstorm, review, solution, or todo list) to the appropriate docs/ or referenceDocs/ subdirectory with a dated filename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_type": {"type": "string", "description": "One of: brainstorm, review, solution, todo"},
                    "content": {"type": "string", "description": "Document content"},
                    "name": {"type": "string", "description": "Optional filename (auto-generates dated name if omitted)"},
                },
                "required": ["doc_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_mark_step",
            "description": "Mark a step as done in a plan or review document. Replaces '[ ]' with '[x]' for the matching step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the plan/review file (relative to repo root)"},
                    "step_text": {"type": "string", "description": "Unique substring of the step line to mark complete"},
                },
                "required": ["file_path", "step_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_list_docs",
            "description": "List all compound engineering documents across brainstorms/, plans/, reviews/, solutions/, todos/ in docs/ or referenceDocs/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_type": {"type": "string", "description": "Optional filter: brainstorms, plans, reviews, solutions, todos. Omit to list all."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_scan_solution_headers",
            "description": "Scan solution files and return only their first 20 lines (title, tags, summary). Use this to decide which solutions are worth reading in full. Pass a directory name to scan only solutions in that subdirectory, or omit to scan all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Optional subdirectory name within solutions/ to scan (e.g. 'shadow-rendering', 'auth'). Omit to scan all solutions."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_run_agent",
            "description": "Spawn a vendored Compound Engineering sub-agent with isolated context. Use this from a /ce: skill to dispatch reviewer, researcher, or document-review personas (e.g. ce-correctness-reviewer, ce-security-reviewer, ce-learnings-researcher). Returns the sub-agent's final output, typically structured JSON. The sub-agent runs to completion in a fresh conversation that does not see your current messages -- pass everything the agent needs in `task`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Vendored agent name without .agent.md, e.g. 'ce-correctness-reviewer'. Use ce_list_agents to discover available agents."},
                    "task": {"type": "string", "description": "Full task description and all context the sub-agent needs (diff, file paths, document path, prior findings to dedupe against, etc.). Be explicit -- the sub-agent does not see your conversation."},
                },
                "required": ["agent_name", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ce_list_agents",
            "description": "List all vendored Compound Engineering sub-agents available via ce_run_agent. Returns names and one-line descriptions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

WORK_DIR = os.getcwd()
SESSION_TODOS = []
SESSION_DOCS = {
    "brainstorm": None,
    "plan": None,
    "review": None,
    "solution": None,
}


@dataclass
class CEWorkflowState:
    active: bool = False
    awaiting_user: bool = False
    objective: str = ""
    pending_question: str = ""
    completed_phases: list[str] = field(default_factory=list)
    manager_messages: list = field(default_factory=list)
    manager_failures: int = 0
    phase_failures: int = 0
    rejected_completions: int = 0


MANAGER_SYSTEM_PROMPT = """You are the Compound Engineering manager layer for a coding agent.

You do not implement code directly. You orchestrate the workflow across specialist phases and pause only when the user must answer a real question.

## Available specialist phases

Each phase below loads a vendored Compound Engineering skill (from EveryInc/compound-engineering-plugin) when invoked. The skill itself dispatches further sub-agents (reviewer personas, document-review personas, researchers) as needed -- you do not pick those.

Core sequence (use these for a typical feature workflow):
- `brainstorm` -- ce-brainstorm: explore requirements through collaborative dialogue, save a requirements doc.
- `plan` -- ce-plan: create a structured implementation plan with checkboxes.
- `deepen_plan` -- ce-plan: deepening pass that runs sub-agent research on the existing plan.
- `doc_review` -- ce-doc-review: run persona reviewers (product-lens, design-lens, security-lens, scope-guardian, feasibility, coherence, adversarial) against the requirements or plan document. Insert this between `deepen_plan` and `work` when the plan makes architectural decisions, has more than ~5 implementation units, or covers a high-stakes domain. Pass the document path in phase_input.
- `work` -- ce-work: execute the plan step by step.
- `review` -- ce-code-review: tiered code review. Internally spawns the relevant reviewer agents (correctness, testing, maintainability, project-standards, agent-native, learnings-researcher, plus security/performance/api-contract/data-migrations/reliability/adversarial when the diff warrants). You do not list reviewers in phase_input -- the skill picks them.
- `compound` -- ce-compound: document learnings into .compound-engineering/learnings.md and solution docs.

Optional phases (use only when explicitly relevant, not by default):
- `ideate` -- ce-ideate: generate and rank improvement ideas. Use only when the user's objective is "what should we work on?" rather than a specific request.
- `debug` -- ce-debug: systematic root-cause investigation. Use instead of `work` when the objective is reproducing or fixing a bug.
- `compound_refresh` -- ce-compound-refresh: age out, replace, or archive stale learnings. Use only when the user explicitly asks to clean up the learnings file.

## Responsibilities

- Decide which phase to run next.
- Ask the user one concise question at a time only when requirements, scope, approvals, or missing decisions block progress.
- Keep actual implementation confined to `work` (or `debug` for bug investigations).
- Treat brainstorm, plan, deepen_plan, doc_review, ideate, review, compound, and compound_refresh as non-implementation phases. They may read and write planning, review, and solution artifacts, but they must not change product code.
- Once enough information exists, move forward without waiting for the user.
- Prefer reusing existing brainstorms or plans when they already fit the request.

## Return format

Return JSON only with this schema:
{
  "action": "ask_user" | "run_phase" | "complete",
  "phase": "brainstorm" | "plan" | "deepen_plan" | "doc_review" | "ideate" | "work" | "debug" | "review" | "compound" | "compound_refresh" | "",
  "message": "short status or completion summary",
  "question": "question for the user when action=ask_user",
  "phase_input": "the exact instruction to send to the specialist phase when action=run_phase",
  "reason": "brief explanation for why this is the right next step"
}

## Rules

- action=ask_user: set question, leave phase empty, leave phase_input empty.
- action=run_phase: set phase and phase_input. question must be empty.
- action=complete: give a short final summary in message.
- For `doc_review`, phase_input MUST include the document path (e.g. "Review docs/plans/2026-04-22-001-foo-plan.md against the requirements at docs/brainstorms/2026-04-22-foo-requirements.md"). The doc-review skill cannot find the doc on its own when invoked headlessly.
- For `review`, do NOT specify which reviewer personas to use. The ce-code-review skill selects them based on the diff.
- Never choose `work` before a plan exists and has been deepened.
- Never choose `review` before `work` (or `debug` for bug fixes).
- Never choose `compound` before `review`.
- `doc_review` (when used) must come after `deepen_plan` and before `work`.
- When deciding between `brainstorm` and `plan`, choose `brainstorm` if user-facing behavior or scope is still unclear. Otherwise choose `plan`.
- Output valid JSON only. No markdown fences or commentary outside the JSON object.
"""


def _reset_session_docs():
    for key in SESSION_DOCS:
        SESSION_DOCS[key] = None


def _set_session_doc(doc_type: str, rel_path: str):
    if doc_type in SESSION_DOCS:
        SESSION_DOCS[doc_type] = rel_path


def _get_latest_ce_doc(subdir: str) -> str | None:
    newest_path = None
    newest_mtime = -1.0
    for parent in ["docs", "referenceDocs"]:
        root = resolve(os.path.join(parent, subdir))
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_path = os.path.relpath(full, WORK_DIR)
    return newest_path


def _workflow_artifact_snapshot() -> dict:
    return {
        "brainstorm": SESSION_DOCS.get("brainstorm") or _get_latest_ce_doc("brainstorms"),
        "plan": SESSION_DOCS.get("plan") or _get_latest_ce_doc("plans"),
        "review": SESSION_DOCS.get("review") or _get_latest_ce_doc("reviews"),
        "solution": SESSION_DOCS.get("solution") or _get_latest_ce_doc("solutions"),
    }


def _format_workflow_artifacts() -> str:
    artifacts = _workflow_artifact_snapshot()
    lines = []
    for key in ["brainstorm", "plan", "review", "solution"]:
        lines.append(f"- {key}: {artifacts.get(key) or 'none'}")
    return "\n".join(lines)


def _extract_first_json_object(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    # Common case: fenced JSON block
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fence:
        return fence.group(1)
    if text.startswith("{") and text.endswith("}"):
        return text
    # Extract the first balanced {...} candidate
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _parse_manager_decision(text: str) -> dict | None:
    candidate = _extract_first_json_object(text)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    action = data.get("action")
    if action not in {"ask_user", "run_phase", "complete"}:
        return None
    phase = data.get("phase", "") or ""
    if action == "run_phase" and phase not in {
        "brainstorm", "plan", "deepen_plan", "doc_review", "ideate",
        "work", "debug", "review", "compound", "compound_refresh",
    }:
        return None
    if action == "ask_user" and not (data.get("question") or "").strip():
        return None
    if action == "run_phase" and not (data.get("phase_input") or "").strip():
        return None
    return {
        "action": action,
        "phase": phase,
        "message": (data.get("message") or "").strip(),
        "question": (data.get("question") or "").strip(),
        "phase_input": (data.get("phase_input") or "").strip(),
        "reason": (data.get("reason") or "").strip(),
    }


def run_manager_turn(manager_messages: list, model: str) -> tuple[dict | None, str]:
    last_detail = ""
    for _ in range(4):
        response = api_call(manager_messages, model)
        if not response:
            return None, "[Manager API call failed]"
        parsed = normalize_api_response(response)
        text = parsed.get("text") or ""
        last_detail = text or parsed.get("error") or ""

        if parsed.get("tool_calls"):
            manager_messages.append({
                "role": "user",
                "content": "Do not call tools in manager mode. Return only a JSON orchestration decision.",
            })
            continue

        if _is_retryable_empty_response(parsed, manager_messages):
            manager_messages.append({
                "role": "user",
                "content": "Your last reply was empty or unparsable. Return one valid JSON decision now.",
            })
            continue

        decision = _parse_manager_decision(text)
        if text:
            manager_messages.append({"role": "assistant", "content": text})
        if decision:
            return decision, text
        manager_messages.append({
            "role": "user",
            "content": "Your previous response was invalid. Reply with valid JSON only using the required schema.",
        })
    return None, (last_detail or "[Manager response invalid]")


def _manager_fallback_decision(raw_text: str) -> dict:
    snippet = (raw_text or "").strip().replace("\n", " ")
    if len(snippet) > 240:
        snippet = snippet[:240] + "..."
    question = "I could not parse the manager decision. Reply 'retry' to continue, or restate your objective in one sentence."
    if snippet:
        question = f"I could not parse the manager decision ({snippet}). Reply 'retry' to continue, or restate your objective in one sentence."
    return {
        "action": "ask_user",
        "phase": "",
        "message": "Manager response was invalid.",
        "question": question,
        "phase_input": "",
        "reason": "Fallback recovery",
    }


def _failsafe_phase_input(phase: str, objective: str, artifacts: dict) -> str:
    plan_path = artifacts.get("plan") or ""
    brainstorm_path = artifacts.get("brainstorm") or ""
    if phase == "brainstorm":
        return f"Managed failsafe: run a focused brainstorm for this objective and save requirements. Objective: {objective}"
    if phase == "plan":
        return f"Managed failsafe: create a concrete implementation plan from this objective. Objective: {objective}"
    if phase == "deepen_plan":
        if plan_path:
            return f"Managed failsafe: deepen this existing plan before work: {plan_path}"
        return "Managed failsafe: deepen the most relevant active plan before work."
    if phase == "doc_review":
        target = plan_path or brainstorm_path
        if target:
            return f"Managed failsafe: run ce-doc-review against {target} in mode:headless. Return findings as structured text."
        return "Managed failsafe: run ce-doc-review against the most recent plan or brainstorm in mode:headless."
    if phase == "ideate":
        return f"Managed failsafe: run ce-ideate to generate ranked improvement ideas for this objective. Objective: {objective}"
    if phase == "work":
        if plan_path:
            return f"Managed failsafe: execute work from this plan: {plan_path}"
        return f"Managed failsafe: execute the objective directly and maintain todos. Objective: {objective}"
    if phase == "debug":
        return f"Managed failsafe: run ce-debug systematic root-cause investigation for this objective. Objective: {objective}"
    if phase == "review":
        return "Managed failsafe: review the changes produced by the work phase using the tiered ce-code-review pipeline. Let the skill select reviewer personas based on the diff."
    if phase == "compound":
        return "Managed failsafe: document learnings and solution notes from this completed workflow."
    if phase == "compound_refresh":
        return "Managed failsafe: refresh stale or drifting learnings in .compound-engineering/learnings.md."
    return objective


def _manager_autorecover_decision(workflow_state: CEWorkflowState, raw_text: str) -> dict:
    artifacts = _workflow_artifact_snapshot()
    completed = set(workflow_state.completed_phases)
    plan_ready = bool(artifacts.get("plan") or "plan" in completed)

    if not plan_ready:
        phase = "brainstorm" if "brainstorm" not in completed else "plan"
    elif "deepen_plan" not in completed:
        phase = "deepen_plan"
    elif "work" not in completed:
        phase = "work"
    elif "review" not in completed:
        phase = "review"
    elif "compound" not in completed:
        phase = "compound"
    else:
        return {
            "action": "complete",
            "phase": "",
            "message": "Managed flow completed via failsafe sequencing.",
            "question": "",
            "phase_input": "",
            "reason": "All phases complete",
        }

    snippet = (raw_text or "").strip().replace("\n", " ")
    if len(snippet) > 220:
        snippet = snippet[:220] + "..."
    msg = f"Manager autorecovery engaged: running {phase}."
    if snippet:
        msg += f" Last manager error: {snippet}"
    return {
        "action": "run_phase",
        "phase": phase,
        "message": msg,
        "question": "",
        "phase_input": _failsafe_phase_input(phase, workflow_state.objective, artifacts),
        "reason": "Autorecover from manager failure",
    }


_IMPLEMENTATION_VERBS = (
    "implement", "build", "code", "write", "add", "create", "fix",
    "refactor", "ship", "deploy", "wire", "integrate", "develop",
    "make ", "update the code", "modify the code", "change the code",
    "work on", "execute",
)

_PLANNING_ONLY_VERBS = (
    "brainstorm", "ideate", "explore", "research", "draft a plan",
    "write a plan", "outline", "design doc", "discuss",
)


def _objective_requires_implementation(objective: str) -> bool:
    text = (objective or "").lower()
    if not text:
        return False
    if any(v in text for v in _PLANNING_ONLY_VERBS) and not any(
        v in text for v in ("then implement", "then build", "then work", "then ship")
    ):
        # Planning-only intent overrides implementation verbs UNLESS chained.
        if not any(f" {v}" in f" {text}" for v in ("implement", "build", "ship", "deploy", "fix")):
            return False
    return any(v in text for v in _IMPLEMENTATION_VERBS)


def _deterministic_completion_block(workflow_state: CEWorkflowState) -> dict | None:
    """Return an override decision if the manager's 'complete' is premature.

    Compares the original objective against completed_phases and produces a
    run_phase decision that nudges the flow back on track.
    """
    completed = set(workflow_state.completed_phases)
    artifacts = _workflow_artifact_snapshot()
    objective = workflow_state.objective or ""
    needs_impl = _objective_requires_implementation(objective)

    next_phase: str | None = None
    reason = ""
    # Only block if implementation is required and the work phase has never run.
    # Once work has executed at least once, trust the manager's completion claim --
    # the user can verify and re-engage if needed. Do NOT force review/compound for
    # small fixes; that thrashes the flow on simple bug fixes.
    if needs_impl and "work" not in completed:
        if not artifacts.get("plan") and "plan" not in completed:
            next_phase = "plan"
            reason = "Objective requires implementation but no plan exists yet."
        else:
            next_phase = "work"
            reason = "Objective requires implementation but the work phase has not run."

    if not next_phase:
        return None

    phase_input = _failsafe_phase_input(next_phase, objective, artifacts)
    return {
        "action": "run_phase",
        "phase": next_phase,
        "message": f"Completion rejected: {reason} Resuming with {next_phase}.",
        "question": "",
        "phase_input": phase_input,
        "reason": reason,
    }


_COMPLETION_VERIFY_PROMPT = (
    "You are verifying whether the managed Compound Engineering flow has satisfied "
    "the user's original objective.\n\n"
    "Original user objective:\n{objective}\n\n"
    "Completed phases: {completed}\n"
    "Workflow artifacts:\n{artifacts}\n\n"
    "Manager's proposed final summary:\n{final_message}\n\n"
    "DEFAULT TO 'complete' UNLESS THERE IS CLEAR EVIDENCE THE WORK WAS NOT DONE.\n"
    "Only reply 'incomplete' when one of these is true:\n"
    "  - The objective explicitly required code changes but the work phase never ran\n"
    "    (work is NOT in the completed phases list).\n"
    "  - The final summary itself admits the task is unfinished, blocked, or only\n"
    "    partially done.\n"
    "  - The summary describes only planning/discussion when the user asked for a\n"
    "    real change.\n\n"
    "Do NOT reply 'incomplete' just because:\n"
    "  - You cannot personally see test results or run the code.\n"
    "  - You think more polishing, review, or extra phases would be nice.\n"
    "  - The summary is brief.\n"
    "  - You did not personally inspect the diff.\n"
    "If work has run and the summary claims the fix or feature is done, trust it.\n\n"
    "Reply with JSON only using this schema:\n"
    "{{\n"
    '  "verdict": "complete" | "incomplete",\n'
    '  "missing": "what is still missing if incomplete, else empty string",\n'
    '  "next_phase": "brainstorm|plan|deepen_plan|work|review|compound or empty",\n'
    '  "next_phase_input": "instruction for the next phase, or empty"\n'
    "}}"
)


def _parse_completion_verdict(text: str) -> dict | None:
    candidate = _extract_first_json_object(text)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    verdict = (data.get("verdict") or "").strip().lower()
    if verdict not in {"complete", "incomplete"}:
        return None
    return {
        "verdict": verdict,
        "missing": (data.get("missing") or "").strip(),
        "next_phase": (data.get("next_phase") or "").strip(),
        "next_phase_input": (data.get("next_phase_input") or "").strip(),
    }


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
    for _ in range(3):
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
    return None


# Manager JSON uses underscores for multi-word phases (doc_review,
# compound_refresh) so they are valid identifier-style values. The omlx
# CE mode names use hyphens to match upstream skill directory names
# (doc-review, compound-refresh). Translate when handing off.
_MANAGER_PHASE_TO_CE_MODE = {
    "deepen_plan": "plan",
    "doc_review": "doc-review",
    "compound_refresh": "compound-refresh",
}


def _phase_to_ce_mode(phase: str) -> str:
    """Translate a manager-decision phase name into the omlx CE mode name."""
    return _MANAGER_PHASE_TO_CE_MODE.get(phase, phase)


def _build_manager_handoff(manager_messages: list, phase: str, phase_input: str) -> str:
    phase_label = "plan (deepening pass)" if phase == "deepen_plan" else phase
    recent_messages = manager_messages[-6:]
    lines = [
        "[FLOW manager handoff]",
        f"Target phase: {phase_label}",
        "Use this as orchestration context from the manager layer. Execute the phase normally, but honor this handoff.",
        "",
        "Recent manager context:",
    ]
    for message in recent_messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        if isinstance(content, list):
            content = json.dumps(content)
        content = str(content).strip()
        if len(content) > 700:
            content = content[:700] + "..."
        lines.append(f"- {role}: {content}")
    lines.extend([
        "",
        "Manager instruction for this phase:",
        phase_input,
    ])
    return "\n".join(lines)


def set_work_dir(path: str):
    global WORK_DIR
    WORK_DIR = os.path.abspath(path)
    os.chdir(WORK_DIR)


def resolve(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(WORK_DIR, path)


# ── Compound Engineering Setup Detection ────────────────────────────────────
# Mirrors the artifacts that the vendored ce-setup skill creates so we can
# detect whether the user has ever run /ce:setup in the current repo.

def _git_repo_root(start: str | None = None) -> str | None:
    """Return the git repo root containing `start`, or None if not in a repo."""
    cwd = start or WORK_DIR
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def ce_setup_status(repo_dir: str | None = None) -> dict:
    """Inspect the repo for ce-setup artifacts.

    Returns a dict with:
      - ok: True iff config.local.yaml exists and the obsolete file is gone
      - in_git_repo: True iff `repo_dir` is inside a git working tree
      - repo_root: absolute path to the git root (or `repo_dir` fallback)
      - has_local_config: bool, .compound-engineering/config.local.yaml exists
      - has_example_config: bool, .compound-engineering/config.local.example.yaml exists
      - has_obsolete_local_md: bool, root-level compound-engineering.local.md is present
      - missing: list[str] of human-readable issues
    """
    root = _git_repo_root(repo_dir) or os.path.abspath(repo_dir or WORK_DIR)
    in_git = _git_repo_root(repo_dir) is not None
    ce_dir = os.path.join(root, ".compound-engineering")
    local_cfg = os.path.join(ce_dir, "config.local.yaml")
    example_cfg = os.path.join(ce_dir, "config.local.example.yaml")
    obsolete = os.path.join(root, "compound-engineering.local.md")

    has_local = os.path.isfile(local_cfg)
    has_example = os.path.isfile(example_cfg)
    has_obsolete = os.path.isfile(obsolete)

    missing = []
    if not has_local:
        missing.append("`.compound-engineering/config.local.yaml` is missing")
    if not has_example:
        missing.append("`.compound-engineering/config.local.example.yaml` is missing")
    if has_obsolete:
        missing.append("obsolete `compound-engineering.local.md` is still present")

    return {
        "ok": has_local and not has_obsolete,
        "in_git_repo": in_git,
        "repo_root": root,
        "has_local_config": has_local,
        "has_example_config": has_example,
        "has_obsolete_local_md": has_obsolete,
        "missing": missing,
    }


def format_ce_setup_warning(status: dict) -> list[str]:
    """Render a multi-line yellow warning describing what /ce:setup would fix."""
    lines = [
        "[Compound Engineering] /ce:setup has not been run for this repo.",
        f"  Repo: {status['repo_root']}",
    ]
    for item in status["missing"]:
        lines.append(f"  - {item}")
    lines.append("  Run /ce:setup to bootstrap the project config and verify dependencies.")
    return lines


# ── Tool Implementations ────────────────────────────────────────────────────

def tool_run_command(command: str) -> str:
    dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
    for d in dangerous:
        if d in command:
            return f"BLOCKED: Refusing to run dangerous command containing '{d}'"
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=WORK_DIR
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if not output:
            output = f"(command completed with exit code {result.returncode})"
        if len(output) > 8000:
            output = output[:4000] + "\n\n... [truncated] ...\n\n" + output[-4000:]
        return output
    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out after 120 seconds"
    except Exception as e:
        return f"ERROR: {e}"


def tool_read_file(path: str, line_start: int = None, line_end: int = None) -> str:
    try:
        full = resolve(path)
        with open(full, "r") as f:
            lines = f.readlines()
        if line_start or line_end:
            s = (line_start or 1) - 1
            e = line_end or len(lines)
            selected = lines[s:e]
            header = f"[{path} lines {s+1}-{min(e, len(lines))} of {len(lines)}]\n"
            return header + "".join(selected)
        content = "".join(lines)
        if len(content) > 12000:
            return f"[{path} - {len(lines)} lines, showing first 200]\n" + "".join(lines[:200])
        return content
    except Exception as e:
        return f"ERROR: {e}"


def _unescape_content(s: str) -> str:
    """Fix double-escaped newlines/tabs from model output."""
    # If the string has literal \n but zero real newlines, the model double-escaped
    if "\\n" in s and "\n" not in s:
        s = s.replace("\\n", "\n").replace("\\t", "\t")
    return s


def tool_write_file(path: str, content: str) -> str:
    try:
        content = _unescape_content(content)
        full = resolve(path)
        dirname = os.path.dirname(full)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_replace_in_file(path: str, old_string: str, new_string: str) -> str:
    try:
        old_string = _unescape_content(old_string)
        new_string = _unescape_content(new_string)
        full = resolve(path)
        with open(full, "r") as f:
            content = f.read()
        count = content.count(old_string)
        if count == 0:
            return f"ERROR: old_string not found in {path}"
        if count > 1:
            return f"ERROR: old_string found {count} times in {path}. Must match exactly once."
        new_content = content.replace(old_string, new_string, 1)
        with open(full, "w") as f:
            f.write(new_content)
        return f"Replaced in {path} ({len(old_string)} chars -> {len(new_string)} chars)"
    except Exception as e:
        return f"ERROR: {e}"


def tool_list_directory(path: str = ".") -> str:
    try:
        full = resolve(path)
        entries = sorted(os.listdir(full))
        result = []
        for e in entries:
            fp = os.path.join(full, e)
            if os.path.isdir(fp):
                result.append(f"  {e}/")
            else:
                size = os.path.getsize(fp)
                result.append(f"  {e}  ({size} bytes)")
        return f"[{path}] {len(entries)} items:\n" + "\n".join(result)
    except Exception as e:
        return f"ERROR: {e}"


def tool_search_files(pattern: str, path: str = ".", include: str = None) -> str:
    cmd = f"grep -rn '{pattern}' {resolve(path)}"
    if include:
        cmd = f"grep -rn --include='{include}' '{pattern}' {resolve(path)}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=WORK_DIR)
    output = result.stdout
    if not output:
        return "No matches found."
    lines = output.strip().split("\n")
    if len(lines) > 50:
        return "\n".join(lines[:50]) + f"\n\n... ({len(lines)} total matches, showing first 50)"
    return output


def tool_git_status() -> str:
    return tool_run_command("git status && echo '---' && git branch --show-current")


def tool_git_diff(staged: bool = False, file: str = None) -> str:
    cmd = "git diff"
    if staged:
        cmd += " --staged"
    if file:
        cmd += f" -- {file}"
    return tool_run_command(cmd)


def tool_git_commit_and_push(message: str, files: str = None) -> str:
    if files:
        stage = f"git add {files}"
    else:
        stage = "git add -A"
    branch_result = subprocess.run(
        "git branch --show-current", shell=True, capture_output=True, text=True, cwd=WORK_DIR
    )
    branch = branch_result.stdout.strip() or "main"
    cmd = f"{stage} && git commit -m '{message}' && git push origin {branch}"
    return tool_run_command(cmd)


def tool_git_pull(branch: str = None) -> str:
    if branch:
        return tool_run_command(f"git pull origin {branch}")
    return tool_run_command("git pull")


def tool_git_log(count: int = 10, file: str = None, since: str = None) -> str:
    cmd = f"git log --oneline --no-decorate -n {count}"
    if since:
        cmd += f" --since='{since}'"
    if file:
        cmd += f" -- {file}"
    return tool_run_command(cmd)


def tool_ce_read_learnings() -> str:
    path = resolve(LEARNINGS_FILE)
    if not os.path.exists(path):
        return "No learnings file found. Use ce_write_learning to create one."
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"ERROR: {e}"


def tool_ce_write_learning(title: str, problem: str, solution: str, tags: str = "") -> str:
    path = resolve(LEARNINGS_FILE)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n\n## {title}\n\n"
    entry += f"**Date:** {timestamp}\n"
    if tags:
        entry += f"**Tags:** {tags}\n"
    entry += f"\n**Problem:**\n{problem}\n\n**Solution:**\n{solution}\n"
    entry += "---"

    if not os.path.exists(path):
        header = f"# Compound Engineering Learnings\n\nProject learnings that make future work easier.\n\n---{entry}"
        with open(path, "w") as f:
            f.write(header)
    else:
        with open(path, "a") as f:
            f.write(entry)
    return f"Learning '{title}' added to {LEARNINGS_FILE}"


def tool_ce_manage_todos(action: str, item: str = None) -> str:
    global SESSION_TODOS
    if action == "list":
        if not SESSION_TODOS:
            return "No todos. Use add to create some."
        lines = []
        for i, t in enumerate(SESSION_TODOS):
            status = "[x]" if t["done"] else "[ ]"
            lines.append(f"  {i+1}. {status} {t['text']}")
        return "Current todos:\n" + "\n".join(lines)
    elif action == "add":
        if not item:
            return "ERROR: item is required for add"
        SESSION_TODOS.append({"text": item, "done": False})
        if _tui_instance:
            _tui_instance.needs_redraw = True
        return f"Added todo #{len(SESSION_TODOS)}: {item}"
    elif action == "complete":
        if not item:
            return "ERROR: item index required"
        try:
            idx = int(item) - 1
            if 0 <= idx < len(SESSION_TODOS):
                SESSION_TODOS[idx]["done"] = True
                if _tui_instance:
                    _tui_instance.needs_redraw = True
                return f"Completed: {SESSION_TODOS[idx]['text']}"
            return "ERROR: invalid index"
        except ValueError:
            return "ERROR: item must be a number"
    elif action == "clear":
        count = len(SESSION_TODOS)
        SESSION_TODOS.clear()
        if _tui_instance:
            _tui_instance.needs_redraw = True
        return f"Cleared {count} todos. Ready for a fresh task."
    elif action == "remove":
        return "ERROR: Use 'complete' to mark items done instead of removing them. Completed items serve as a progress record. Use 'clear' only when starting an entirely new task."
    return f"ERROR: unknown action '{action}'"


def _find_ce_dir(subdir: str) -> str:
    """Locate the CE subdirectory (plans/, brainstorms/, etc.) under docs/ or referenceDocs/."""
    for parent in ["docs", "referenceDocs"]:
        candidate = resolve(os.path.join(parent, subdir))
        if os.path.isdir(candidate):
            return candidate
    # Fallback: try docs/ first, then referenceDocs/, create under docs/ if neither exists
    for parent in ["docs", "referenceDocs"]:
        if os.path.isdir(resolve(parent)):
            return resolve(os.path.join(parent, subdir))
    return resolve(os.path.join("docs", subdir))


def _dated_filename(prefix: str = "plan") -> str:
    """Generate a dated filename like plan-2026-04-12.md."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    return f"{prefix}-{date_str}.md"


def _dedupe_filename(directory: str, filename: str) -> str:
    """If filename exists, append -2, -3, etc."""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return filename
    base, ext = os.path.splitext(filename)
    i = 2
    while os.path.exists(os.path.join(directory, f"{base}-{i}{ext}")):
        i += 1
    return f"{base}-{i}{ext}"


def tool_ce_create_plan(action: str, name: str = None, content: str = None) -> str:
    plans_dir = _find_ce_dir("plans")
    if action == "list":
        if not os.path.exists(plans_dir):
            return f"No plans directory found. Checked docs/plans/ and referenceDocs/plans/."
        files = [f for f in os.listdir(plans_dir) if f.endswith(".md")]
        if not files:
            return "No plans found."
        return "Plans:\n" + "\n".join(f"  - {f}" for f in sorted(files))
    elif action == "read":
        if not name:
            return "ERROR: name required"
        path = os.path.join(plans_dir, name)
        if not os.path.exists(path):
            return f"ERROR: plan '{name}' not found in {plans_dir}"
        with open(path, "r") as f:
            return f.read()
    elif action == "create":
        if not content:
            return "ERROR: content required"
        # Auto-generate dated name if not provided
        if not name:
            name = _dated_filename("plan")
        if not name.endswith(".md"):
            name += ".md"
        os.makedirs(plans_dir, exist_ok=True)
        name = _dedupe_filename(plans_dir, name)
        path = os.path.join(plans_dir, name)
        with open(path, "w") as f:
            f.write(content)
        rel = os.path.relpath(path, WORK_DIR)
        _set_session_doc("plan", rel)
        return f"Plan created: {rel}"
    return f"ERROR: unknown action '{action}'"


def tool_ce_save_doc(doc_type: str, content: str, name: str = None) -> str:
    """Save a CE document to the appropriate subdirectory."""
    type_to_dir = {
        "brainstorm": "brainstorms",
        "review": "reviews",
        "solution": "solutions",
        "todo": "todos",
    }
    subdir = type_to_dir.get(doc_type)
    if not subdir:
        return f"ERROR: doc_type must be one of: {', '.join(type_to_dir.keys())}"
    target_dir = _find_ce_dir(subdir)
    if not name:
        name = _dated_filename(doc_type)
    if not name.endswith(".md"):
        name += ".md"
    os.makedirs(target_dir, exist_ok=True)
    name = _dedupe_filename(target_dir, name)
    path = os.path.join(target_dir, name)
    with open(path, "w") as f:
        f.write(content)
    rel = os.path.relpath(path, WORK_DIR)
    _set_session_doc(doc_type, rel)

    # R4: Update temp file indexes when a solution is saved
    if doc_type == "solution":
        _update_temp_indexes(rel, content)

    return f"{doc_type.capitalize()} saved: {rel}"


def tool_ce_mark_step(file_path: str, step_text: str) -> str:
    """Mark a step as done in a plan or review document."""
    path = resolve(file_path)
    if not os.path.exists(path):
        return f"ERROR: file not found: {file_path}"
    with open(path, "r") as f:
        lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if step_text in line and "[ ]" in line:
            lines[i] = line.replace("[ ]", "[x]", 1)
            found = True
            break
    if not found:
        return f"ERROR: no unchecked step matching '{step_text}' found in {file_path}"
    with open(path, "w") as f:
        f.writelines(lines)
    return f"Marked complete: {step_text}"


def tool_ce_list_docs(doc_type: str = None) -> str:
    """List all CE documents across subdirectories (recursively)."""
    subdirs = ["brainstorms", "plans", "reviews", "solutions", "todos"]
    if doc_type:
        if doc_type not in subdirs:
            return f"ERROR: doc_type must be one of: {', '.join(subdirs)}"
        subdirs = [doc_type]
    results = []
    for subdir in subdirs:
        for parent in ["docs", "referenceDocs"]:
            d = resolve(os.path.join(parent, subdir))
            if os.path.isdir(d):
                # Walk recursively to find .md files in nested subdirectories
                found_files = []
                found_dirs = []
                for entry in sorted(os.listdir(d)):
                    full = os.path.join(d, entry)
                    if os.path.isdir(full):
                        found_dirs.append(entry)
                        # List .md files inside subdirectory
                        sub_files = sorted(f for f in os.listdir(full) if f.endswith(".md"))
                        for sf in sub_files:
                            found_files.append(f"{entry}/{sf}")
                    elif entry.endswith(".md"):
                        found_files.append(entry)
                if found_files or found_dirs:
                    results.append(f"\n{parent}/{subdir}/:")
                    if found_dirs:
                        results.append(f"  subdirectories: {', '.join(found_dirs)}")
                    for f in found_files:
                        results.append(f"  - {f}")
    if not results:
        return "No CE documents found in docs/ or referenceDocs/."
    return "\n".join(results)


def tool_ce_scan_solution_headers(directory: str = None) -> str:
    """Scan solution files. With no args, returns the full directory tree (all subdirectory
    names and all filenames inside them) so you can identify relevant files by name.
    With a directory arg, returns the first 20 lines (title, tags, summary) of each file
    in that subdirectory."""
    results = []
    for parent in ["docs", "referenceDocs"]:
        sol_dir = resolve(os.path.join(parent, "solutions"))
        if not os.path.isdir(sol_dir):
            continue
        if directory:
            # Scan specific subdirectory — return headers of all files
            target = os.path.join(sol_dir, directory)
            if os.path.isdir(target):
                for f in sorted(os.listdir(target)):
                    if f.endswith(".md"):
                        fpath = os.path.join(target, f)
                        rel = os.path.relpath(fpath, WORK_DIR)
                        header = _read_head(fpath, 20)
                        results.append(f"\n--- {rel} ---\n{header}")
            # Also check if it's a file directly
            elif os.path.isfile(target + ".md"):
                fpath = target + ".md"
                rel = os.path.relpath(fpath, WORK_DIR)
                header = _read_head(fpath, 20)
                results.append(f"\n--- {rel} ---\n{header}")
            if not results:
                return f"No solution docs found in '{directory}'. Call with no args to see the full directory tree."
        else:
            # Full directory tree — show all subdirectories and all filenames inside them
            entries = sorted(os.listdir(sol_dir)) if os.path.isdir(sol_dir) else []
            top_files = [e for e in entries if e.endswith(".md") and os.path.isfile(os.path.join(sol_dir, e))]
            subdirs = [e for e in entries if os.path.isdir(os.path.join(sol_dir, e))]
            if top_files:
                results.append(f"\n{parent}/solutions/:")
                for f in top_files:
                    results.append(f"  - {f}")
            for sd in subdirs:
                sd_path = os.path.join(sol_dir, sd)
                sd_files = sorted(f for f in os.listdir(sd_path) if f.endswith(".md"))
                results.append(f"\n{parent}/solutions/{sd}/:")
                if sd_files:
                    for f in sd_files:
                        results.append(f"  - {f}")
                else:
                    results.append(f"  (empty)")
    if not results:
        return "No solution docs found in docs/solutions/ or referenceDocs/solutions/."
    return "\n".join(results)
    if not results:
        return "No solution docs found."
    return "\n".join(results)


def _read_head(filepath: str, n: int = 20) -> str:
    """Read first n lines of a file."""
    try:
        with open(filepath, "r") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= n:
                    break
                lines.append(line)
            return "".join(lines)
    except Exception as e:
        return f"(error reading: {e})"


_TEMP_FILES = [
    ".temp_solutionDirs",
    ".temp_tagList",
    ".temp_relevantTags",
    ".temp_relevantSolutions",
]


def _rotate_temp_files():
    """Rotate .temp_* session files to .old on startup, preserving prior session data."""
    for name in _TEMP_FILES:
        src = os.path.join(WORK_DIR, name)
        if os.path.exists(src):
            dst = src + ".old"
            try:
                os.replace(src, dst)
            except OSError:
                pass  # best-effort; don't block startup


def _extract_tags(content: str) -> list:
    """Extract tags from the first 20 lines of solution content.
    Supports YAML frontmatter (tags:\\n  - tag) and markdown bold (**tags:** t1, t2)."""
    lines = content.split("\n", 20)[:20]
    tags = []
    in_yaml_tags = False
    for line in lines:
        stripped = line.strip()
        if in_yaml_tags:
            if stripped.startswith("- "):
                tags.append(stripped[2:].strip())
                continue
            else:
                in_yaml_tags = False
        if stripped.lower() == "tags:":
            in_yaml_tags = True
            continue
        if stripped.lower().startswith("**tags:**"):
            raw = stripped[len("**tags:**"):].strip()
            tags.extend(t.strip() for t in raw.split(",") if t.strip())
    return tags


def _update_temp_indexes(rel_path: str, content: str):
    """Append a new solution's info to .temp_solutionDirs and .temp_tagList if they exist."""
    dirs_file = os.path.join(WORK_DIR, ".temp_solutionDirs")
    tags_file = os.path.join(WORK_DIR, ".temp_tagList")
    try:
        if os.path.exists(dirs_file):
            with open(dirs_file, "a") as f:
                f.write(rel_path + "\n")
        if os.path.exists(tags_file):
            tags = _extract_tags(content)
            tag_str = ", ".join(tags) if tags else ""
            with open(tags_file, "a") as f:
                f.write(f"{rel_path}: {tag_str}\n")
    except OSError:
        pass  # best-effort; don't fail the save


TOOL_DISPATCH = {
    "run_command": lambda args: tool_run_command(args["command"]),
    "read_file": lambda args: tool_read_file(args["path"], args.get("line_start"), args.get("line_end")),
    "write_file": lambda args: tool_write_file(args["path"], args["content"]),
    "replace_in_file": lambda args: tool_replace_in_file(args["path"], args["old_string"], args["new_string"]),
    "list_directory": lambda args: tool_list_directory(args.get("path", ".")),
    "search_files": lambda args: tool_search_files(args["pattern"], args.get("path", "."), args.get("include")),
    "git_status": lambda args: tool_git_status(),
    "git_diff": lambda args: tool_git_diff(args.get("staged", False), args.get("file")),
    "git_commit_and_push": lambda args: tool_git_commit_and_push(args["message"], args.get("files")),
    "git_pull": lambda args: tool_git_pull(args.get("branch")),
    "git_log": lambda args: tool_git_log(args.get("count", 10), args.get("file"), args.get("since")),
    "ce_read_learnings": lambda args: tool_ce_read_learnings(),
    "ce_write_learning": lambda args: tool_ce_write_learning(args["title"], args["problem"], args["solution"], args.get("tags", "")),
    "ce_manage_todos": lambda args: tool_ce_manage_todos(args["action"], args.get("item")),
    "ce_create_plan": lambda args: tool_ce_create_plan(args["action"], args.get("name"), args.get("content")),
    "ce_save_doc": lambda args: tool_ce_save_doc(args["doc_type"], args["content"], args.get("name")),
    "ce_mark_step": lambda args: tool_ce_mark_step(args["file_path"], args["step_text"]),
    "ce_list_docs": lambda args: tool_ce_list_docs(args.get("doc_type")),
    "ce_scan_solution_headers": lambda args: tool_ce_scan_solution_headers(args.get("directory")),
    "ce_run_agent": lambda args: tool_ce_run_agent(args["agent_name"], args["task"]),
    "ce_list_agents": lambda args: tool_ce_list_agents(),
}


# ── Compound Engineering Vendor Loader ───────────────────────────────────────
#
# omlx_agent vendors the official EveryInc/compound-engineering-plugin
# agents/ and skills/ verbatim under ./compound-engineering/. This loader
# parses the YAML frontmatter + Markdown body so we can use upstream prompts
# instead of paraphrasing them inline. Refresh with ./sync-ce-vendor.sh.

CE_VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "compound-engineering"
)
_CE_AGENT_CACHE: dict[str, dict] = {}
_CE_SKILL_CACHE: dict[str, dict] = {}

# Map omlx_agent's short mode names to vendored skill directory names.
# When invoked as /ce:<mode>, we load the corresponding skill's SKILL.md
# body and inject it as the active system prompt.
CE_MODE_TO_SKILL = {
    "brainstorm": "ce-brainstorm",
    "ideate": "ce-ideate",
    "plan": "ce-plan",
    "work": "ce-work",
    "debug": "ce-debug",
    "review": "ce-code-review",
    "code-review": "ce-code-review",
    "compound": "ce-compound",
    "compound-refresh": "ce-compound-refresh",
    "doc-review": "ce-doc-review",
    "commit": "ce-commit",
    "pr-description": "ce-pr-description",
    "commit-push-pr": "ce-commit-push-pr",
    "setup": "ce-setup",
    "test-browser": "ce-test-browser",
    "worktree": "ce-worktree",
    "clean-gone-branches": "ce-clean-gone-branches",
    "optimize": "ce-optimize",
    "sessions": "ce-sessions",
    "demo-reel": "ce-demo-reel",
    "report-bug": "ce-report-bug",
    "resolve-pr-feedback": "ce-resolve-pr-feedback",
    "polish-beta": "ce-polish-beta",
    "lfg": "lfg",
}

# omlx_agent host-adaptation footer appended to every vendored prompt.
# The vendored prompts assume Claude Code's host tools (Read, Write, Bash,
# Grep, Glob, Edit, AskUserQuestion, Task subagent dispatch). This footer
# tells the model how those map to omlx_agent's actual tool surface.
CE_HOST_ADAPTATION = """

---

## omlx_agent host adaptation

You are running inside omlx_agent (a single-file local-LLM coding agent),
not Claude Code. The skill/agent prompt above was authored for Claude Code's
host tools. Translate as follows:

- `Read` -> `read_file`
- `Write` -> `write_file`
- `Edit` / `MultiEdit` -> `replace_in_file`
- `Bash` -> `run_command`
- `Grep` / `Glob` -> `search_files`
- `Task` (subagent dispatch) -> `ce_run_agent` (spawn a vendored sub-agent
  with isolated context; returns the sub-agent's final text/JSON)
- `AskUserQuestion` / `request_user_input` / `ask_user` -> respond with a
  plain-text question and stop. The user will reply on the next turn.
- `WebSearch` / `WebFetch` -> not available; report the limitation in your
  output instead of attempting the call.
- `TodoWrite` -> `ce_manage_todos`

CE document storage uses `docs/{brainstorms,plans,reviews,solutions,todos}/`
via `ce_save_doc`, matching the upstream convention. Project learnings live
in `.compound-engineering/learnings.md` via `ce_read_learnings` and
`ce_write_learning`.

When the upstream skill says "spawn parallel subagents," dispatch them
sequentially via `ce_run_agent` (omlx_agent runs one model at a time on
local hardware). Aggregate their JSON outputs yourself.
"""


def _parse_ce_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter (key: value lines between two ---) from a CE file.

    Returns (frontmatter_dict, body_string). If no frontmatter delimiter is
    found, returns ({}, original_text).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text

    fm: dict = {}
    current_key: str | None = None
    for raw in lines[1:end_idx]:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        # key: value on one line
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", raw)
        if m:
            current_key = m.group(1)
            value = m.group(2).strip()
            # Strip surrounding quotes (single or double) if balanced
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            fm[current_key] = value
        elif current_key and raw.startswith((" ", "\t")):
            # multi-line continuation -- append with single space
            fm[current_key] = (fm.get(current_key, "") + " " + raw.strip()).strip()

    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    return fm, body


def _load_ce_doc(path: str) -> dict | None:
    """Load and parse a vendored .agent.md or SKILL.md file."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    frontmatter, body = _parse_ce_frontmatter(text)
    return {
        "name": frontmatter.get("name", ""),
        "description": frontmatter.get("description", ""),
        "model": frontmatter.get("model", "inherit"),
        "tools": frontmatter.get("tools", ""),
        "argument_hint": frontmatter.get("argument-hint", ""),
        "body": body,
        "path": path,
    }


def load_ce_skill(skill_name: str) -> dict | None:
    """Load a vendored skill by directory name (e.g. 'ce-code-review'). Cached."""
    if skill_name in _CE_SKILL_CACHE:
        return _CE_SKILL_CACHE[skill_name]
    path = os.path.join(CE_VENDOR_DIR, "skills", skill_name, "SKILL.md")
    doc = _load_ce_doc(path)
    if doc is not None:
        _CE_SKILL_CACHE[skill_name] = doc
    return doc


def load_ce_agent(agent_name: str) -> dict | None:
    """Load a vendored agent by name (e.g. 'ce-correctness-reviewer'). Cached."""
    if agent_name in _CE_AGENT_CACHE:
        return _CE_AGENT_CACHE[agent_name]
    path = os.path.join(CE_VENDOR_DIR, "agents", f"{agent_name}.agent.md")
    doc = _load_ce_doc(path)
    if doc is not None:
        _CE_AGENT_CACHE[agent_name] = doc
    return doc


def list_ce_agents() -> list[str]:
    """List all vendored agent names."""
    agents_dir = os.path.join(CE_VENDOR_DIR, "agents")
    if not os.path.isdir(agents_dir):
        return []
    out = []
    for fn in sorted(os.listdir(agents_dir)):
        if fn.endswith(".agent.md"):
            out.append(fn[: -len(".agent.md")])
    return out


def list_ce_skills() -> list[str]:
    """List all vendored skill names."""
    skills_dir = os.path.join(CE_VENDOR_DIR, "skills")
    if not os.path.isdir(skills_dir):
        return []
    return sorted(
        d for d in os.listdir(skills_dir)
        if os.path.isfile(os.path.join(skills_dir, d, "SKILL.md"))
    )


def get_ce_prompt(mode: str) -> str:
    """Return the system prompt body for /ce:<mode>.

    Resolution order:
      1. Vendored skill (CE_MODE_TO_SKILL[mode] -> SKILL.md body + host adapter)
      2. Legacy inline CE_PROMPTS dict (paraphrased fallback for any mode
         that loses its vendor file mid-refresh)
    """
    skill_name = CE_MODE_TO_SKILL.get(mode)
    if skill_name:
        skill = load_ce_skill(skill_name)
        if skill and skill.get("body"):
            return skill["body"] + CE_HOST_ADAPTATION
    return CE_PROMPTS.get(mode, "")


def all_ce_modes() -> list[str]:
    """Union of vendored skill modes and legacy inline modes, longest-first.

    Sorting longest-first ensures that prefix matching in the slash-command
    handler picks /ce:commit-push-pr before /ce:commit, /ce:doc-review
    before /ce:debug, etc.
    """
    modes = set(CE_MODE_TO_SKILL.keys()) | set(CE_PROMPTS.keys())
    # ce_mode availability requires either a vendored skill or a legacy prompt.
    available = [m for m in modes if get_ce_prompt(m)]
    return sorted(available, key=lambda m: (-len(m), m))


# ── Compound Engineering Workflow Prompts (legacy fallback) ─────────────────
#
# These inline prompts predate the vendor loader. They remain as a safety
# net for any /ce:<mode> whose vendored SKILL.md is missing or fails to
# parse. New work should add to compound-engineering/skills/ instead.

CE_PROMPTS = {
    "brainstorm": """You are now in BRAINSTORM mode. Your job is to explore requirements and approaches through collaborative dialogue before writing code.

Workflow:
1. First, use ce_list_docs to check for existing brainstorms on this topic
2. Read ce_read_learnings to check for relevant past solutions and known gotchas
3. Ask clarifying questions about the user's idea (scope, constraints, edge cases)
4. Propose 2-3 approaches with tradeoffs
4. Converge on a requirements document
5. Save the brainstorm using ce_save_doc(doc_type="brainstorm", content=...)
6. When ready, suggest switching to /ce:plan

CRITICAL: You MUST save the brainstorm to a file using ce_save_doc before concluding. This enables resumability if the session is interrupted.

Guidelines:
- Challenge assumptions constructively
- Surface hidden complexity early
- Keep the conversation focused and productive
- When ceremony is not needed (simple/clear task), say so and suggest skipping to /ce:work

Output a short requirements summary when the brainstorm concludes.""",

    "plan": """You are now in PLAN mode. Create a structured implementation plan.

Workflow:
1. Use ce_list_docs to check for existing plans and brainstorms
2. Read ce_read_learnings for relevant past solutions and known patterns
3. Read any existing brainstorm/requirements context

Solution Context (run BEFORE main work):
If .temp_relevantSolutions exists, read it and load the listed solution files as reference, then skip to step 4.
Otherwise, if docs/solutions/ or referenceDocs/solutions/ exists, run this pipeline:
  a. Call ce_scan_solution_headers() with no args to get the solutions directory tree. Write one repo-relative file path per line to .temp_solutionDirs.
  b. For subdirectories whose names look relevant to the brainstorm's problem statement, call ce_scan_solution_headers(directory=<subdir>) to get headers. Extract tags and write each as "path: tag1, tag2" to .temp_tagList.
  c. Select tags relevant to the brainstorm's problem statement. Write selected tags (one per line) to .temp_relevantTags.
  d. Find solution files matching any selected tag. Write matching paths to .temp_relevantSolutions.
  e. Read the matched solution files as reference material.
If no solutions directory exists, skip the pipeline entirely.

3. Break the work into ordered, concrete steps
4. Identify risks and dependencies
5. Estimate relative complexity per step
6. Save the plan using ce_create_plan (auto-generates a dated filename)

CRITICAL WRITING RULES:
- You MUST write the plan to a file. Summarizing in chat is NOT enough.
- If the user specified a file path, write to that file using write_file.
- If DEEPENING an existing plan, use replace_in_file to make surgical edits to specific sections rather than rewriting the entire file. This prevents truncation on large plans.
- Only use write_file for NEW plans or very short files. For anything over ~100 lines, build incrementally with replace_in_file.
- NEVER stop after analysis. The plan is not done until it is written to disk.
- Use checkbox format ([ ]) for each step so progress can be tracked with ce_mark_step.

Plan format:
# Plan: [Title]
## Goal
[One sentence]
## Steps
1. [ ] Step description (complexity: low/medium/high)
2. [ ] ...
## Risks
- Risk and mitigation
## Open Questions
- Any unresolved items

Guidelines:
- Each step should be independently verifiable
- Include test/verification steps
- When done, suggest /ce:work to begin execution""",

    "work": """You are now in WORK mode. Execute implementation systematically.

Workflow:
1. Use ce_list_docs(doc_type="plans") to find the plan, then read it
2. Read ce_read_learnings for relevant gotchas before starting work

Solution Context (run BEFORE main work):
If .temp_relevantSolutions exists, read it and load the listed solution files as reference, then skip to step 3.
Otherwise, if docs/solutions/ or referenceDocs/solutions/ exists, run this pipeline:
  a. Call ce_scan_solution_headers() with no args to get the solutions directory tree. Write one repo-relative file path per line to .temp_solutionDirs.
  b. For subdirectories whose names look relevant to the plan's goal and current step, call ce_scan_solution_headers(directory=<subdir>) to get headers. Extract tags and write each as "path: tag1, tag2" to .temp_tagList.
  c. Select tags relevant to the plan's goal and current step. Write selected tags (one per line) to .temp_relevantTags.
  d. Find solution files matching any selected tag. Write matching paths to .temp_relevantSolutions.
  e. Read the matched solution files as reference material.
If no solutions directory exists, skip the pipeline entirely.

3. Check which steps are already marked [x] (completed) -- resume from the first unchecked [ ] step
3. Create a todo list from the REMAINING unchecked steps (ce_manage_todos). This is MANDATORY -- never skip this step.
4. Work through each todo:
   a. Gather context (read files, search)
   b. Make the change
   c. Verify (run tests, check errors)
   d. Mark complete in the session todos using ce_manage_todos(action='complete')
   e. IMMEDIATELY mark the step done in the plan file using ce_mark_step
   f. NEVER remove completed todos -- they serve as a visible progress record
5. After all todos done, list the final todo state to show completion, then summarize changes

RESUMABILITY: Always use ce_mark_step to check off completed steps in the plan file as you finish each one. This way, if the session is interrupted, the next /ce:work invocation will see which steps are already done and resume from where you left off.

CRITICAL RULE -- TOOL USE IS MANDATORY:
You must use tools to make every change. NEVER show code in a message and ask the user to add it themselves.
NEVER write "Add this to...", "Insert the following...", "Here is the code to add...", "You should add...", "The change needed is..."
If you need to edit a file, call replace_in_file or write_file RIGHT NOW. No narration, no explanation first.
If you catch yourself writing markdown code blocks as instructions to the user, STOP and make the tool call instead.

Guidelines:
- One task at a time, verify before moving on
- Use replace_in_file for surgical edits, write_file for new files
- Run tests after changes when possible
- If stuck, explain the blocker and try an alternative
- Use git_commit_and_push when reaching a good checkpoint
- NEVER stop mid-work. Finish what you start.""",

    "review": """You are now in REVIEW mode. Perform a structured multi-perspective code review.

Workflow:
1. Read ce_read_learnings for known patterns and gotchas to check against

Solution Context (run BEFORE the review):
If .temp_relevantSolutions exists, read it and load the listed solution files as reference, then skip to step 2.
Otherwise, if docs/solutions/ or referenceDocs/solutions/ exists, run this pipeline:
  a. Call ce_scan_solution_headers() with no args to get the solutions directory tree. Write one repo-relative file path per line to .temp_solutionDirs.
  b. For subdirectories whose names look relevant to the diff's changed files and topics, call ce_scan_solution_headers(directory=<subdir>) to get headers. Extract tags and write each as "path: tag1, tag2" to .temp_tagList.
  c. Select tags relevant to the diff's changed file paths and topics. Write selected tags (one per line) to .temp_relevantTags.
  d. Find solution files matching any selected tag. Write matching paths to .temp_relevantSolutions.
  e. Read the matched solution files as reference material.
If no solutions directory exists, skip the pipeline entirely.

2. Get the diff: use git_diff to see all changes
3. Review from multiple perspectives:
   - **Correctness**: Logic errors, edge cases, state bugs
   - **Security**: Input validation, auth checks, injection risks
   - **Performance**: N+1 queries, unnecessary re-renders, memory leaks
   - **Maintainability**: Naming, coupling, complexity, dead code
   - **Testing**: Coverage gaps, weak assertions, missing edge cases
3. For each finding, assign confidence (high/medium/low)
4. Only report high-confidence findings
5. Suggest specific fixes with code
6. Save the review using ce_save_doc(doc_type="review", content=...)

CRITICAL: Save the review to a file using ce_save_doc so findings are preserved and actionable.

Guidelines:
- Be specific: cite file, line, and the exact issue
- Distinguish blocking issues from nice-to-haves
- If the code is good, say so briefly
""",

    "compound": """You are now in COMPOUND mode. Document what was learned to make future work easier.

Workflow:
1. Review what was just done (git_log, git_diff)
2. Read ce_read_learnings to check for existing entries -- avoid duplicating what's already documented
3. Check for any review docs with ce_list_docs(doc_type="reviews") -- if a review doc exists, read it and work through its findings
4. Identify learnings:
   - What problem was solved?
   - What was the root cause?
   - What was the solution?
   - What patterns/gotchas should be remembered?
5. Write each learning using ce_write_learning (this updates .compound-engineering/learnings.md)
6. If working from a review doc, use ce_mark_step to check off each finding as you process it
7. Check if existing learnings need updating -- use ce_read_learnings and look for entries that should be revised or merged
8. Save a solution summary using ce_save_doc(doc_type="solution", content=...)
9. IMPORTANT: After writing solution docs, always update .compound-engineering/learnings.md with key learnings so other modes can reference them without scanning solution files

RESUMABILITY: If working through a review document, mark each finding complete with ce_mark_step as you process it. This way, if interrupted, the next /ce:compound invocation will resume from where you left off.

Guidelines:
- Focus on non-obvious insights that would save time next time
- Include enough context that the learning is useful standalone
- Tag appropriately for searchability
- Keep entries concise but complete""",

    "debug": """You are now in DEBUG mode. Systematically find root causes and fix bugs.

Workflow:
1. Understand the symptoms (ask if unclear)
2. Read ce_read_learnings -- this bug or a similar pattern may already be documented
3. Form hypotheses about root cause
4. Test each hypothesis:
   a. Search for relevant code
   b. Read the code carefully
   c. Look for the specific condition that could cause the bug
4. Once root cause is found:
   a. Write a test that reproduces the bug (if testable)
   b. Fix the code
   c. Verify the fix
5. Document the fix using ce_write_learning

Guidelines:
- Trace the causal chain, do not guess
- One hypothesis at a time
- If the first fix does not work, reassess rather than piling on patches""",

    "ideate": """You are now in IDEATE mode. Discover high-impact improvements for the project.

Workflow:
1. Explore the codebase (list_directory, search_files, read key files)
2. Read ce_read_learnings for known patterns, past issues, and areas of technical debt
3. Generate 5-10 improvement ideas across categories:
   - Performance improvements
   - Code quality / tech debt
   - Developer experience
   - Feature opportunities
   - Security hardening
4. For each idea:
   - Impact: high/medium/low
   - Effort: high/medium/low
   - Confidence: how sure are you this is a real issue?
5. Rank by impact/effort ratio
6. Present top 3-5 with enough detail to act on

Guidelines:
- Ground ideas in actual code, not speculation
- Be specific about what you found and why it matters
- Filter aggressively -- only surface genuinely valuable improvements""",
}


# ── API Calls ────────────────────────────────────────────────────────────────

# Path to the oMLX server start script (lives next to this agent file).
_OMLX_START_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start-omlx.sh")
_OMLX_RESTART_LOG = "/tmp/omlx-server-restart.log"
_omlx_restart_in_progress = False
_omlx_last_restart_ts = 0.0


def _is_connection_refused(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "connection refused" in text
        or "errno 61" in text
        or "remote end closed" in text
        or "connection reset" in text
        or "incompleteread" in text
        or "broken pipe" in text
    )


def _omlx_server_alive(timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(
            f"{API_URL}/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return True
    except Exception:
        return False


def _restart_omlx_server(max_wait: float = 90.0) -> bool:
    """Restart the oMLX server via start-omlx.sh and wait for it to accept requests."""
    global _omlx_restart_in_progress, _omlx_last_restart_ts, _omlx_session_cookie
    _p = tui_print if _tui_instance else print

    if _omlx_restart_in_progress:
        # Another caller is already restarting; just wait for it.
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if _omlx_server_alive():
                return True
            time.sleep(1.0)
        return _omlx_server_alive()

    # Cooldown: don't thrash if we just restarted.
    if time.time() - _omlx_last_restart_ts < 15.0:
        return _omlx_server_alive()

    if not os.path.isfile(_OMLX_START_SCRIPT):
        _p(f"[oMLX restart aborted: {_OMLX_START_SCRIPT} not found]", C_RED if _tui_instance else 0)
        return False

    _omlx_restart_in_progress = True
    try:
        _p("[oMLX server unreachable - attempting restart via start-omlx.sh]",
           C_YELLOW if _tui_instance else 0)
        # Best-effort: kill any straggling omlx process so the new one can bind :8000.
        try:
            subprocess.run(
                ["pkill", "-f", "omlx serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
        time.sleep(1.0)

        try:
            log_fh = open(_OMLX_RESTART_LOG, "ab")
        except Exception:
            log_fh = subprocess.DEVNULL
        try:
            subprocess.Popen(
                ["/bin/bash", _OMLX_START_SCRIPT],
                stdout=log_fh, stderr=log_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=os.path.dirname(_OMLX_START_SCRIPT),
            )
        except Exception as exc:
            _p(f"[oMLX restart failed to spawn: {exc}]", C_RED if _tui_instance else 0)
            return False

        # Invalidate any cached admin session.
        _omlx_session_cookie = None

        deadline = time.time() + max_wait
        while time.time() < deadline:
            if _omlx_server_alive():
                _omlx_last_restart_ts = time.time()
                _p("[oMLX server restarted successfully]",
                   C_GREEN if _tui_instance else 0)
                return True
            time.sleep(2.0)

        _p(f"[oMLX server did not come back within {int(max_wait)}s - see {_OMLX_RESTART_LOG}]",
           C_RED if _tui_instance else 0)
        return False
    finally:
        _omlx_restart_in_progress = False


def api_call(messages: list, model: str) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": MAX_TOKENS,
    }
    data = json.dumps(payload).encode("utf-8")
    def _do_request():
        req = urllib.request.Request(
            f"{API_URL}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())

    try:
        return _do_request()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        _p = tui_print if _tui_instance else print
        if e.code == 400 and "too long" in error_body.lower():
            _p(f"[Context overflow: {error_body.strip()}]", C_YELLOW)
            return {"_context_overflow": True}
        _p(f"API Error {e.code}: {error_body}", C_RED)
        return None
    except Exception as e:
        _p = tui_print if _tui_instance else print
        if _is_connection_refused(e):
            _p(f"[oMLX connection lost: {e}]", C_YELLOW if _tui_instance else 0)
            if _restart_omlx_server():
                try:
                    return _do_request()
                except Exception as e2:
                    _p(f"Connection error after restart: {e2}", C_RED)
                    return None
        _p(f"Connection error: {e}", C_RED)
        return None


def fetch_models() -> list:
    req = urllib.request.Request(
        f"{API_URL}/models",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception as e:
        print(f"\033[31mFailed to fetch models: {e}\033[0m")
        return []


_omlx_session_cookie = None  # cached admin session cookie

def _omlx_admin_login() -> str | None:
    """Login to oMLX admin API and return session cookie value."""
    global _omlx_session_cookie
    if _omlx_session_cookie:
        return _omlx_session_cookie
    payload = json.dumps({"api_key": API_KEY, "remember": True}).encode("utf-8")

    def _attempt():
        req = urllib.request.Request(
            f"{API_URL.replace('/v1', '')}/admin/api/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            for header in resp.headers.get_all("Set-Cookie") or []:
                if "omlx_admin_session=" in header:
                    return header.split("omlx_admin_session=")[1].split(";")[0]
        return None

    try:
        cookie = _attempt()
        if cookie:
            _omlx_session_cookie = cookie
            return cookie
    except Exception as e:
        if _is_connection_refused(e) and _restart_omlx_server():
            try:
                cookie = _attempt()
                if cookie:
                    _omlx_session_cookie = cookie
                    return cookie
            except Exception:
                pass
    return None

def set_omlx_settings(settings: dict) -> bool:
    """Update oMLX global settings via admin API."""
    _p = tui_print if _tui_instance else print
    cookie = _omlx_admin_login()
    if not cookie:
        _p("[Warning: Could not login to oMLX admin API]", C_YELLOW if _tui_instance else 0)
        return False
    payload = json.dumps(settings).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL.replace('/v1', '')}/admin/api/global-settings",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Cookie": f"omlx_admin_session={cookie}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        desc = ", ".join(f"{k}={v}" for k, v in settings.items())
        _p(f"[oMLX settings: {desc}]", C_DIM if _tui_instance else 0)
        return True
    except Exception as e:
        global _omlx_session_cookie
        _omlx_session_cookie = None
        _p(f"[Warning: Could not update oMLX settings: {e}]", C_YELLOW if _tui_instance else 0)
        return False


def set_omlx_concurrency(n: int) -> bool:
    """Convenience wrapper for concurrency-only updates."""
    return set_omlx_settings({"max_concurrent_requests": n})


def unload_omlx_model(model_name: str) -> bool:
    """Force unload a model from oMLX to free memory before loading a different one."""
    _p = tui_print if _tui_instance else print
    cookie = _omlx_admin_login()
    if not cookie:
        _p(f"[Warning: Could not login to admin API for model unload]", C_YELLOW if _tui_instance else 0)
        return False
    try:
        # oMLX admin API: POST /admin/api/models/{model_id}/unload (model_id in path, not body)
        encoded_model = urllib.parse.quote(model_name, safe="")
        req = urllib.request.Request(
            f"{API_URL.replace('/v1', '')}/admin/api/models/{encoded_model}/unload",
            data=b"",
            headers={
                "Content-Type": "application/json",
                "Cookie": f"omlx_admin_session={cookie}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        _p(f"[Unloaded model: {model_name}]", C_DIM if _tui_instance else 0)
        return True
    except urllib.error.HTTPError as e:
        _p(f"[Model unload HTTP {e.code} for {model_name}]", C_YELLOW if _tui_instance else 0)
        return False
    except Exception as e:
        _p(f"[Model unload error: {e}]", C_YELLOW if _tui_instance else 0)
        return False


# Map each CE mode to its model group
CE_MODE_TO_GROUP = {
    "flow": "manager",
    "ideate": "ideate_brainstorm",
    "brainstorm": "ideate_brainstorm",
    "plan": "plan",
    "work": "work",
    "debug": "work",
    "review": "review",
    "code-review": "review",
    "doc-review": "review",
    "compound": "compound",
    "compound-refresh": "compound",
    "commit": "work",
    "pr-description": "work",
    "commit-push-pr": "work",
    "setup": "work",
    "test-browser": "work",
    "worktree": "work",
    "clean-gone-branches": "work",
    "optimize": "work",
    "sessions": "work",
    "demo-reel": "work",
    "report-bug": "work",
    "resolve-pr-feedback": "review",
    "polish-beta": "review",
    "lfg": "manager",
}

# Ordered list of model groups for startup selection
MODEL_GROUPS = [
    ("manager", "Manager", "/ce:flow orchestration"),
    ("ideate_brainstorm", "Ideate/Brainstorm", "/ce:ideate, /ce:brainstorm"),
    ("plan", "Planning", "/ce:plan"),
    ("work", "Work/Debug/Chat", "/ce:work, /ce:debug, general chat"),
    ("review", "Review", "/ce:review"),
    ("compound", "Compound", "/ce:compound"),
]


# ── Agent Loop ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a coding agent operating in a local repository. You follow the Compound Engineering methodology: plan thoroughly, execute precisely, review carefully, and document learnings.

You have tools to:
- Read, write, search, and modify files
- Run shell commands
- Interact with git/GitHub
- Manage compound engineering workflows (learnings, todos, plans)
- Save CE documents (brainstorms, reviews, solutions, todos) to docs/ or referenceDocs/
- Track progress in plan/review files with ce_mark_step
- List all CE docs with ce_list_docs

CE documents are stored in docs/ or referenceDocs/ subdirectories (brainstorms/, plans/, reviews/, solutions/, todos/). Always check for existing docs before starting a new workflow. When resuming work, read the plan and check which steps are already marked [x].

Guidelines:
- Use tools to gather context before making changes
- NEVER guess or fabricate file paths. When you need a file whose path you do not already know, use list_directory first to discover what exists. Only then use read_file or search_files on confirmed paths.
- Use replace_in_file for surgical edits; write_file for new files
- After making changes, verify with read_file or run_command
- Use git_commit_and_push when asked to commit/push/sync
- Check ce_read_learnings at the start of non-trivial tasks
- Be concise. Act first, explain briefly.
- NEVER stop mid-work. Finish what you start.

Todo List Management:
- For ANY multi-step task, create a todo list using ce_manage_todos BEFORE starting work.
- Mark each todo complete as you finish it. NEVER remove completed items -- they serve as a progress record.
- Only use the "clear" action when starting an entirely new task that needs a fresh list.
- Check the todo list frequently to stay on track and show progress.

Working directory: {work_dir}"""


def parse_text_tool_calls(text: str) -> list:
    """Parse <tool_call> or <tool_use> tags from model text output."""
    calls = []
    # Match both <tool_call>...</tool_call> and <tool_use>...</tool_use> blocks
    pattern = r'<tool_(?:call|use)>\s*(\{.*?\})\s*</tool_(?:call|use)>'
    # Also match unclosed tags (model sometimes omits closing tag)
    pattern_unclosed = r'<tool_(?:call|use)>\s*(\{.*?)(?:</tool_(?:call|use)>|$)'
    
    matches = list(re.finditer(pattern, text, re.DOTALL))
    if not matches:
        matches = list(re.finditer(pattern_unclosed, text, re.DOTALL))
    
    for match in matches:
        raw = match.group(1).strip()
        # Try to fix truncated JSON by ensuring it ends with }
        if not raw.endswith('}'):
            raw = raw.rstrip() + '}'
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: extract name with regex
            name_match = re.search(r'["\']?name["\']?\s*:\s*["\']?([\w]+)["\']?', raw)
            if not name_match:
                continue
            parsed = {"name": name_match.group(1)}
            # Try to get arguments
            args_match = re.search(r'["\']?arguments["\']?\s*:\s*(\{.*\})', raw, re.DOTALL)
            if args_match:
                try:
                    parsed["arguments"] = json.loads(args_match.group(1))
                except json.JSONDecodeError:
                    kv_pattern = r'["\']([\w]+)["\']\s*:\s*["\']([^"\']*)["\']'
                    parsed["arguments"] = {kv.group(1): kv.group(2) for kv in re.finditer(kv_pattern, args_match.group(1))}
            else:
                # Try extracting flat key-value pairs as arguments
                kv_pattern = r'["\']([\w]+)["\']\s*:\s*["\']([^"\']*)["\']'
                flat = {kv.group(1): kv.group(2) for kv in re.finditer(kv_pattern, raw)}
                flat.pop("name", None)
                parsed["arguments"] = flat
        
        name = parsed.get("name", "").strip()
        if not name:
            continue
        
        # Handle both nested {"arguments": {...}} and flat {"name": ..., "path": ...} formats
        if "arguments" in parsed:
            arguments = parsed["arguments"]
        else:
            # Flat format: everything except "name" is an argument
            arguments = {k: v for k, v in parsed.items() if k != "name"}
        
        calls.append({"name": name, "arguments": arguments})
    return calls


def _content_to_text(content) -> str:
    """Normalize model content fields that may be str, dict, or list blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _content_to_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return str(content)


def _extract_output_text(output) -> str:
    """Extract text from Responses-style output arrays."""
    if not isinstance(output, list):
        return ""
    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("message", "output_message"):
            parts.append(_content_to_text(item.get("content")))
        elif item_type in ("text", "output_text"):
            parts.append(_content_to_text(item.get("text")))
    return "".join(p for p in parts if p)


def _normalize_tool_calls(msg: dict) -> list:
    """Normalize structured tool calls from several common response formats."""
    normalized = []
    if not isinstance(msg, dict):
        return normalized

    raw_calls = msg.get("tool_calls")
    if isinstance(raw_calls, list):
        for i, tc in enumerate(raw_calls, 1):
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id") or f"call_{i}"
            # Try nested .function first, then top-level keys as fallback
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
            fn_name = (fn.get("name") if fn else None) or tc.get("name")
            if not fn_name:
                continue
            fn_args = (fn.get("arguments") if fn else None) or tc.get("arguments") or tc.get("parameters") or {}
            normalized.append({
                "id": tc_id,
                "function": {
                    "name": fn_name,
                    "arguments": fn_args,
                },
            })

    function_call = msg.get("function_call")
    if isinstance(function_call, dict):
        fn_name = function_call.get("name")
        if fn_name:
            normalized.append({
                "id": f"call_fc_{len(normalized) + 1}",
                "function": {
                    "name": fn_name,
                    "arguments": function_call.get("arguments", {}),
                },
            })

    return normalized


def normalize_api_response(response: dict) -> dict:
    """Return a tolerant normalized view of an API response."""
    usage = response.get("usage") if isinstance(response, dict) else {}
    if not isinstance(usage, dict):
        usage = {}

    normalized = {
        "text": "",
        "tool_calls": [],
        "reasoning_content": "",
        "finish_reason": None,
        "usage": usage,
        "error": "",
        "response_shape": list(response.keys())[:12] if isinstance(response, dict) else [],
    }

    if not isinstance(response, dict):
        normalized["error"] = f"response is {type(response).__name__}, expected dict"
        return normalized

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

        normalized["text"] = _content_to_text(msg.get("content"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(choice.get("text"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(choice.get("content"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(msg.get("text"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(delta.get("content"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(delta.get("text"))
        if not normalized["text"]:
            normalized["text"] = _content_to_text(msg.get("refusal"))

        normalized["reasoning_content"] = _content_to_text(msg.get("reasoning_content"))
        if not normalized["reasoning_content"]:
            normalized["reasoning_content"] = _content_to_text(choice.get("reasoning_content"))
        if not normalized["reasoning_content"]:
            normalized["reasoning_content"] = _content_to_text(msg.get("reasoning"))
        if not normalized["reasoning_content"]:
            normalized["reasoning_content"] = _content_to_text(delta.get("reasoning_content"))

        normalized["tool_calls"] = _normalize_tool_calls(msg)
        if not normalized["tool_calls"]:
            normalized["tool_calls"] = _normalize_tool_calls(choice)
        if not normalized["tool_calls"]:
            normalized["tool_calls"] = _normalize_tool_calls(delta)
        normalized["finish_reason"] = choice.get("finish_reason")
        if not normalized["finish_reason"]:
            normalized["finish_reason"] = response.get("finish_reason")
    else:
        msg = response.get("message") if isinstance(response.get("message"), dict) else {}
        normalized["text"] = (
            _content_to_text(response.get("response"))
            or _content_to_text(response.get("content"))
            or _content_to_text(response.get("text"))
            or _content_to_text(response.get("output_text"))
            or _extract_output_text(response.get("output"))
            or _content_to_text(msg.get("content"))
        )
        normalized["reasoning_content"] = (
            _content_to_text(response.get("reasoning_content"))
            or _content_to_text(msg.get("reasoning_content"))
        )
        normalized["tool_calls"] = _normalize_tool_calls(msg)
        normalized["finish_reason"] = response.get("finish_reason")

    error_obj = response.get("error")
    if isinstance(error_obj, dict):
        normalized["error"] = error_obj.get("message") or json.dumps(error_obj)
    elif isinstance(error_obj, str):
        normalized["error"] = error_obj
    elif response.get("detail"):
        normalized["error"] = str(response.get("detail"))

    return normalized


def _is_retryable_empty_response(parsed: dict, messages: list | None = None) -> bool:
    """True when response is structurally valid but carries no actionable payload yet."""
    if parsed.get("tool_calls"):
        return False
    if parsed.get("text"):
        return False
    finish_reason = parsed.get("finish_reason")
    # None/empty: partial or ambiguous provider payloads.
    # "tool_calls": model signalled a tool call but parsing failed to extract it -- retry rather than abort.
    if finish_reason in (None, "", "tool_calls"):
        return True
    if finish_reason == "stop":
        stop_nudge = "[System: Your previous response ended with finish_reason=stop but contained no visible content or tool call. Resend your final answer or tool call, and ensure either content text or tool_calls is present.]"
        if not isinstance(messages, list) or not messages:
            return True
        last_message = messages[-1] if isinstance(messages[-1], dict) else {}
        return last_message.get("content") != stop_nudge
    return False


def _write_malformed_response_debug(response: dict) -> str:
    """Persist the last malformed payload at a deterministic path for inspection."""
    os.makedirs(os.path.dirname(MALFORMED_DEBUG_FILE), exist_ok=True)
    with open(MALFORMED_DEBUG_FILE, "w") as debug_file:
        json.dump(response, debug_file, indent=2, default=str)
    return MALFORMED_DEBUG_FILE


def execute_tool(name: str, arguments) -> str:
    if name not in TOOL_DISPATCH:
        return f"ERROR: Unknown tool '{name}'"
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments)
        elif isinstance(arguments, dict):
            args = arguments
        else:
            args = {}
    except json.JSONDecodeError as e:
        return f"ERROR: Invalid JSON arguments: {e}"
    _print = tui_print if _tui_instance else print
    # Update activity in TUI status bar
    if _tui_instance:
        _tui_instance._activity = f"tool: {name}"
        _tui_instance._last_tool = name
        _tui_instance.needs_redraw = True
    _ts = datetime.now().strftime("%H:%M:%S")
    _print(f"  [{_ts}] > {name}({json.dumps(args, indent=None)[:120]})", C_CYAN)
    try:
        result = TOOL_DISPATCH[name](args)
    except KeyError as e:
        result = f"ERROR: Missing required argument {e} for tool '{name}'. This likely means your output was truncated mid-tool-call. IMPORTANT: Do NOT retry the same call. Instead, break large content into smaller pieces. For write_file with large content: 1) write_file with a short header first, 2) then use replace_in_file to append sections one at a time."
    except Exception as e:
        result = f"ERROR: Tool '{name}' failed: {type(e).__name__}: {e}"
    preview = result[:200].replace("\n", " ")
    if len(result) > 200:
        preview += "..."
    _print(f"    = {preview}", C_DIM)
    return result


def emergency_trim(messages: list) -> list:
    """Drop the oldest non-system messages when oMLX reports context overflow.

    Only called when the API returns HTTP 400 'Prompt too long'.
    Drops EMERGENCY_TRIM_DROP oldest non-system messages to free token space.
    """
    _p = tui_print if _tui_instance else print

    # Find where non-system messages start
    body_start = 0
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            body_start = i
            break

    system_msgs = messages[:body_start]
    body = messages[body_start:]

    drop = min(EMERGENCY_TRIM_DROP, max(0, len(body) - 4))
    if drop <= 0:
        _p("[Cannot trim further -- only system messages remain]", C_RED)
        return messages

    trimmed = system_msgs + body[drop:]
    _p(f"[Context overflow -- dropped {drop} oldest messages: {len(messages)} -> {len(trimmed)}]", C_YELLOW)
    return trimmed


def agent_turn(messages: list, model: str) -> str:
    for round_num in range(MAX_TOOL_ROUNDS):
        response = api_call(messages, model)
        if response and response.get("_context_overflow"):
            messages[:] = emergency_trim(messages)
            continue
        if not response:
            return "[API call failed]"

        parsed = normalize_api_response(response)
        text = parsed["text"]
        tool_calls = parsed["tool_calls"]
        finish_reason = parsed["finish_reason"]

        if parsed["reasoning_content"]:
            rc = parsed["reasoning_content"]
            display = f"{rc[:300]}..." if len(rc) > 300 else rc
            _p = tui_print if _tui_instance else print
            _ts = datetime.now().strftime("%H:%M:%S")
            _p(f"[{_ts}] [thinking] {display}", C_DIM)

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls,
            })
            if text:
                _ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n\033[33m[{_ts}] {text}\033[0m")
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                if not name:
                    continue
                result = execute_tool(name, fn.get("arguments", {}))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "call_unknown"),
                    "content": result,
                })
            continue

        # Fallback: parse <tool_call> tags from text content
        text_calls = parse_text_tool_calls(text)
        if text_calls:
            # Strip tool_call/tool_use tags from displayed text
            display_text = re.sub(r'<tool_(?:call|use)>.*?</tool_(?:call|use)>', '', text, flags=re.DOTALL)
            display_text = re.sub(r'<tool_(?:call|use)>.*$', '', display_text, flags=re.DOTALL).strip()
            if display_text:
                _ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n\033[33m[{_ts}] {display_text}\033[0m")

            # Execute the parsed tool calls
            tool_results = []
            for tc in text_calls:
                result = execute_tool(tc["name"], tc["arguments"])
                tool_results.append(f"[{tc['name']}] {result}")

            # Add to conversation as assistant + tool results
            messages.append({"role": "assistant", "content": text})
            combined_results = "\n\n".join(tool_results)
            messages.append({"role": "user", "content": f"[Tool results]:\n{combined_results}\n\nContinue with the task."})
            continue

        # Auto-continue if generation was truncated (hit max_tokens)
        if finish_reason == "length":
            _p = tui_print if _tui_instance else print
            _ts = datetime.now().strftime("%H:%M:%S")
            _p(f"[{_ts}] [Generation hit token limit -- auto-continuing]", C_YELLOW if _tui_instance else 0)
            if text:  # don't append empty assistant turns; null content breaks some models
                messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "[System: Your response was truncated because it hit the generation token limit. Continue EXACTLY where you left off. Do NOT repeat what you already said. If you were about to make a tool call, make it now.]"})
            continue

        if not text:
            if _is_retryable_empty_response(parsed, messages) and round_num < MAX_TOOL_ROUNDS - 1:
                if finish_reason == "tool_calls":
                    nudge = "[System: Your last response indicated a tool call (finish_reason=tool_calls) but no tool call was parseable. Please resend your tool call in the standard format.]"
                elif finish_reason == "stop":
                    nudge = "[System: Your previous response ended with finish_reason=stop but contained no visible content or tool call. Resend your final answer or tool call, and ensure either content text or tool_calls is present.]"
                else:
                    nudge = "[System: Your previous response had no visible content or tool call. Reply with either plain text content or a structured tool call now.]"
                messages.append({"role": "user", "content": nudge})
                continue
            # Log raw response for post-mortem debugging
            debug_path = None
            try:
                debug_path = _write_malformed_response_debug(response)
            except Exception:
                pass
            detail = parsed["error"] or f"keys={','.join(parsed['response_shape'])}"
            if debug_path:
                return f"[Malformed API response: {detail}; debug={debug_path}]"
            return f"[Malformed API response: {detail}]"

        # Narration guard: if model is describing what to do instead of doing it, nudge it
        _narration_keywords = [
            "let me", "i need to", "i'll ", "i will", "i should",
            "next step", "now i", "let's ",
            "add a ", "add the ", "you need to", "you should", "you can ",
            "here is the", "here's the", "the change", "the fix",
            "insert the", "insert this", "add this", "place this",
            "to implement", "to add", "would need to", "should be added",
            "implementation", "result:", "**result**",
        ]
        _active_ce = getattr(_tui_instance, 'active_ce_mode', None) if _tui_instance else False
        if (_active_ce and round_num < MAX_TOOL_ROUNDS - 1
                and text
                and any(kw in text.lower() for kw in _narration_keywords)):
            _p = tui_print if _tui_instance else print
            _ts = datetime.now().strftime("%H:%M:%S")
            _p(f"[{_ts}] [auto-nudge: model narrated instead of acting]", C_DIM if _tui_instance else 0)
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "[System: Do not narrate what to do or show code for the user to apply manually. Use your tools (replace_in_file, write_file) to make the change RIGHT NOW. Make the tool call.]"
            })
            continue

        messages.append({"role": "assistant", "content": text})
        return text

    return "[Max tool rounds reached]"


# ── CE Sub-agent Dispatch ────────────────────────────────────────────────────

# Cap sub-agent transcripts to prevent runaway loops in nested dispatches.
_CE_SUBAGENT_MAX_DEPTH = 3
_ce_subagent_depth = 0


def run_ce_subagent(agent_name: str, task: str, model: str | None = None) -> str:
    """Run a vendored CE sub-agent in an isolated conversation.

    Loads the agent's body from compound-engineering/agents/<agent_name>.agent.md,
    appends the omlx_agent host adaptation footer, builds a fresh messages
    list, and runs it through agent_turn(). Returns the agent's final
    text/JSON output.

    The sub-agent gets the omlx_agent tool surface (read_file, write_file,
    run_command, etc.) plus ce_run_agent itself for nested dispatch up to
    _CE_SUBAGENT_MAX_DEPTH levels.
    """
    global _ce_subagent_depth
    if _ce_subagent_depth >= _CE_SUBAGENT_MAX_DEPTH:
        return f"[ce_run_agent refused: max sub-agent depth ({_CE_SUBAGENT_MAX_DEPTH}) reached]"

    agent = load_ce_agent(agent_name)
    if agent is None:
        available = ", ".join(list_ce_agents()[:20])
        return f"[ce_run_agent error: agent '{agent_name}' not found in {CE_VENDOR_DIR}/agents/. Available (first 20): {available}]"

    # Resolve model: caller-specified > inherit from active model > fallback.
    if model is None:
        if _tui_instance is not None:
            try:
                model = _tui_instance.active_model
            except Exception:
                model = None
    if not model:
        return "[ce_run_agent error: no model available; pass model= or run inside an active TUI session]"

    system_prompt = agent["body"] + CE_HOST_ADAPTATION
    sub_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    _p = tui_print if _tui_instance else print
    _ts = datetime.now().strftime("%H:%M:%S")
    _p(f"[{_ts}] [ce_run_agent -> {agent_name} (depth {_ce_subagent_depth + 1})]",
       C_MAGENTA if _tui_instance else 0)

    _ce_subagent_depth += 1
    try:
        result = agent_turn(sub_messages, model)
    finally:
        _ce_subagent_depth -= 1

    return result


def tool_ce_run_agent(agent_name: str, task: str) -> str:
    """TOOL_DISPATCH entry for ce_run_agent."""
    if not isinstance(agent_name, str) or not agent_name:
        return "ERROR: agent_name is required"
    if not isinstance(task, str) or not task.strip():
        return "ERROR: task is required (pass full context for the sub-agent)"
    return run_ce_subagent(agent_name, task)


def tool_ce_list_agents() -> str:
    """TOOL_DISPATCH entry for ce_list_agents."""
    names = list_ce_agents()
    if not names:
        return f"No CE agents found in {CE_VENDOR_DIR}/agents/"
    lines = [f"Available CE sub-agents ({len(names)}):"]
    for name in names:
        agent = load_ce_agent(name)
        desc = (agent.get("description") or "").strip().replace("\n", " ") if agent else ""
        if len(desc) > 140:
            desc = desc[:137] + "..."
        lines.append(f"  {name}: {desc}" if desc else f"  {name}")
    return "\n".join(lines)


def print_help():
    print("""
\033[1mCommands:\033[0m
  /model              Switch model
  /clear              Clear conversation
  /status             Show git status and session info
  /quit               Exit

\033[1mCompound Engineering:\033[0m
  /ce:brainstorm        Explore ideas and requirements
  /ce:plan              Create a structured implementation plan
  /ce:work              Execute work with task tracking
  /ce:review            Tiered persona code review (vendored ce-code-review)
  /ce:doc-review        Persona-based requirements/plan document review
  /ce:compound          Document learnings from recent work
  /ce:compound-refresh  Age out, replace, or archive stale learnings
  /ce:debug             Systematic bug investigation
  /ce:ideate            Discover project improvements
  /ce:commit            Value-first git commit
  /ce:pr-description    Generate a PR title and body
  /ce:commit-push-pr    Commit, push, and open a PR
  /ce:setup             Diagnose env and bootstrap project config
  /ce:test-browser      Run browser tests on PR-affected pages
  /ce:flow              Autonomous brainstorm -> plan -> work -> review -> compound
  /ce:done              Exit current CE mode
  /ce:learnings         View all project learnings
  /ce:todos             View current todo list
  /help               Show this help
""")


# ── TUI ──────────────────────────────────────────────────────────────────────

# Global TUI reference so print redirection works
_tui_instance = None


def tui_print(text: str, color: int = 0):
    """Write a line of text to the TUI output pane (or stdout if no TUI)."""
    if _tui_instance:
        _tui_instance.add_output(text, color)
    else:
        print(text)


# Color constants for the TUI
C_DEFAULT = 0
C_GREEN = 1
C_YELLOW = 2
C_CYAN = 3
C_STATUS_BAR = 4
C_RED = 5
C_MAGENTA = 6
C_DIM = 7
C_BOLD_GREEN = 8


class AgentTUI:
    """Curses-based TUI with separate output/input panes and async agent."""

    def __init__(self, model_config: dict, messages: list, active_ce_mode: str = None, models: list = None):
        # model_config: dict of group_name -> {"model": str, "concurrency": int, "memory": str}
        self.model_groups = model_config
        self._current_group = None   # which model group's settings are active on oMLX
        self._current_model_name = None  # which model is currently loaded
        self.messages = messages
        self.active_ce_mode = active_ce_mode
        self.models = models or []
        self.workflow_state = CEWorkflowState()
        self._ce_setup_warned = False  # session flag: have we surfaced the /ce:setup nag yet

        # Output
        self.output_lines = []  # list of (str, color_pair_num)
        self.output_lock = threading.Lock()
        self.scroll_offset = 0

        # Input (multiline)
        self.input_lines = [[]]      # list of lines, each line is list of chars
        self.input_row = 0           # cursor row in input_lines
        self.input_col = 0           # cursor col in current line
        self.input_history = self._load_input_history()  # persistent history
        self.history_index = -1      # -1 = not browsing history
        self.saved_input = ""        # saved partial input when browsing history
        self._at_top_edge = False    # True when Up was pressed at row 0
        self._at_bottom_edge = False # True when Down was pressed at last row
        self._last_escape_time = 0.0 # for detecting Alt+Enter (Esc then Enter)

        # Agent state
        self.agent_running = False
        self.agent_thread = None
        self.interrupt_event = threading.Event()
        self.steer_queue = queue.Queue()
        self.pending_queue = queue.Queue()
        self._activity = ""       # current activity shown in status bar
        self._last_tool = ""      # last tool executed

        # Misc
        self.quit_flag = False
        self.needs_redraw = True
        self.stdscr = None

        # Action menu (popup when agent is running)
        self.menu_open = False
        self.menu_index = 0
        self.menu_items = [
            ("Stop & Send",  "Interrupt agent, send your message now",    "stop_send"),
            ("Queue",        "Send after current task finishes",         "queue"),
            ("Steer",        "Inject message without stopping agent",   "steer"),
            ("Cancel",       "Cancel current agent operation",           "cancel"),
        ]

        # Todo popup state
        self.todo_popup_anim_state = "hidden"  # hidden, expanding, shown, collapsing
        self.todo_popup_anim_height = 0
        self.todo_popup_target_height = 0
        self.todo_popup_scroll = 0
        self.todo_popup_lines = []      # cached wrapped lines for display
        self.todo_popup_width = 0
        self.todo_popup_title = ""
        self._todo_popup_scrollable = False

        # Image paste and popup state
        self.pasted_images = []  # list of dicts: {filename, path}
        self.image_popup_anim_state = "hidden"  # hidden, expanding, shown, collapsing
        self.image_popup_anim_height = 0
        self.image_popup_target_height = 0
        self.image_popup_scroll = 0
        self.image_popup_lines = []
        self.image_popup_width = 0
        self.image_popup_title = ""
        self._image_popup_scrollable = False
        self._image_view_index = None  # index of selected image in popup
        self._last_clipboard_image_hash = None

        # Memory monitor state
        self._mem_lock = threading.Lock()
        self._mem_sys_total = 0       # total system RAM bytes
        self._mem_sys_used = 0        # used system RAM bytes
        self._mem_sys_free = 0        # free + inactive system RAM bytes
        self._mem_pressure = ""       # macOS memory pressure level
        self._mem_model_used = 0      # oMLX model memory used bytes
        self._mem_model_max = 0       # oMLX model memory max bytes
        self._mem_ctx_msgs = 0        # current conversation message count
        self._mem_active_reqs = 0     # active inference requests
        self._mem_cache_eff = 0.0     # KV cache efficiency %
        self._mem_total_tokens = 0    # total tokens this session
        self._mem_hot_cache_used = 0  # hot cache used bytes
        self._mem_hot_cache_max = 0   # hot cache max bytes
        self._mem_hot_cache_entries = 0  # hot cache entry count
        self._mem_poll_thread = None
        self._mem_poll_stop = threading.Event()

    def _start_memory_monitor(self):
        """Start background thread that polls system and oMLX memory stats."""
        def poll_loop():
            while not self._mem_poll_stop.is_set():
                try:
                    self._poll_memory()
                except Exception:
                    pass
                self._mem_poll_stop.wait(3)  # poll every 3 seconds
        self._mem_poll_thread = threading.Thread(target=poll_loop, daemon=True)
        self._mem_poll_thread.start()

    def _stop_memory_monitor(self):
        """Stop the memory polling thread."""
        self._mem_poll_stop.set()
        if self._mem_poll_thread:
            self._mem_poll_thread.join(timeout=2)

    def _poll_memory(self):
        """Poll system memory and oMLX server stats."""
        # System memory via vm_stat (available on all macOS)
        try:
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=3
            )
            page_size = 16384  # default Apple Silicon
            ps_match = re.search(r'page size of (\d+) bytes', result.stdout)
            if ps_match:
                page_size = int(ps_match.group(1))

            stats = {}
            for line in result.stdout.strip().split('\n'):
                m = re.match(r'^(.+?):\s+(\d+)', line)
                if m:
                    stats[m.group(1).strip()] = int(m.group(2))

            free_pages = stats.get('Pages free', 0)
            active_pages = stats.get('Pages active', 0)
            inactive_pages = stats.get('Pages inactive', 0)
            wired_pages = stats.get('Pages wired down', 0)
            speculative_pages = stats.get('Pages speculative', 0)
            # Compressor pages count as used
            compressor_pages = stats.get('Pages occupied by compressor', 0)
            purgeable_pages = stats.get('Pages purgeable', 0)

            total_used_pages = active_pages + wired_pages + compressor_pages
            total_free_pages = free_pages + inactive_pages + speculative_pages + purgeable_pages

            # Get total from sysctl
            try:
                sysctl_result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=3
                )
                total_bytes = int(sysctl_result.stdout.strip())
            except Exception:
                total_bytes = (total_used_pages + total_free_pages) * page_size

            with self._mem_lock:
                self._mem_sys_total = total_bytes
                self._mem_sys_used = total_used_pages * page_size
                self._mem_sys_free = total_free_pages * page_size
        except Exception:
            pass

        # macOS memory pressure
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True, text=True, timeout=3
            )
            level = int(result.stdout.strip())
            pressure_map = {1: "normal", 2: "warn", 4: "critical"}
            with self._mem_lock:
                self._mem_pressure = pressure_map.get(level, f"L{level}")
        except Exception:
            with self._mem_lock:
                self._mem_pressure = "?"

        # oMLX server stats
        try:
            req = urllib.request.Request(
                f"{API_URL.replace('/v1', '')}/api/status",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            with self._mem_lock:
                self._mem_model_used = data.get("model_memory_used", 0) or 0
                self._mem_model_max = data.get("model_memory_max", 0) or 0
                self._mem_active_reqs = data.get("active_requests", 0)
                self._mem_cache_eff = data.get("cache_efficiency", 0.0) or 0.0
                self._mem_total_tokens = (
                    (data.get("total_prompt_tokens", 0) or 0)
                    + (data.get("total_completion_tokens", 0) or 0)
                )
        except Exception:
            pass

        # Hot cache stats via admin API
        try:
            cookie = _omlx_admin_login()
            if cookie:
                req = urllib.request.Request(
                    f"{API_URL.replace('/v1', '')}/admin/api/stats",
                    headers={"Cookie": f"omlx_admin_session={cookie}"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    stats_data = json.loads(resp.read())
                rc = stats_data.get("runtime_cache", {})
                models = rc.get("models", [])
                # Aggregate hot cache across all loaded models
                hc_used = 0
                hc_max = 0
                hc_entries = 0
                for m in models:
                    hc_used += int(m.get("hot_cache_size_bytes", 0) or 0)
                    hc_max += int(m.get("hot_cache_max_bytes", 0) or 0)
                    hc_entries += int(m.get("hot_cache_entries", 0) or 0)
                with self._mem_lock:
                    self._mem_hot_cache_used = hc_used
                    self._mem_hot_cache_max = hc_max
                    self._mem_hot_cache_entries = hc_entries
        except Exception:
            pass

        # Context message count
        with self._mem_lock:
            self._mem_ctx_msgs = len(self.messages)

        self.needs_redraw = True

    def _format_bytes(self, b: int) -> str:
        """Format bytes to human-readable string."""
        if b <= 0:
            return "0B"
        if b < 1024 ** 3:
            return f"{b / (1024**2):.0f}MB"
        return f"{b / (1024**3):.1f}GB"

    @property
    def active_group(self) -> str:
        """Return the model group key for the current CE mode."""
        return CE_MODE_TO_GROUP.get(self.active_ce_mode, "work")

    @property
    def active_model(self) -> str:
        """Return the right model based on current CE mode."""
        return self.model_groups[self.active_group]["model"]

    def _ensure_model(self):
        """Ensure the correct model is loaded for the current CE mode.
        Unloads the previous model if switching to a different one."""
        group = self.active_group
        config = self.model_groups[group]
        new_model = config["model"]

        if group != self._current_group:
            # Unload old model if it's different from the new one
            if self._current_model_name and self._current_model_name != new_model:
                unload_omlx_model(self._current_model_name)

            # Apply new concurrency and memory settings
            settings = {"max_concurrent_requests": config.get("concurrency", 1)}
            mem = config.get("memory")
            if mem:
                settings["max_model_memory"] = mem
            set_omlx_settings(settings)

            self._current_group = group
            self._current_model_name = new_model

    # ── Output management ────────────────────────────────────────────────

    def add_output(self, text: str, color: int = 0):
        """Thread-safe: add text lines to the output buffer."""
        with self.output_lock:
            for line in text.split("\n"):
                self.output_lines.append((line, color))
        self.scroll_offset = 0
        self.needs_redraw = True

    def _wrapped_output(self, width: int) -> list:
        """Return output lines wrapped to terminal width."""
        wrapped = []
        with self.output_lock:
            for text, color in self.output_lines:
                # Strip ANSI escape codes for wrapping calculation
                clean = re.sub(r'\033\[[0-9;]*m', '', text)
                if len(clean) <= width - 1:
                    wrapped.append((text, color))
                else:
                    # Wrap long lines
                    chunks = textwrap.wrap(clean, width - 1, break_long_words=True, break_on_hyphens=False)
                    if not chunks:
                        wrapped.append(("", color))
                    else:
                        for chunk in chunks:
                            wrapped.append((chunk, color))
        return wrapped

    # ── Todo popup helpers ────────────────────────────────────────────────

    def _get_todo_raw_lines(self) -> list:
        """Get raw todo list lines."""
        if not SESSION_TODOS:
            return ["  N/A"]
        lines = []
        for i, t in enumerate(SESSION_TODOS):
            status = "[x]" if t["done"] else "[ ]"
            lines.append(f"  {i+1}. {status} {t['text']}")
        return lines

    def _open_todo_popup(self, max_w: int, max_h: int):
        """Prepare and start opening the todo popup."""
        raw = self._get_todo_raw_lines()
        count = len(SESSION_TODOS)
        self.todo_popup_title = f" Todo List ({count}) " if count else " Todo List "

        # Compute ideal inner width
        max_line_len = max((len(l) for l in raw), default=0)
        ideal_inner = max(max_line_len, len(self.todo_popup_title))
        inner_w = min(ideal_inner, max_w - 4)  # 4 = "| " + " |"
        inner_w = max(inner_w, 10)  # minimum
        self.todo_popup_width = inner_w + 4

        # Wrap lines to fit inner width
        self.todo_popup_lines = []
        for line in raw:
            if len(line) <= inner_w:
                self.todo_popup_lines.append(line)
            else:
                chunks = textwrap.wrap(line, inner_w, subsequent_indent="       ",
                                       break_long_words=True, break_on_hyphens=False)
                self.todo_popup_lines.extend(chunks if chunks else [line[:inner_w]])

        # Compute target height
        content_h = len(self.todo_popup_lines)
        ideal_h = content_h + 2  # +2 for borders
        self.todo_popup_target_height = min(ideal_h, max_h)
        self._todo_popup_scrollable = content_h > (self.todo_popup_target_height - 2)

        self.todo_popup_scroll = 0
        self.todo_popup_anim_state = "expanding"
        self.todo_popup_anim_height = 1
        self.needs_redraw = True

    def _refresh_todo_popup_content(self):
        """Refresh cached todo popup lines from SESSION_TODOS without restarting animation."""
        if self.todo_popup_anim_state not in ("shown", "expanding"):
            return
        raw = self._get_todo_raw_lines()
        count = len(SESSION_TODOS)
        self.todo_popup_title = f" Todo List ({count}) " if count else " Todo List "

        inner_w = self.todo_popup_width - 4
        if inner_w < 1:
            return

        self.todo_popup_lines = []
        for line in raw:
            if len(line) <= inner_w:
                self.todo_popup_lines.append(line)
            else:
                chunks = textwrap.wrap(line, inner_w, subsequent_indent="       ",
                                       break_long_words=True, break_on_hyphens=False)
                self.todo_popup_lines.extend(chunks if chunks else [line[:inner_w]])

        content_h = len(self.todo_popup_lines)
        self._todo_popup_scrollable = content_h > (self.todo_popup_target_height - 2)

    # ── Drawing ──────────────────────────────────────────────────────────

    def draw(self):
        if not self.needs_redraw and not self.agent_running:
            return
        self.needs_redraw = False

        h, w = self.stdscr.getmaxyx()
        if h < 6 or w < 20:
            return

        # Compute wrapped input display lines for soft-wrapping
        num_input_lines = len(self.input_lines)
        prefix_len = 4  # ">>> " or "... "
        max_text_w = max(1, w - prefix_len - 1)

        wrapped_input = []  # list of (line_idx, segment_text, col_offset)
        cursor_wrap_row = 0
        cursor_wrap_col = 0

        for line_idx, chars in enumerate(self.input_lines):
            line_str = "".join(chars)
            if not line_str:
                wrapped_input.append((line_idx, "", 0))
                if line_idx == self.input_row:
                    cursor_wrap_row = len(wrapped_input) - 1
                    cursor_wrap_col = 0
            else:
                wrap_line_start = len(wrapped_input)
                for seg_start in range(0, len(line_str), max_text_w):
                    seg = line_str[seg_start:seg_start + max_text_w]
                    wrapped_input.append((line_idx, seg, seg_start))
                if line_idx == self.input_row:
                    wrap_count = len(wrapped_input) - wrap_line_start
                    wrap_offset = min(self.input_col // max_text_w, wrap_count - 1)
                    cursor_wrap_row = wrap_line_start + wrap_offset
                    cursor_wrap_col = self.input_col % max_text_w
                    # Cursor at end of line exactly at wrap boundary
                    if (self.input_col == len(line_str) and len(line_str) > 0
                            and len(line_str) % max_text_w == 0):
                        wrapped_input.append((line_idx, "", len(line_str)))
                        cursor_wrap_row = len(wrapped_input) - 1
                        cursor_wrap_col = 0

        num_display_lines = len(wrapped_input)

        # Dynamic input height based on wrapped line count
        max_input_rows = min(8, max(1, h - 6))  # leave room for output+status+sep+prompt
        input_rows = min(num_display_lines, max_input_rows)

        # Layout from bottom up:
        #   h-1 .. h-input_rows : input lines
        #   h-input_rows-1      : prompt label
        #   h-input_rows-2      : status bar (commands/hints)
        #   h-input_rows-3      : memory bar (system + model memory)
        #   h-input_rows-4      : separator
        #   0 .. h-input_rows-5 : output
        input_start_row = h - input_rows
        prompt_row = input_start_row - 1
        status_row = prompt_row - 1
        memory_row = status_row - 1
        separator_row = memory_row - 1
        output_height = separator_row

        self.stdscr.erase()

        # ── Output pane ──────────────────────────────────────
        wrapped = self._wrapped_output(w)
        total = len(wrapped)
        visible_start = max(0, total - output_height - self.scroll_offset)
        visible_end = visible_start + output_height

        for i, idx in enumerate(range(visible_start, min(visible_end, total))):
            text, color = wrapped[idx]
            clean = re.sub(r'\033\[[0-9;]*m', '', text)
            try:
                self.stdscr.addnstr(i, 0, clean[:w-1], w - 1, curses.color_pair(color))
            except curses.error:
                pass

        # Scroll indicator
        if self.scroll_offset > 0 and total > output_height:
            indicator = f" [{self.scroll_offset} lines above] "
            try:
                self.stdscr.addnstr(0, max(0, w - len(indicator) - 1), indicator, w - 1,
                                    curses.color_pair(C_YELLOW))
            except curses.error:
                pass

        # ── Separator ────────────────────────────────────────
        try:
            sep = "-" * (w - 1)
            self.stdscr.addnstr(separator_row, 0, sep, w - 1, curses.color_pair(C_DIM))
        except curses.error:
            pass

        # ── Status bar ───────────────────────────────────────
        if self.agent_running:
            round_info = f"R:{getattr(self, 'current_round', 0)}"
            activity = getattr(self, '_activity', '') or 'waiting'
            status = f" {round_info} {activity} "
            hints = "[Tab: Actions | ^C Cancel | ^O Todos | ^P Images | ^X Import]"
            bar = f"{status}{hints}"
            color = C_YELLOW
        else:
            mode_tag = f" [{self.active_ce_mode}]" if self.active_ce_mode else ""
            status = f" Ready{mode_tag}  Model: {self.active_model[:40]} "
            nl_hint = "Ret:newline | ^S:submit"
            hints = f"[{nl_hint} | ^O Todos | ^P Images | ^X Import | ^L Clear | ^D Quit]"
            bar = f"{status}{hints}"
            color = C_GREEN

        try:
            padded = bar.ljust(w - 1)[:w - 1]
            self.stdscr.addnstr(status_row, 0, padded, w - 1, curses.color_pair(C_STATUS_BAR))
        except curses.error:
            pass

        # ── Memory bar (below separator, above status bar) ───
        with self._mem_lock:
            sys_total = self._mem_sys_total
            sys_used = self._mem_sys_used
            sys_free = self._mem_sys_free
            pressure = self._mem_pressure
            model_used = self._mem_model_used
            model_max = self._mem_model_max
            ctx_msgs = self._mem_ctx_msgs
            active_reqs = self._mem_active_reqs
            cache_eff = self._mem_cache_eff
            total_tokens = self._mem_total_tokens
            hc_used = self._mem_hot_cache_used
            hc_max = self._mem_hot_cache_max
            hc_entries = self._mem_hot_cache_entries

        if sys_total > 0:
            pct_used = (sys_used / sys_total) * 100
            sys_part = f"RAM: {self._format_bytes(sys_used)}/{self._format_bytes(sys_total)} ({pct_used:.0f}%)"
        else:
            sys_part = "RAM: --"
            pct_used = 0

        # Memory pressure indicator with color
        if pressure == "critical":
            mem_color = C_RED
            pressure_tag = " CRITICAL"
        elif pressure == "warn":
            mem_color = C_YELLOW
            pressure_tag = " WARN"
        elif pressure == "normal":
            mem_color = C_GREEN
            pressure_tag = ""
        else:
            mem_color = C_DIM
            pressure_tag = ""

        # Model memory
        if model_max > 0:
            model_pct = (model_used / model_max) * 100
            model_part = f"Model: {self._format_bytes(model_used)}/{self._format_bytes(model_max)} ({model_pct:.0f}%)"
        elif model_used > 0:
            model_part = f"Model: {self._format_bytes(model_used)}"
        else:
            model_part = ""

        # Hot cache
        if hc_max > 0:
            hc_pct = (hc_used / hc_max) * 100
            hot_part = f"Hot$: {self._format_bytes(hc_used)}/{self._format_bytes(hc_max)} ({hc_pct:.0f}%)"
        elif hc_used > 0:
            hot_part = f"Hot$: {self._format_bytes(hc_used)}"
        else:
            hot_part = ""

        # Context info
        ctx_part = f"Msgs: {ctx_msgs}"
        if total_tokens > 0:
            if total_tokens >= 1000000:
                tok_str = f"{total_tokens/1000000:.1f}M"
            elif total_tokens >= 1000:
                tok_str = f"{total_tokens/1000:.0f}K"
            else:
                tok_str = str(total_tokens)
            ctx_part += f"  Tok: {tok_str}"
        if cache_eff > 0:
            ctx_part += f"  Cache: {cache_eff:.0f}%"

        mem_bar_parts = [f" {sys_part}{pressure_tag}"]
        if model_part:
            mem_bar_parts.append(model_part)
        if hot_part:
            mem_bar_parts.append(hot_part)
        mem_bar_parts.append(ctx_part)
        mem_bar = "  |  ".join(mem_bar_parts) + " "

        # Color based on overall RAM pressure
        if pct_used >= 90 or pressure == "critical":
            bar_color = C_RED
        elif pct_used >= 75 or pressure == "warn":
            bar_color = C_YELLOW
        else:
            bar_color = C_DIM

        try:
            mem_padded = mem_bar.ljust(w - 1)[:w - 1]
            self.stdscr.addnstr(memory_row, 0, mem_padded, w - 1, curses.color_pair(bar_color))
        except curses.error:
            pass

        todo_popup_visible = self.todo_popup_anim_state != "hidden"
        image_popup_visible = self.image_popup_anim_state != "hidden"

        # ── Action menu popup (over output area, above status bar) ────
            # ── Image popup (drawn over output pane) ───────────────
        if self.image_popup_anim_state != "hidden":
            # Refresh content live
            anim_h = self.image_popup_anim_height
            pw = min(self.image_popup_width, w)
            inner_w = pw - 4
            popup_x = 0
            if todo_popup_visible:
                            popup_x = max(0, min(w - pw, min(w - 1, self.todo_popup_width + 1)))

            # Animate
            if self.image_popup_anim_state == "expanding":
                step = max(1, self.image_popup_target_height // 5)
                self.image_popup_anim_height = min(anim_h + step, self.image_popup_target_height)
                anim_h = self.image_popup_anim_height
                if anim_h >= self.image_popup_target_height:
                    self.image_popup_anim_state = "shown"
                self.needs_redraw = True
            elif self.image_popup_anim_state == "collapsing":
                step = max(1, self.image_popup_target_height // 5)
                self.image_popup_anim_height = max(0, anim_h - step)
                anim_h = self.image_popup_anim_height
                if anim_h <= 0:
                    self.image_popup_anim_state = "hidden"
                    self._image_view_index = None
                    self.needs_redraw = True
                else:
                    self.needs_redraw = True

            if anim_h > 0 and self.image_popup_anim_state != "hidden":
                popup_bottom = separator_row
                popup_top = max(0, popup_bottom - anim_h + 1)
                actual_h = popup_bottom - popup_top + 1
                content_rows = actual_h - 2

                # Top border with title
                title = self.image_popup_title
                if len(title) > pw - 2:
                    title = title[:pw - 2]
                pad_left = (pw - 2 - len(title)) // 2
                pad_right = pw - 2 - len(title) - pad_left
                top_border = "+" + "-" * pad_left + title + "-" * pad_right + "+"
                try:
                    self.stdscr.addnstr(popup_top, popup_x, top_border[:pw], pw, curses.color_pair(C_MAGENTA))
                except curses.error:
                    pass

                # Content rows
                if content_rows > 0:
                    total_lines = len(self.image_popup_lines)
                    max_scroll = max(0, total_lines - content_rows)
                    self.image_popup_scroll = max(0, min(self.image_popup_scroll, max_scroll))
                    can_scroll_up = self.image_popup_scroll > 0
                    can_scroll_down = (self.image_popup_scroll + content_rows) < total_lines

                    for ci in range(content_rows):
                        line_idx = self.image_popup_scroll + ci
                        screen_row = popup_top + 1 + ci
                        if screen_row >= popup_bottom:
                            break
                        if line_idx < total_lines:
                            text = self.image_popup_lines[line_idx]
                            color = C_BOLD_GREEN if self._image_view_index == line_idx else C_DEFAULT
                        else:
                            text = ""
                            color = C_DEFAULT
                        padded = text[:inner_w].ljust(inner_w)
                        row_str = "| " + padded + " |"
                        try:
                            self.stdscr.addnstr(screen_row, popup_x, row_str[:pw], pw, curses.color_pair(color))
                            self.stdscr.addnstr(screen_row, popup_x, "|", 1, curses.color_pair(C_MAGENTA))
                            self.stdscr.addnstr(screen_row, popup_x + pw - 1, "|", 1, curses.color_pair(C_MAGENTA))
                        except curses.error:
                            pass

                # Bottom border
                arrow_str = ""
                if self._image_popup_scrollable and content_rows > 0:
                    total_lines = len(self.image_popup_lines)
                    can_up = self.image_popup_scroll > 0
                    can_dn = (self.image_popup_scroll + content_rows) < total_lines
                    if can_up and can_dn:
                        arrow_str = "[^v]"
                    elif can_up:
                        arrow_str = "[^ ]"
                    elif can_dn:
                        arrow_str = "[ v]"
                bot_fill = pw - 2 - len(arrow_str)
                bot_border = "+" + "-" * bot_fill + arrow_str + "+"
                try:
                    self.stdscr.addnstr(popup_bottom, popup_x, bot_border[:pw], pw, curses.color_pair(C_MAGENTA))
                except curses.error:
                    pass
        if self.menu_open and self.agent_running:
            menu_w = 50
            menu_h = len(self.menu_items) + 2
            menu_x = max(0, (w - menu_w) // 2)
            menu_y = max(0, separator_row - menu_h)

            title = " Send Action "
            top_border = "+" + title.center(menu_w - 2, "-") + "+"
            bot_border = "+" + "-" * (menu_w - 2) + "+"
            try:
                self.stdscr.addnstr(menu_y, menu_x, top_border, menu_w, curses.color_pair(C_CYAN))
            except curses.error:
                pass

            for i, (label, desc, _) in enumerate(self.menu_items):
                row = menu_y + 1 + i
                if i == self.menu_index:
                    marker = ">"
                    attr = curses.color_pair(C_STATUS_BAR) | curses.A_BOLD
                else:
                    marker = " "
                    attr = curses.color_pair(C_DEFAULT)
                line = f"| {marker} {label:<16} {desc:<{menu_w - 23}}|"
                try:
                    self.stdscr.addnstr(row, menu_x, line[:menu_w], menu_w, attr)
                except curses.error:
                    pass

            try:
                self.stdscr.addnstr(menu_y + menu_h - 1, menu_x, bot_border, menu_w, curses.color_pair(C_CYAN))
            except curses.error:
                pass

        # ── Todo popup (drawn over output pane) ───────────────
        if self.todo_popup_anim_state != "hidden":
            # Refresh content live so completed items update without reopening
            self._refresh_todo_popup_content()
            anim_h = self.todo_popup_anim_height
            pw = min(self.todo_popup_width, w)
            inner_w = pw - 4  # "| " + " |"
            popup_x = 0

            # Animate
            if self.todo_popup_anim_state == "expanding":
                step = max(1, self.todo_popup_target_height // 5)
                self.todo_popup_anim_height = min(anim_h + step, self.todo_popup_target_height)
                anim_h = self.todo_popup_anim_height
                if anim_h >= self.todo_popup_target_height:
                    self.todo_popup_anim_state = "shown"
                self.needs_redraw = True
            elif self.todo_popup_anim_state == "collapsing":
                step = max(1, self.todo_popup_target_height // 5)
                self.todo_popup_anim_height = max(0, anim_h - step)
                anim_h = self.todo_popup_anim_height
                if anim_h <= 0:
                    self.todo_popup_anim_state = "hidden"
                    self.needs_redraw = True
                else:
                    self.needs_redraw = True

            if anim_h > 0 and self.todo_popup_anim_state != "hidden":
                popup_bottom = separator_row
                popup_top = max(0, popup_bottom - anim_h + 1)
                actual_h = popup_bottom - popup_top + 1
                content_rows = actual_h - 2  # minus top/bottom border

                # Top border with title
                title = self.todo_popup_title
                if len(title) > pw - 2:
                    title = title[:pw - 2]
                pad_left = (pw - 2 - len(title)) // 2
                pad_right = pw - 2 - len(title) - pad_left
                top_border = "+" + "-" * pad_left + title + "-" * pad_right + "+"
                try:
                    self.stdscr.addnstr(popup_top, popup_x, top_border[:pw], pw, curses.color_pair(C_CYAN))
                except curses.error:
                    pass

                # Content rows
                if content_rows > 0:
                    total_lines = len(self.todo_popup_lines)
                    # Clamp scroll
                    max_scroll = max(0, total_lines - content_rows)
                    self.todo_popup_scroll = max(0, min(self.todo_popup_scroll, max_scroll))
                    can_scroll_up = self.todo_popup_scroll > 0
                    can_scroll_down = (self.todo_popup_scroll + content_rows) < total_lines

                    # Determine arrow indicator space
                    arrow_str = ""
                    if can_scroll_up and can_scroll_down:
                        arrow_str = "^v"
                    elif can_scroll_up:
                        arrow_str = "^ "
                    elif can_scroll_down:
                        arrow_str = " v"

                    for ci in range(content_rows):
                        line_idx = self.todo_popup_scroll + ci
                        screen_row = popup_top + 1 + ci
                        if screen_row >= popup_bottom:
                            break
                        if line_idx < total_lines:
                            text = self.todo_popup_lines[line_idx]
                            # Check if this todo is done (for coloring)
                            is_done = "[x]" in text
                            color = C_DIM if is_done else C_DEFAULT
                        else:
                            text = ""
                            color = C_DEFAULT

                        # Pad or truncate to inner width
                        padded = text[:inner_w].ljust(inner_w)
                        row_str = "| " + padded + " |"
                        try:
                            self.stdscr.addnstr(screen_row, popup_x, row_str[:pw], pw, curses.color_pair(color))
                            # Draw border chars in cyan
                            self.stdscr.addnstr(screen_row, popup_x, "|", 1, curses.color_pair(C_CYAN))
                            self.stdscr.addnstr(screen_row, popup_x + pw - 1, "|", 1, curses.color_pair(C_CYAN))
                        except curses.error:
                            pass

                # Bottom border with optional scroll arrows
                arrow_str = ""
                if self._todo_popup_scrollable and content_rows > 0:
                    total_lines = len(self.todo_popup_lines)
                    can_up = self.todo_popup_scroll > 0
                    can_dn = (self.todo_popup_scroll + content_rows) < total_lines
                    if can_up and can_dn:
                        arrow_str = "[^v]"
                    elif can_up:
                        arrow_str = "[^ ]"
                    elif can_dn:
                        arrow_str = "[ v]"

                bot_fill = pw - 2 - len(arrow_str)
                bot_border = "+" + "-" * bot_fill + arrow_str + "+"
                try:
                    self.stdscr.addnstr(popup_bottom, popup_x, bot_border[:pw], pw, curses.color_pair(C_CYAN))
                except curses.error:
                    pass

        # ── Prompt label ─────────────────────────────────────
        if self.active_ce_mode:
            prompt_label = f"[{self.active_ce_mode}] You: "
        else:
            prompt_label = "You: "
        line_count_hint = f" ({num_input_lines}L)" if num_input_lines > 1 else ""
        try:
            self.stdscr.addnstr(prompt_row, 0, prompt_label + line_count_hint, w - 1, curses.color_pair(C_MAGENTA))
        except curses.error:
            pass

        # ── Input lines (soft-wrapped) ────────────────────────
        first_prefix = ">>> "
        cont_prefix = "... "
        wrap_prefix = "    "  # continuation of same logical line (wrapped)

        # Scroll so cursor's wrapped row is visible
        scroll_start = max(0, cursor_wrap_row - max_input_rows + 1)
        for i in range(input_rows):
            disp_idx = scroll_start + i
            screen_row = input_start_row + i
            if disp_idx >= num_display_lines:
                break
            line_idx, seg_text, col_off = wrapped_input[disp_idx]
            if line_idx == 0 and col_off == 0:
                pfx = first_prefix
            elif col_off == 0:
                pfx = cont_prefix
            else:
                pfx = wrap_prefix

            try:
                self.stdscr.addnstr(screen_row, 0, pfx, w - 1, curses.color_pair(C_CYAN))
                self.stdscr.addnstr(screen_row, len(pfx), seg_text[:max_text_w], max_text_w)
            except curses.error:
                pass

        # Position cursor
        cursor_screen_row = input_start_row + (cursor_wrap_row - scroll_start)
        cursor_x = prefix_len + cursor_wrap_col
        cursor_x = max(prefix_len, min(cursor_x, w - 2))
        cursor_screen_row = max(0, min(cursor_screen_row, h - 1))
        try:
            self.stdscr.move(cursor_screen_row, cursor_x)
        except curses.error:
            pass

        self.stdscr.refresh()

    # ── Input handling ───────────────────────────────────────────────────

    def get_input_text(self) -> str:
        return "\n".join("".join(line) for line in self.input_lines)

    def clear_input(self):
        self.input_lines = [[]]
        self.input_row = 0
        self.input_col = 0
        self.history_index = -1
        self._at_top_edge = False
        self._at_bottom_edge = False
        self.needs_redraw = True

    def submit_input(self, mode: str = "normal") -> str:
        """Submit current input. mode: normal, stop_send, queue, steer"""
        text = self.get_input_text().strip()
        if not text:
            return ""
        # Avoid duplicate of the most recent entry
        if not self.input_history or self.input_history[-1] != text:
            self.input_history.append(text)
            # Cap and persist
            self.input_history = self.input_history[-MAX_INPUT_HISTORY:]
            self._save_input_history()
        self.clear_input()
        return text

    @staticmethod
    def _load_input_history() -> list:
        try:
            with open(INPUT_HISTORY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(e) for e in data][-MAX_INPUT_HISTORY:]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return []

    def _save_input_history(self):
        try:
            os.makedirs(os.path.dirname(INPUT_HISTORY_FILE), exist_ok=True)
            with open(INPUT_HISTORY_FILE, "w") as f:
                json.dump(self.input_history[-MAX_INPUT_HISTORY:], f)
        except OSError:
            pass

    def _insert_newline(self):
        """Insert a newline at the current cursor position."""
        line = self.input_lines[self.input_row]
        # Split current line at cursor
        before = line[:self.input_col]
        after = line[self.input_col:]
        self.input_lines[self.input_row] = before
        self.input_lines.insert(self.input_row + 1, after)
        self.input_row += 1
        self.input_col = 0

    def _set_input_from_text(self, text: str):
        """Set the input buffer from a string (may contain newlines)."""
        lines = text.split("\n")
        self.input_lines = [list(line) for line in lines]
        if not self.input_lines:
            self.input_lines = [[]]
        self.input_row = len(self.input_lines) - 1
        self.input_col = len(self.input_lines[self.input_row])

    def handle_key(self, key):
        self.needs_redraw = True
        now = time.time()

        # --- Action menu is open: handle menu navigation ---
        if self.menu_open:
            if key == curses.KEY_UP:
                self.menu_index = (self.menu_index - 1) % len(self.menu_items)
                return
            elif key == curses.KEY_DOWN:
                self.menu_index = (self.menu_index + 1) % len(self.menu_items)
                return
            elif key in (curses.KEY_ENTER, 10, 13):
                _, _, action = self.menu_items[self.menu_index]
                self.menu_open = False
                self._execute_menu_action(action)
                return
            elif key == 27 or key == 9:
                self.menu_open = False
                return
            else:
                self.menu_open = False

        # --- Tab: open action menu (when agent running + has input) ---
        if key == 9:
            if self.agent_running and self.get_input_text().strip():
                self.menu_open = True
                self.menu_index = 0
                return
            return

        # --- Ctrl key combos ---
        if key == 19:  # Ctrl+S -- submit
            if self.agent_running:
                self._execute_menu_action("stop_send")
                return
            elif not self.agent_running:
                text = self.submit_input()
                if text:
                    self.process_user_input(text)
                return
        elif key == 17:  # Ctrl+Q
            if self.agent_running:
                self._execute_menu_action("queue")
                return
        elif key == 20:  # Ctrl+T
            if self.agent_running:
                self._execute_menu_action("steer")
                return
        elif key == 3:  # Ctrl+C
            if self.agent_running:
                self.interrupt_event.set()
                self.add_output("[Cancelled]", C_RED)
            return
        elif key == 4:  # Ctrl+D
            self.quit_flag = True
            return
        elif key == 12:  # Ctrl+L
            with self.output_lock:
                self.output_lines.clear()
            self.scroll_offset = 0
            return
        elif key == 15:  # Ctrl+O -- toggle todo popup
            if self.todo_popup_anim_state in ("shown", "expanding"):
                self.todo_popup_anim_state = "collapsing"
            else:
                h, w = self.stdscr.getmaxyx()
                max_popup_h = max(4, h - 8)
                self._open_todo_popup(w, max_popup_h)
            return
        elif key == 16:  # Ctrl+P -- toggle image popup
            if self.image_popup_anim_state in ("shown", "expanding"):
                self.image_popup_anim_state = "collapsing"
                self._image_view_index = None
            else:
                h, w = self.stdscr.getmaxyx()
                max_popup_h = max(4, h - 8)
                self._open_image_popup(w, max_popup_h)
            return
        elif key == 24:  # Ctrl+X -- import clipboard image
            self._try_paste_image()
            return
        elif key == 22:  # Ctrl+V -- paste (check clipboard for image)
            self._try_paste_image()
            return

        # --- Escape: record time for Alt+Enter detection ---
        if key == 27:
            self._last_escape_time = now
            return

        # --- Enter ---
        if key in (curses.KEY_ENTER, 10, 13):
            if (now - self._last_escape_time) < 0.15:
                self._last_escape_time = 0.0
                if self.agent_running and self.get_input_text().strip():
                    self.menu_open = True
                    self.menu_index = 0
                    return
                text = self.submit_input()
                if text:
                    self.process_user_input(text)
                return
            self._last_escape_time = 0.0
            self._insert_newline()
            return

        # --- Arrow keys: Left / Right ---
        if key == curses.KEY_LEFT:
            if self.input_col > 0:
                self.input_col -= 1
            elif self.input_row > 0:
                self.input_row -= 1
                self.input_col = len(self.input_lines[self.input_row])
            return
        elif key == curses.KEY_RIGHT:
            if self.input_col < len(self.input_lines[self.input_row]):
                self.input_col += 1
            elif self.input_row < len(self.input_lines) - 1:
                self.input_row += 1
                self.input_col = 0
            return
        elif key == curses.KEY_HOME or key == 1:  # Ctrl+A
            self.input_col = 0
            return
        elif key == curses.KEY_END or key == 5:  # Ctrl+E
            self.input_col = len(self.input_lines[self.input_row])
            return

        # --- Up / Down: popup scroll takes priority ---
        if self.image_popup_anim_state == "shown" and self._image_popup_scrollable:
            if key == curses.KEY_UP:
                self.image_popup_scroll = max(0, self.image_popup_scroll - 1)
                return
            if key == curses.KEY_DOWN:
                self.image_popup_scroll += 1
                self.needs_redraw = True
                return
        elif self.todo_popup_anim_state == "shown" and self._todo_popup_scrollable:
            if key == curses.KEY_UP:
                self.todo_popup_scroll = max(0, self.todo_popup_scroll - 1)
                return
            if key == curses.KEY_DOWN:
                self.todo_popup_scroll += 1
                self.needs_redraw = True
                return

        # --- Image popup navigation ---
        if self.image_popup_anim_state == "shown":
            if key == curses.KEY_UP:
                if self._image_view_index is None:
                    self._image_view_index = 0
                else:
                    self._image_view_index = max(0, self._image_view_index - 1)
                self.needs_redraw = True
                return
            elif key == curses.KEY_DOWN:
                if self._image_view_index is None:
                    self._image_view_index = 0
                else:
                    self._image_view_index = min(len(self.pasted_images) - 1, self._image_view_index + 1)
                self.needs_redraw = True
                return
            elif key in (curses.KEY_ENTER, 10, 13):
                if self._image_view_index is not None and self._image_view_index < len(self.pasted_images):
                    img = self.pasted_images[self._image_view_index]
                    self._show_iterm2_image(img["path"])
                return
            elif key == 9:
                self.image_popup_anim_state = "collapsing"
                self._image_view_index = None
                return

        # --- Up / Down: navigate within lines first, then history ---
        if key == curses.KEY_UP:
            if self.input_row > 0:
                self.input_row -= 1
                self.input_col = min(self.input_col, len(self.input_lines[self.input_row]))
                self._at_top_edge = False
            elif self._at_top_edge:
                if self.input_history:
                    if self.history_index == -1:
                        self.saved_input = self.get_input_text()
                        self.history_index = len(self.input_history) - 1
                    elif self.history_index > 0:
                        self.history_index -= 1
                    self._set_input_from_text(self.input_history[self.history_index])
                self._at_top_edge = False
            else:
                self._at_top_edge = True
            self._at_bottom_edge = False
            return
        elif key == curses.KEY_DOWN:
            if self.input_row < len(self.input_lines) - 1:
                self.input_row += 1
                self.input_col = min(self.input_col, len(self.input_lines[self.input_row]))
                self._at_bottom_edge = False
            elif self._at_bottom_edge:
                if self.history_index >= 0:
                    if self.history_index < len(self.input_history) - 1:
                        self.history_index += 1
                        self._set_input_from_text(self.input_history[self.history_index])
                    else:
                        self.history_index = -1
                        self._set_input_from_text(self.saved_input)
                self._at_bottom_edge = False
            else:
                self._at_bottom_edge = True
            self._at_top_edge = False
            return

        # --- Scroll output ---
        if key == curses.KEY_PPAGE:
            self.scroll_offset = min(self.scroll_offset + 10, max(0, len(self.output_lines) - 5))
            return
        elif key == curses.KEY_NPAGE:
            self.scroll_offset = max(0, self.scroll_offset - 10)
            return

        # --- Backspace ---
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.input_col > 0:
                self.input_lines[self.input_row].pop(self.input_col - 1)
                self.input_col -= 1
            elif self.input_row > 0:
                prev_len = len(self.input_lines[self.input_row - 1])
                self.input_lines[self.input_row - 1].extend(self.input_lines[self.input_row])
                self.input_lines.pop(self.input_row)
                self.input_row -= 1
                self.input_col = prev_len
            return

        # --- Delete ---
        if key == curses.KEY_DC:
            if self.input_col < len(self.input_lines[self.input_row]):
                self.input_lines[self.input_row].pop(self.input_col)
            elif self.input_row < len(self.input_lines) - 1:
                self.input_lines[self.input_row].extend(self.input_lines[self.input_row + 1])
                self.input_lines.pop(self.input_row + 1)
            return

        # --- Kill line (Ctrl+K) ---
        if key == 11:
            self.input_lines[self.input_row] = self.input_lines[self.input_row][:self.input_col]
            return

        # --- Kill to start (Ctrl+U) ---
        if key == 21:
            self.input_lines[self.input_row] = self.input_lines[self.input_row][self.input_col:]
            self.input_col = 0
            return

        # --- Normal character (including pasted content with newlines) ---
        if isinstance(key, int) and 32 <= key <= 126:
            ch = chr(key)
            self.input_lines[self.input_row].insert(self.input_col, ch)
            self.input_col += 1
        elif isinstance(key, str) and len(key) == 1:
            if key == "\n":
                self._insert_newline()
            elif ord(key) >= 32:
                self.input_lines[self.input_row].insert(self.input_col, key)
                self.input_col += 1

    # ── Image paste helpers ─────────────────────────────────────────────────

    def _save_clipboard_image(self):
        """Save the current macOS clipboard image to disk and return metadata.
        Returns None when the clipboard does not currently contain an image.
        """
        osascript_cmd = shutil.which("osascript") or "/usr/bin/osascript"
        if not os.path.exists(osascript_cmd):
            raise RuntimeError("osascript not found")

        # Check if clipboard has image data via osascript
        check = subprocess.run(
            [osascript_cmd, "-e",
             'try\n'
             '  set imgData to (the clipboard as TIFF picture)\n'
             '  return "yes"\n'
             'on error\n'
             '  return "no"\n'
             'end try'],
            capture_output=True, text=True, timeout=3
        )
        if check.stdout.strip() != "yes":
            return None

        os.makedirs(os.path.expanduser("~/.omlx/images"), exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        idx = len(self.pasted_images) + 1
        filename = f"paste-{ts}-{idx}.png"
        dest = os.path.expanduser(f"~/.omlx/images/{filename}")

        pngpaste_cmd = shutil.which("pngpaste")
        saved = False

        # Fast path when pngpaste is installed.
        if pngpaste_cmd:
            result = subprocess.run([pngpaste_cmd, dest], capture_output=True)
            saved = result.returncode == 0 and os.path.exists(dest)

        # Fallback path (built-in macOS tools): osascript -> TIFF -> sips -> PNG.
        if not saved:
            tiff_dest = dest.replace(".png", ".tiff")
            script = (
                f'set imgData to (the clipboard as TIFF picture)\n'
                f'set f to open for access POSIX file "{tiff_dest}" with write permission\n'
                f'write imgData to f\n'
                f'close access f'
            )
            r2 = subprocess.run([osascript_cmd, "-e", script], capture_output=True)
            if r2.returncode == 0 and os.path.exists(tiff_dest):
                sips_cmd = shutil.which("sips") or "/usr/bin/sips"
                if os.path.exists(sips_cmd):
                    subprocess.run([sips_cmd, "-s", "format", "png", tiff_dest, "--out", dest], capture_output=True)
                    saved = os.path.exists(dest)
                try:
                    os.remove(tiff_dest)
                except OSError:
                    pass

        if not os.path.exists(dest):
            return None

        with open(dest, "rb") as image_file:
            image_hash = hashlib.sha1(image_file.read()).hexdigest()

        return {"filename": filename, "path": dest, "hash": image_hash}

    def _insert_image_reference(self, filename: str):
        tag = f"[image:{filename}]"
        for ch in tag:
            self.input_lines[self.input_row].insert(self.input_col, ch)
            self.input_col += 1

    def _refresh_image_popup_if_open(self):
        if self.image_popup_anim_state in ("shown", "expanding"):
            h, w = self.stdscr.getmaxyx()
            self._open_image_popup(w, max(4, h - 8))

    def _try_paste_image(self):
        """Try to read an image from the macOS clipboard and save it as a temp file."""
        try:
            saved = self._save_clipboard_image()
            if not saved:
                return

            self._last_clipboard_image_hash = saved["hash"]
            self.pasted_images.append({"filename": saved["filename"], "path": saved["path"]})
            self._insert_image_reference(saved["filename"])
            self.add_output(f"[Image pasted: {saved['filename']} | Ctrl+P to manage images]", C_MAGENTA)
            self._refresh_image_popup_if_open()
        except RuntimeError as exc:
            self.add_output(f"[Image paste skipped: {exc}]", C_DIM)
        except Exception as exc:
            self.add_output(f"[Image paste error: {exc}]", C_RED)

    def _show_iterm2_image(self, path: str):
        """Display an image inline using the iTerm2 inline image protocol.
        Temporarily suspends curses, prints the image, then resumes."""
        if not os.path.exists(path):
            self.add_output(f"[Image not found: {path}]", C_RED)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode()
            # iTerm2 inline image escape: ESC ] 1337 ; File=... : <base64> BEL
            iterm_seq = f"\x1b]1337;File=inline=1;width=auto;height=auto;preserveAspectRatio=1:{b64}\x07"
            # Suspend curses to write raw escape to stdout
            curses.endwin()
            sys.stdout.write(iterm_seq + "\n")
            sys.stdout.flush()
            # Wait for user to press Enter, then restore
            sys.stdin.readline()
            self.stdscr = curses.initscr()
            curses.noecho()
            curses.cbreak()
            self.stdscr.keypad(True)
            self.stdscr.nodelay(True)
            curses.start_color()
            curses.use_default_colors()
            self.needs_redraw = True
        except Exception as exc:
            self.add_output(f"[iTerm2 preview error: {exc}]", C_RED)

    # ── Image popup helpers ────────────────────────────────────────────────
    def _get_image_raw_lines(self) -> list:
        if not self.pasted_images:
            return ["  No images pasted"]
        lines = []
        for i, img in enumerate(self.pasted_images):
            lines.append(f"  {i+1}. {img['filename']}")
        return lines

    def _open_image_popup(self, max_w: int, max_h: int):
        raw = self._get_image_raw_lines()
        count = len(self.pasted_images)
        self.image_popup_title = f" Images ({count}) " if count else " Images "
        max_line_len = max((len(l) for l in raw), default=0)
        ideal_inner = max(max_line_len, len(self.image_popup_title))
        inner_w = min(ideal_inner, max_w - 4)
        inner_w = max(inner_w, 10)
        self.image_popup_width = inner_w + 4
        self.image_popup_lines = []
        for line in raw:
            if len(line) <= inner_w:
                self.image_popup_lines.append(line)
            else:
                chunks = textwrap.wrap(line, inner_w, subsequent_indent="       ", break_long_words=True, break_on_hyphens=False)
                self.image_popup_lines.extend(chunks if chunks else [line[:inner_w]])
        content_h = len(self.image_popup_lines)
        ideal_h = content_h + 2
        self.image_popup_target_height = min(ideal_h, max_h)
        self._image_popup_scrollable = content_h > (self.image_popup_target_height - 2)
        self.image_popup_scroll = 0
        self.image_popup_anim_state = "expanding"
        self.image_popup_anim_height = 1
        self._image_view_index = None
        self.needs_redraw = True

    # ── Command dispatch ─────────────────────────────────────────────────

    def _execute_menu_action(self, action: str):
        """Execute an action from the popup menu or keyboard shortcut."""
        text = self.submit_input(action)
        if action == "cancel":
            self.interrupt_event.set()
            self.add_output("[Cancelled]", C_RED)
        elif action == "stop_send":
            if text:
                self.interrupt_event.set()
                self.add_output(f">>> [Stop & Send] {text}", C_YELLOW)
                self.steer_queue.put(("stop_send", text))
            else:
                self.add_output("[Stop & Send needs typed text]", C_DIM)
        elif action == "queue":
            if text:
                self.pending_queue.put(text)
                self.add_output(f">>> [Queued] {text}", C_DIM)
            else:
                self.add_output("[Queue needs typed text]", C_DIM)
        elif action == "steer":
            if text:
                self.add_output(f">>> [Steer] {text}", C_YELLOW)
                self.steer_queue.put(("steer", text))
            else:
                self.add_output("[Steer needs typed text]", C_DIM)

    def process_user_input(self, text: str):
        """Handle user input (slash commands or chat message)."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.add_output(f"[{ts}] You: {text}", C_CYAN)

        # Slash commands (synchronous)
        if text == "/quit":
            self.quit_flag = True
            return
        elif text == "/help":
            self.add_output(HELP_TEXT, C_DEFAULT)
            return
        elif text == "/model":
            self._handle_model_switch()
            return
        elif text == "/clear":
            self.messages.clear()
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT.format(work_dir=WORK_DIR)})
            self.active_ce_mode = None
            self.workflow_state = CEWorkflowState()
            SESSION_TODOS.clear()
            _reset_session_docs()
            with self.output_lock:
                self.output_lines.clear()
            self.add_output("Conversation, todos, and critical context cleared.", C_GREEN)
            return
        elif text == "/status":
            lines = ["Model assignments:"]
            for gk, gl, gd in MODEL_GROUPS:
                cfg = self.model_groups[gk]
                lines.append(f"  {gl}: {cfg['model']} (conc: {cfg.get('concurrency', 1)})")
            lines.append(f"Active: {self.active_model} [{self.active_group}]")
            lines.append(f"Messages: {len(self.messages)}")
            lines.append(f"Working dir: {WORK_DIR}")
            if self.active_ce_mode:
                lines.append(f"CE Mode: {self.active_ce_mode}")
            if self.workflow_state.active:
                wait_state = "waiting for user" if self.workflow_state.awaiting_user else "running"
                lines.append(f"Managed flow: {wait_state}")
                if self.workflow_state.completed_phases:
                    lines.append(f"Completed phases: {', '.join(self.workflow_state.completed_phases)}")
            if SESSION_TODOS:
                done = sum(1 for t in SESSION_TODOS if t["done"])
                lines.append(f"Todos: {done}/{len(SESSION_TODOS)} complete")
            lines.append(tool_git_status())
            self.add_output("\n".join(lines), C_DEFAULT)
            return
        elif text == "/ce:flow":
            self._start_managed_flow("Drive the full Compound Engineering workflow for the current task.")
            return
        elif text.startswith("/ce:flow "):
            self._start_managed_flow(text[len("/ce:flow "):].strip())
            return
        elif text == "/ce:learnings":
            self.add_output(tool_ce_read_learnings(), C_DEFAULT)
            return
        elif text == "/ce:todos":
            self.add_output(tool_ce_manage_todos("list"), C_DEFAULT)
            return
        elif text == "/ce:done":
            if self.active_ce_mode:
                self.add_output(f"--- Exiting {self.active_ce_mode.upper()} mode ---", C_MAGENTA)
                self.active_ce_mode = None
                self.workflow_state = CEWorkflowState()
            else:
                self.add_output("Not in a CE mode.", C_DEFAULT)
            return

        if self.workflow_state.active and self.active_ce_mode == "flow":
            self.workflow_state.awaiting_user = False
            self.workflow_state.pending_question = ""
            self.workflow_state.manager_messages.append({
                "role": "user",
                "content": f"User response for the managed workflow:\n{text}",
            })
            self._run_managed_flow_async()
            return

        # CE workflow commands -- match /ce:cmd at start, as prefix, or inline
        ce_cmd = None
        ce_extra = ""
        for cmd_name in all_ce_modes():
            if text == f"/ce:{cmd_name}":
                ce_cmd = cmd_name
                break
            elif text.startswith(f"/ce:{cmd_name} "):
                ce_cmd = cmd_name
                ce_extra = text[len(f"/ce:{cmd_name} "):]
                break
            elif f"/ce:{cmd_name}" in text:
                ce_cmd = cmd_name
                import re
                ce_extra = re.sub(r'\(?\s*(?:using\s+)?/ce:' + re.escape(cmd_name) + r'\s*\)?', '', text).strip()
                break

        if ce_cmd:
            # Nag (once per session) if /ce:setup hasn't been completed for this repo,
            # but never block the command and never nag /ce:setup itself.
            if ce_cmd != "setup" and not self._ce_setup_warned:
                try:
                    _status = ce_setup_status(WORK_DIR)
                    if not _status["ok"]:
                        for _ln in format_ce_setup_warning(_status):
                            self.add_output(_ln, C_YELLOW)
                        self._ce_setup_warned = True
                except Exception:
                    pass
            self.active_ce_mode = ce_cmd
            mode_prompt = get_ce_prompt(ce_cmd)
            self.messages.append({
                "role": "system",
                "content": f"[Entering {ce_cmd.upper()} mode]\n{mode_prompt}",
            })
            self.add_output(f"--- Entering {ce_cmd.upper()} mode ---", C_MAGENTA)

            if ce_extra:
                raw_content = ce_extra
            else:
                defaults = {
                    "review": "Review the current changes in this branch.",
                    "compound": "Document learnings from the recent work.",
                    "ideate": "Analyze this project and suggest improvements.",
                }
                raw_content = defaults.get(ce_cmd, f"I am ready to {ce_cmd}. What should we work on?")

            self.messages.append({"role": "user", "content": self._build_user_content(raw_content)})
            self._run_agent_async()
            return

        # Normal message -- attach any [image:filename] references as multimodal content
        user_content = self._build_user_content(text)
        self.messages.append({"role": "user", "content": user_content})
        self._run_agent_async()

    def _build_user_content(self, text: str):
        """Build message content, embedding any [image:filename] tags as base64 image parts."""
        # Find all image tags in the text
        image_refs = re.findall(r'\[image:([^\]]+)\]', text)
        if not image_refs:
            return text

        # Build multimodal content list
        parts = []
        # Split text at image tags and interleave with image parts
        segments = re.split(r'\[image:[^\]]+\]', text)
        for i, seg in enumerate(segments):
            if seg.strip():
                parts.append({"type": "text", "text": seg})
            if i < len(image_refs):
                ref = image_refs[i]
                # Look up the path from pasted_images
                img_path = None
                for img in self.pasted_images:
                    if img["filename"] == ref:
                        img_path = img["path"]
                        break
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        ext = os.path.splitext(img_path)[1].lstrip(".").lower() or "png"
                        mime = f"image/{ext}" if ext in ("png", "jpg", "jpeg", "gif", "webp") else "image/png"
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}
                        })
                    except Exception:
                        parts.append({"type": "text", "text": f"[image: {ref} (failed to load)]"})
                else:
                    parts.append({"type": "text", "text": f"[image: {ref} (not found)]"})

        # If only one text segment and no images loaded, return plain string
        if all(p["type"] == "text" for p in parts):
            return "\n".join(p["text"] for p in parts if p["text"].strip())

        return parts

    def _handle_model_switch(self):
        """Show model list; user types a group prefix + model number to switch."""
        if not self.models:
            self.add_output("No models available.", C_RED)
            return
        group_keys = [gk for gk, _, _ in MODEL_GROUPS]
        lines = ["Current model assignments:"]
        for i, (gk, gl, _) in enumerate(MODEL_GROUPS):
            cfg = self.model_groups[gk]
            prefix = chr(ord('a') + i)  # a, b, c, d, e
            lines.append(f"  {prefix}) {gl}: {cfg['model']}")
        lines.append("")
        lines.append("Available Models:")
        for i, m in enumerate(self.models, 1):
            lines.append(f"  {i}. {m}")
        lines.append("")
        lines.append("Type '<letter><N>' to change a group's model:")
        selector_map = ", ".join(f"{chr(ord('a') + i)}={label}" for i, (_, label, _) in enumerate(MODEL_GROUPS))
        lines.append(f"  {selector_map}")
        lines.append("  e.g. a1 = set the first group to model 1")
        self.add_output("\n".join(lines), C_DEFAULT)
        self._pending_model_select = True

    def _try_model_select(self, text: str) -> bool:
        """If we're in model select mode, try to handle the input."""
        if not getattr(self, '_pending_model_select', False):
            return False
        self._pending_model_select = False
        text = text.strip().lower()
        group_keys = [gk for gk, _, _ in MODEL_GROUPS]
        group_labels = [gl for _, gl, _ in MODEL_GROUPS]
        valid_prefixes = ''.join(chr(ord('a') + i) for i in range(len(group_keys)))
        if len(text) >= 2 and text[0] in valid_prefixes:
            group_idx = ord(text[0]) - ord('a')
            if group_idx < len(group_keys):
                try:
                    model_idx = int(text[1:]) - 1
                    if 0 <= model_idx < len(self.models):
                        chosen = self.models[model_idx]
                        gk = group_keys[group_idx]
                        old_model = self.model_groups[gk]["model"]
                        conc, mem = _estimate_model_defaults(chosen, gk)
                        self.model_groups[gk] = {"model": chosen, "concurrency": conc, "memory": mem}
                        # Force model reload on next API call
                        self._current_group = None
                        self.add_output(f"{group_labels[group_idx]}: {chosen} (conc: {conc}, mem: {mem})", C_GREEN)
                        return True
                except ValueError:
                    pass
        self.add_output(f"Invalid choice. Use <letter><N>. Letters: {valid_prefixes}.", C_YELLOW)
        return True

    def _start_managed_flow(self, objective: str):
        self.workflow_state = CEWorkflowState(
            active=True,
            objective=objective,
            manager_messages=[
                {"role": "system", "content": MANAGER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"User request:\n{objective}\n\n"
                        f"Current workflow artifacts:\n{_format_workflow_artifacts()}\n\n"
                        "Start orchestrating the Compound Engineering flow."
                    ),
                },
            ],
        )
        self.active_ce_mode = "flow"
        self.add_output("--- Entering FLOW mode ---", C_MAGENTA)
        self._run_managed_flow_async()

    def _run_managed_flow_async(self):
        self.agent_running = True
        self.interrupt_event.clear()
        self.needs_redraw = True

        def worker():
            try:
                response = self._managed_flow_tui()
                if response:
                    ts = datetime.now().strftime("%H:%M:%S")
                    self.add_output(f"\n[{ts}] Agent: {response}", C_GREEN)
            except Exception as e:
                self.add_output(f"\nAgent error: {e}", C_RED)
            finally:
                self.agent_running = False
                self.needs_redraw = True
                self._drain_pending_queue()

        self.agent_thread = threading.Thread(target=worker, daemon=True)
        self.agent_thread.start()

    def _run_workflow_manager_turn(self) -> dict:
        saved_mode = self.active_ce_mode
        self.active_ce_mode = "flow"
        self._ensure_model()
        self._activity = "manager deciding..."
        self.needs_redraw = True
        try:
            decision, raw_text = run_manager_turn(self.workflow_state.manager_messages, self.active_model)
        except Exception as exc:
            decision = None
            raw_text = f"[manager exception] {exc}"
        self.active_ce_mode = saved_mode or "flow"
        if not decision:
            self.workflow_state.manager_failures += 1
            decision = _manager_autorecover_decision(self.workflow_state, raw_text)
            self.workflow_state.manager_messages.append({
                "role": "user",
                "content": f"[Manager autorecovery triggered] {decision['message']}",
            })
        return decision

    def _verify_managed_completion(self, decision: dict) -> dict | None:
        """Validate a manager 'complete' decision against the original objective.

        Returns an override decision (action=run_phase) if the flow has not
        actually finished the user's request. Returns None to accept completion.
        """
        # Cap the number of completion rejections so we cannot loop forever.
        if self.workflow_state.rejected_completions >= 1:
            return None

        deterministic = _deterministic_completion_block(self.workflow_state)
        if deterministic is not None:
            return deterministic

        manager_model = self.model_groups["manager"]["model"]
        # Make sure the manager model is loaded for the verifier turn.
        if self._current_model_name != manager_model:
            unload_omlx_model(self._current_model_name) if self._current_model_name else None
            self._current_group = None
            self._current_model_name = None
            saved_mode = self.active_ce_mode
            self.active_ce_mode = "flow"
            self._ensure_model()
            self.active_ce_mode = saved_mode or "flow"

        verdict = run_completion_verifier(
            self.workflow_state, decision.get("message", ""), manager_model
        )
        if not verdict or verdict["verdict"] == "complete":
            return None

        artifacts = _workflow_artifact_snapshot()
        next_phase = verdict.get("next_phase") or ""
        valid_phases = {"brainstorm", "plan", "deepen_plan", "work", "review", "compound"}
        if next_phase not in valid_phases:
            # Fall back to deterministic next-phase picker.
            fallback = _manager_autorecover_decision(self.workflow_state, "completion verifier did not specify next phase")
            if fallback.get("action") != "run_phase":
                return None
            next_phase = fallback["phase"]
            phase_input = fallback["phase_input"]
        else:
            phase_input = verdict.get("next_phase_input") or _failsafe_phase_input(
                next_phase, self.workflow_state.objective, artifacts
            )

        missing = verdict.get("missing") or "objective not yet satisfied"
        return {
            "action": "run_phase",
            "phase": next_phase,
            "message": f"Completion rejected by verifier: {missing}",
            "question": "",
            "phase_input": phase_input,
            "reason": missing,
        }

    def _run_phase_from_flow(self, phase: str, phase_input: str) -> str:
        actual_phase = _phase_to_ce_mode(phase)
        mode_prompt = get_ce_prompt(actual_phase)
        handoff = _build_manager_handoff(self.workflow_state.manager_messages, phase, phase_input)
        manager_model = self.model_groups["manager"]["model"]
        target_model = self.model_groups[CE_MODE_TO_GROUP[actual_phase]]["model"]
        if self._current_model_name == manager_model and manager_model != target_model:
            unload_omlx_model(manager_model)
            self._current_group = None
            self._current_model_name = None

        start_idx = len(self.messages)
        self.messages.append({
            "role": "system",
            "content": f"[Entering {phase.upper()} via FLOW manager]\n{mode_prompt}",
        })
        self.messages.append({"role": "system", "content": handoff})
        self.messages.append({"role": "user", "content": self._build_user_content(phase_input)})
        saved_mode = self.active_ce_mode
        self.active_ce_mode = actual_phase
        result = self._agent_turn_tui()
        self.active_ce_mode = saved_mode or "flow"
        del self.messages[start_idx:start_idx + 2]
        return result

    def _managed_flow_tui(self) -> str:
        max_iterations = 80
        iterations = 0
        while self.workflow_state.active:
            iterations += 1
            if iterations > max_iterations:
                self.workflow_state.active = False
                self.active_ce_mode = None
                return "Managed flow stopped after too many recovery iterations."
            if self.interrupt_event.is_set():
                self.interrupt_event.clear()
                return "[Cancelled by user]"

            decision = self._run_workflow_manager_turn()
            action = decision["action"]

            if action == "complete":
                override = self._verify_managed_completion(decision)
                if override is not None:
                    decision = override
                    action = override["action"]
                    self.workflow_state.rejected_completions += 1
                    self.workflow_state.manager_messages.append({
                        "role": "user",
                        "content": (
                            "[Completion verifier rejected]\n"
                            f"Reason: {override.get('reason', '')}\n"
                            f"Resuming with phase: {override.get('phase', '')}."
                        ),
                    })
                    self.add_output(
                        f"[Manager] Completion rejected: {override.get('reason', '')}",
                        C_YELLOW,
                    )
                else:
                    self.workflow_state.active = False
                    self.workflow_state.awaiting_user = False
                    self.workflow_state.pending_question = ""
                    self.active_ce_mode = None
                    return decision["message"] or "Managed Compound Engineering flow complete."

            if action == "ask_user":
                # Re-check in case override produced a question (it shouldn't, but be safe)
                question = decision["question"] or decision["message"] or "What should I clarify before continuing?"
                self.workflow_state.awaiting_user = True
                self.workflow_state.pending_question = question
                return question

            phase = decision["phase"]
            status = decision["message"] or decision["reason"] or f"Running {phase}"
            self.add_output(f"[Manager] {status}", C_YELLOW)
            phase_succeeded = True
            try:
                result = self._run_phase_from_flow(phase, decision["phase_input"])
                self.workflow_state.phase_failures = 0
            except Exception as exc:
                phase_succeeded = False
                self.workflow_state.phase_failures += 1
                result = f"[Phase {phase} exception] {exc}"
            if phase_succeeded:
                self.workflow_state.completed_phases.append(phase)
            self.workflow_state.awaiting_user = False
            self.workflow_state.pending_question = ""
            self.workflow_state.manager_messages.append({
                "role": "user",
                "content": (
                    f"Phase {'completed' if phase_succeeded else 'failed'}: {phase}\n"
                    f"Completed phases: {', '.join(self.workflow_state.completed_phases)}\n"
                    f"Workflow artifacts:\n{_format_workflow_artifacts()}\n\n"
                    f"Phase result:\n{result[:4000]}"
                ),
            })

        self.active_ce_mode = None
        return "Managed Compound Engineering flow complete."

    def _try_concurrency_select(self, text: str) -> bool:
        """No longer used - concurrency is auto-estimated."""
        return False

    # ── Threaded agent execution ─────────────────────────────────────────

    def _run_agent_async(self):
        """Run agent_turn in a background thread."""
        self.agent_running = True
        self.interrupt_event.clear()
        self.needs_redraw = True

        def worker():
            try:
                response = self._agent_turn_tui()
                ts = datetime.now().strftime("%H:%M:%S")
                self.add_output(f"\n[{ts}] Agent: {response}", C_GREEN)
            except Exception as e:
                self.add_output(f"\nAgent error: {e}", C_RED)
            finally:
                self.agent_running = False
                self.needs_redraw = True
                # Process any queued messages
                self._drain_pending_queue()

        self.agent_thread = threading.Thread(target=worker, daemon=True)
        self.agent_thread.start()

    def _agent_turn_tui(self) -> str:
        """Agent turn that uses tui_print and checks for interrupts/steers."""
        self.current_round = 0
        total_rounds_used = 0

        while True:
            for round_num in range(MAX_TOOL_ROUNDS):
                self.current_round = total_rounds_used + round_num + 1
                self.needs_redraw = True

                # Check for interrupt
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    try:
                        mode, text = self.steer_queue.get_nowait()
                        if mode == "stop_send":
                            self.messages.append({"role": "user", "content": text})
                            continue
                    except queue.Empty:
                        return "[Cancelled by user]"

                # Check for steer messages
                try:
                    mode, text = self.steer_queue.get_nowait()
                    if mode == "steer":
                        self.messages.append({"role": "user", "content": f"[User steer]: {text}"})
                except queue.Empty:
                    pass

                self._ensure_model()
                self._activity = "calling API..."
                self.needs_redraw = True
                response = api_call(self.messages, self.active_model)
                if response and response.get("_context_overflow"):
                    self.messages = emergency_trim(self.messages)
                    continue
                if not response:
                    return "[API call failed]"

                parsed = normalize_api_response(response)
                text = parsed["text"]
                tool_calls = parsed["tool_calls"]
                finish_reason = parsed["finish_reason"]

                # Show usage info if available
                usage = parsed["usage"]
                if usage:
                    prompt_tok = usage.get("prompt_tokens", 0)
                    gen_tok = usage.get("completion_tokens", 0)
                    self._activity = f"tokens: {prompt_tok}p/{gen_tok}g"
                    self.needs_redraw = True

                if parsed["reasoning_content"]:
                    self._activity = "thinking..."
                    self.needs_redraw = True
                    rc = parsed["reasoning_content"]
                    display = f"{rc[:300]}..." if len(rc) > 300 else rc
                    _ts = datetime.now().strftime("%H:%M:%S")
                    tui_print(f"[{_ts}] [thinking] {display}", C_DIM)

                # Structured tool calls
                if tool_calls:
                    self.messages.append({
                        "role": "assistant",
                        "content": text,
                        "tool_calls": tool_calls,
                    })
                    if text:
                        _ts = datetime.now().strftime("%H:%M:%S")
                        tui_print(f"[{_ts}] {text}", C_YELLOW)
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        name = fn.get("name")
                        if not name:
                            continue
                        result = execute_tool(name, fn.get("arguments", {}))
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "call_unknown"),
                            "content": result,
                        })
                    continue

                # Fallback: parse <tool_call>/<tool_use> tags from text content
                text_calls = parse_text_tool_calls(text)
                if text_calls:
                    display_text = re.sub(r'<tool_(?:call|use)>.*?</tool_(?:call|use)>', '', text, flags=re.DOTALL)
                    display_text = re.sub(r'<tool_(?:call|use)>.*$', '', display_text, flags=re.DOTALL).strip()
                    if display_text:
                        _ts = datetime.now().strftime("%H:%M:%S")
                        tui_print(f"[{_ts}] {display_text}", C_YELLOW)

                    tool_results = []
                    for tc in text_calls:
                        result = execute_tool(tc["name"], tc["arguments"])
                        tool_results.append(f"[{tc['name']}] {result}")

                    self.messages.append({"role": "assistant", "content": text})
                    combined_results = "\n\n".join(tool_results)
                    self.messages.append({"role": "user", "content": f"[Tool results]:\n{combined_results}\n\nContinue with the task."})
                    continue

                # Auto-continue if generation was truncated (hit max_tokens)
                if finish_reason == "length":
                    _ts = datetime.now().strftime("%H:%M:%S")
                    tui_print(f"[{_ts}] [Generation hit token limit -- auto-continuing]", C_YELLOW)
                    if text:  # don't append empty assistant turns; null content breaks some models
                        self.messages.append({"role": "assistant", "content": text})
                    self.messages.append({"role": "user", "content": "[System: Your response was truncated because it hit the generation token limit. Continue EXACTLY where you left off. Do NOT repeat what you already said. If you were about to make a tool call, make it now.]"})
                    continue

                if not text:
                    if _is_retryable_empty_response(parsed, self.messages) and round_num < MAX_TOOL_ROUNDS - 1:
                        if finish_reason == "tool_calls":
                            nudge = "[System: Your last response indicated a tool call (finish_reason=tool_calls) but no tool call was parseable. Please resend your tool call in the standard format.]"
                        elif finish_reason == "stop":
                            nudge = "[System: Your previous response ended with finish_reason=stop but contained no visible content or tool call. Resend your final answer or tool call, and ensure either content text or tool_calls is present.]"
                        else:
                            nudge = "[System: Your previous response had no visible content or tool call. Reply with either plain text content or a structured tool call now.]"
                        self.messages.append({"role": "user", "content": nudge})
                        continue
                    # Log raw response for post-mortem debugging
                    debug_path = None
                    try:
                        debug_path = _write_malformed_response_debug(response)
                    except Exception:
                        pass
                    detail = parsed["error"] or f"keys={','.join(parsed['response_shape'])}"
                    if debug_path:
                        return f"[Malformed API response: {detail}; debug={debug_path}]"
                    return f"[Malformed API response: {detail}]"

                # Final text response -- but check if the model is narrating instead of acting
                _narration_keywords = [
                    "let me", "i need to", "i'll ", "i will", "i should",
                    "next step", "now i", "let's ",
                    "add a ", "add the ", "you need to", "you should", "you can ",
                    "here is the", "here's the", "the change", "the fix",
                    "insert the", "insert this", "add this", "place this",
                    "to implement", "to add", "would need to", "should be added",
                    "implementation", "result:", "**result**",
                ]
                if (self.active_ce_mode and round_num < MAX_TOOL_ROUNDS - 1
                        and text
                        and any(kw in text.lower() for kw in _narration_keywords)):
                    _ts = datetime.now().strftime("%H:%M:%S")
                    tui_print(f"[{_ts}] [auto-nudge: model narrated instead of acting]", C_DIM)
                    self.messages.append({"role": "assistant", "content": text})
                    self.messages.append({
                        "role": "user",
                        "content": "[System: Do not narrate what to do or show code for the user to apply manually. Use your tools (replace_in_file, write_file) to make the change RIGHT NOW. Make the tool call.]"
                    })
                    continue
                self.messages.append({"role": "assistant", "content": text})
                return text

            # Max rounds hit -- auto-continue instead of stopping
            total_rounds_used += MAX_TOOL_ROUNDS
            tui_print(f"[Round limit {total_rounds_used} reached -- auto-continuing. Steer or Ctrl+C to stop.]", C_YELLOW)
            self.messages.append({
                "role": "user",
                "content": "[System: You have used many tool rounds. Continue working efficiently. Focus on completing the current task with fewer tool calls. Combine operations where possible.]"
            })
            # Loop continues with a fresh round budget

    def _drain_pending_queue(self):
        """Process queued messages after agent finishes."""
        try:
            while True:
                text = self.pending_queue.get_nowait()
                self.add_output(f"[Processing queued] {text}", C_YELLOW)
                self.messages.append({"role": "user", "content": text})
                self._run_agent_async()
                return  # Only process one; the next will be drained recursively
        except queue.Empty:
            pass

    # ── Main TUI loop ────────────────────────────────────────────────────

    def run(self, stdscr):
        global _tui_instance
        _tui_instance = self
        self.stdscr = stdscr

        # Setup curses
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
        curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
        curses.init_pair(C_STATUS_BAR, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_RED, curses.COLOR_RED, -1)
        curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_DIM, 8, -1)  # "bright black" = gray
        curses.init_pair(C_BOLD_GREEN, curses.COLOR_GREEN, -1)

        stdscr.nodelay(True)
        stdscr.keypad(True)
        curses.curs_set(1)
        # Disable terminal driver interception of Ctrl+S and Ctrl+O
        import tty, termios
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        new_attrs = termios.tcgetattr(fd)
        new_attrs[0] &= ~termios.IXON  # iflag: disable XON/XOFF (frees Ctrl+S)
        # Disable VDISCARD (Ctrl+O) -- macOS terminal eats it as "discard output"
        try:
            new_attrs[3] &= ~termios.FLUSHO  # lflag: clear any active flush
            vdiscard_idx = termios.VDISCARD
            new_attrs[6][vdiscard_idx] = 0  # cc: disable VDISCARD char
        except (AttributeError, IndexError):
            pass  # not all platforms define VDISCARD
        termios.tcsetattr(fd, termios.TCSANOW, new_attrs)

        self.add_output("oMLX Coding Agent + Compound Engineering", C_GREEN)
        self.add_output(f"Working directory: {WORK_DIR}", C_DEFAULT)
        for gk, gl, _ in MODEL_GROUPS:
            cfg = self.model_groups[gk]
            self.add_output(f"  {gl}: {cfg['model']} (conc: {cfg.get('concurrency', 1)})", C_GREEN)
        self.add_output("Type /help for commands. Start typing to chat.", C_DIM)
        self.add_output("-" * 60, C_DIM)

        # One-time /ce:setup detection: surface a yellow banner if the repo
        # has not been bootstrapped via the vendored ce-setup skill.
        try:
            _setup_status = ce_setup_status(WORK_DIR)
            if not _setup_status["ok"]:
                for _ln in format_ce_setup_warning(_setup_status):
                    self.add_output(_ln, C_YELLOW)
                self.add_output("-" * 60, C_DIM)
                self._ce_setup_warned = True
        except Exception as _exc:
            self.add_output(f"[ce:setup detect] {_exc}", C_DIM)

        # Start memory monitoring background thread
        self._start_memory_monitor()

        try:
            while not self.quit_flag:
                self.draw()
                try:
                    key = stdscr.get_wch()
                except curses.error:
                    # No input available (nodelay mode)
                    time.sleep(0.03)
                    # Still redraw if agent thread is producing output or animating
                    if self.agent_running or self.todo_popup_anim_state in ("expanding", "collapsing"):
                        self.needs_redraw = True
                    continue

                # Convert to int for special keys
                if isinstance(key, str):
                    if len(key) == 1:
                        key_int = ord(key)
                    else:
                        continue
                else:
                    key_int = key

                # Check model select mode first
                if key_int in (curses.KEY_ENTER, 10, 13):
                    if getattr(self, '_pending_model_select', False):
                        text = self.get_input_text().strip()
                        self.clear_input()
                        self._try_model_select(text)
                        continue

                self.handle_key(key_int)
        finally:
            # Stop memory monitor
            self._stop_memory_monitor()
            # Restore original terminal attributes (re-enable flow control)
            termios.tcsetattr(fd, termios.TCSANOW, old_attrs)

        _tui_instance = None


HELP_TEXT = """Commands:
    /model              Switch model for a CE step group (letter + model#)
  /clear              Clear conversation
  /status             Show git status and session info
  /quit               Exit (also Ctrl+D)

Compound Engineering:
    /ce:flow              Manager-run CE flow across brainstorm, plan, work, review, compound
  /ce:brainstorm        Explore ideas and requirements
  /ce:plan              Create a structured implementation plan
  /ce:work              Execute work with task tracking
  /ce:review            Tiered persona code review (vendored ce-code-review)
  /ce:doc-review        Persona-based requirements/plan document review
  /ce:compound          Document learnings from recent work
  /ce:compound-refresh  Age out, replace, or archive stale learnings
  /ce:debug             Systematic bug investigation
  /ce:ideate            Discover project improvements
  /ce:commit            Value-first git commit
  /ce:pr-description    Generate a PR title and body
  /ce:commit-push-pr    Commit, push, and open a PR
  /ce:setup             Diagnose env and bootstrap project config
  /ce:test-browser      Run browser tests on PR-affected pages
  /ce:done              Exit current CE mode
  /ce:learnings         View all project learnings
  /ce:todos             View current todo list

While Agent is Working:
  Ctrl+S              Stop & Send (interrupt, send your message)
  Ctrl+Q              Queue (send after current task finishes)
  Ctrl+T              Steer (inject message without stopping)
  Ctrl+C              Cancel current operation

Navigation:
  Up/Down             Input history (or scroll todos when open)
  Left/Right          Move cursor in input
  Ctrl+A/Ctrl+E       Home/End of input
  Ctrl+O              Toggle todo list popup
    Ctrl+P              Toggle image list popup
    Ctrl+X              Import clipboard image
  Ctrl+U/Ctrl+K       Kill line before/after cursor
  PageUp/PageDn       Scroll output
  Ctrl+L              Clear output"""


def _estimate_model_defaults(model_name: str, group_key: str) -> tuple:
    """Estimate intelligent defaults for concurrency and memory based on model name."""
    name = model_name.lower()
    # Detect quantization from model name
    if "8bit" in name or "8-bit" in name:
        weight_gb = 27  # 27B at 8-bit
    elif "4bit" in name or "4-bit" in name:
        weight_gb = 14  # 27B at 4-bit
    elif "3bit" in name or "3-bit" in name:
        weight_gb = 11
    else:
        weight_gb = 14  # assume 4-bit if unknown

    # Memory: weights + headroom for KV cache + activations
    # 48GB total, ~6GB OS, so ~42GB usable
    mem_gb = min(weight_gb + 12, 40)
    # Concurrency: thinking-like groups get 1, others depend on model size
    if group_key in ("ideate_brainstorm", "plan"):
        conc = 1  # thinking = single focused request
    elif weight_gb >= 20:
        conc = 1  # large model, keep it simple
    else:
        conc = 2  # small model can handle 2

    return conc, f"{mem_gb}GB"


def _select_models_for_groups(models: list) -> dict:
    """Prompt user to select a model for each CE step group. Returns model_config dict."""
    print("\n\033[1m--- Model Configuration ---\033[0m")
    print("\nAvailable models:")
    for i, m in enumerate(models, 1):
        print(f"  {i}. {m}")

    config = {}
    last_model = None

    for group_key, group_label, group_desc in MODEL_GROUPS:
        if last_model:
            prompt = f"\n\033[1m{group_label}\033[0m ({group_desc})\n  Model [Enter = same as above, or 1-{len(models)}]: "
        else:
            prompt = f"\n\033[1m{group_label}\033[0m ({group_desc})\n  Model [1-{len(models)}]: "

        selected = None
        while not selected:
            try:
                choice = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)

            if not choice and last_model:
                selected = last_model
                break

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(models):
                    selected = models[idx]
                    break
            except ValueError:
                pass

            if last_model:
                print(f"  Invalid. Enter 1-{len(models)} or press Enter for same model.")
            else:
                print(f"  Invalid. Enter 1-{len(models)}.")

        conc, mem = _estimate_model_defaults(selected, group_key)
        config[group_key] = {"model": selected, "concurrency": conc, "memory": mem}
        print(f"  -> {selected} (concurrency: {conc}, memory: {mem})")
        last_model = selected

    return config


def main():
    import argparse
    parser = argparse.ArgumentParser(description="oMLX Coding Agent")
    parser.add_argument("--repo", default=os.getcwd(), help="Path to git repo (default: cwd)")
    parser.add_argument("--model", help="Model name (skip selection, use for all groups)")
    parser.add_argument("--no-tui", action="store_true", help="Use plain text mode (no curses TUI)")
    args = parser.parse_args()

    set_work_dir(args.repo)
    _rotate_temp_files()

    # Fetch models before entering curses
    models = fetch_models()
    if not models:
        print("No models found. Is oMLX running?")
        sys.exit(1)

    if args.model:
        conc, mem = _estimate_model_defaults(args.model, "work")
        model_config = {
            gk: {"model": args.model, "concurrency": conc, "memory": mem}
            for gk, _, _ in MODEL_GROUPS
        }
    else:
        model_config = _select_models_for_groups(models)

    # Set initial settings for the work model (default at startup)
    work_cfg = model_config["work"]
    startup_settings = {"max_concurrent_requests": work_cfg.get("concurrency", 1)}
    if work_cfg.get("memory"):
        startup_settings["max_model_memory"] = work_cfg["memory"]
    set_omlx_settings(startup_settings)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(work_dir=WORK_DIR)},
    ]

    # Load learnings context at startup
    learnings_path = resolve(LEARNINGS_FILE)
    if os.path.exists(learnings_path):
        with open(learnings_path, "r") as f:
            content = f.read()
        if content.strip():
            messages.append({
                "role": "system",
                "content": f"[Project learnings loaded from {LEARNINGS_FILE}:]\n{content[:3000]}",
            })

    if args.no_tui:
        # Fallback to the old plain-text interface
        _main_plain(model_config, messages, models)
    else:
        # Launch the full TUI
        tui = AgentTUI(model_config=model_config, messages=messages, models=models)
        curses.wrapper(tui.run)


def _main_plain(model_config, messages, models):
    """Original plain-text interface (--no-tui mode)."""
    print(f"\033[1moMLX Coding Agent + Compound Engineering\033[0m")
    print(f"Working directory: {WORK_DIR}")
    for gk, gl, _ in MODEL_GROUPS:
        cfg = model_config[gk]
        print(f"\n  {gl}: \033[1;32m{cfg['model']}\033[0m (conc: {cfg.get('concurrency', 1)})")
    print("\nType /help for commands. Start typing to chat.")
    print("-" * 60)

    # One-time /ce:setup detection banner.
    _ce_setup_warned = [False]
    try:
        _setup_status = ce_setup_status(WORK_DIR)
        if not _setup_status["ok"]:
            for _ln in format_ce_setup_warning(_setup_status):
                print(f"\033[33m{_ln}\033[0m")
            print("-" * 60)
            _ce_setup_warned[0] = True
    except Exception as _exc:
        print(f"[ce:setup detect] {_exc}")

    active_ce_mode = None
    workflow_state = CEWorkflowState()
    _current_model_name = None

    def _get_model():
        group = CE_MODE_TO_GROUP.get(active_ce_mode, "work")
        return model_config[group]["model"]

    def _ensure_model_plain():
        nonlocal _current_model_name
        group = CE_MODE_TO_GROUP.get(active_ce_mode, "work")
        cfg = model_config[group]
        new_model = cfg["model"]
        if _current_model_name and _current_model_name != new_model:
            unload_omlx_model(_current_model_name)
            settings = {"max_concurrent_requests": cfg.get("concurrency", 1)}
            if cfg.get("memory"):
                settings["max_model_memory"] = cfg["memory"]
            set_omlx_settings(settings)
        _current_model_name = new_model

    def _run_plain_phase_from_flow(phase: str, phase_input: str, manager_messages: list) -> str:
        nonlocal active_ce_mode, _current_model_name
        actual_phase = _phase_to_ce_mode(phase)
        handoff = _build_manager_handoff(manager_messages, phase, phase_input)
        manager_model = model_config["manager"]["model"]
        target_model = model_config[CE_MODE_TO_GROUP[actual_phase]]["model"]
        if _current_model_name == manager_model and manager_model != target_model:
            unload_omlx_model(manager_model)
            _current_model_name = None

        start_idx = len(messages)
        saved_mode = active_ce_mode
        active_ce_mode = actual_phase
        messages.append({
            "role": "system",
            "content": f"[Entering {phase.upper()} via FLOW manager]\n{get_ce_prompt(actual_phase)}",
        })
        messages.append({"role": "system", "content": handoff})
        messages.append({"role": "user", "content": phase_input})
        _ensure_model_plain()
        result = agent_turn(messages, _get_model())
        active_ce_mode = saved_mode or "flow"
        del messages[start_idx:start_idx + 2]
        return result

    def _verify_plain_completion(workflow_state: CEWorkflowState, decision: dict) -> dict | None:
        nonlocal active_ce_mode, _current_model_name
        if workflow_state.rejected_completions >= 1:
            return None
        deterministic = _deterministic_completion_block(workflow_state)
        if deterministic is not None:
            return deterministic
        manager_model = model_config["manager"]["model"]
        if _current_model_name and _current_model_name != manager_model:
            unload_omlx_model(_current_model_name)
            _current_model_name = None
        active_ce_mode = "flow"
        _ensure_model_plain()
        verdict = run_completion_verifier(workflow_state, decision.get("message", ""), manager_model)
        if not verdict or verdict["verdict"] == "complete":
            return None
        artifacts = _workflow_artifact_snapshot()
        next_phase = verdict.get("next_phase") or ""
        valid_phases = {"brainstorm", "plan", "deepen_plan", "work", "review", "compound"}
        if next_phase not in valid_phases:
            fallback = _manager_autorecover_decision(workflow_state, "completion verifier did not specify next phase")
            if fallback.get("action") != "run_phase":
                return None
            next_phase = fallback["phase"]
            phase_input = fallback["phase_input"]
        else:
            phase_input = verdict.get("next_phase_input") or _failsafe_phase_input(
                next_phase, workflow_state.objective, artifacts
            )
        missing = verdict.get("missing") or "objective not yet satisfied"
        return {
            "action": "run_phase",
            "phase": next_phase,
            "message": f"Completion rejected by verifier: {missing}",
            "question": "",
            "phase_input": phase_input,
            "reason": missing,
        }

    while True:
        try:
            if active_ce_mode:
                prompt_label = f"\033[1;35m[{active_ce_mode}]\033[0m \033[1;34mYou:\033[0m "
            else:
                prompt_label = "\033[1;34mYou:\033[0m "
            user_input = input(f"\n{prompt_label}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            print("Bye!")
            break
        elif user_input == "/help":
            print(HELP_TEXT)
            continue
        elif user_input == "/model":
            print("Model switching in plain mode not yet supported. Restart with new selection.")
            continue
        elif user_input == "/clear":
            messages.clear()
            messages.append({"role": "system", "content": SYSTEM_PROMPT.format(work_dir=WORK_DIR)})
            active_ce_mode = None
            workflow_state = CEWorkflowState()
            SESSION_TODOS.clear()
            _reset_session_docs()
            print("Conversation, todos, and critical context cleared.")
            continue
        elif user_input == "/status":
            print("Model assignments:")
            for gk, gl, _ in MODEL_GROUPS:
                cfg = model_config[gk]
                print(f"  {gl}: {cfg['model']} (conc: {cfg.get('concurrency', 1)})")
            group = CE_MODE_TO_GROUP.get(active_ce_mode, "work")
            print(f"Active: {_get_model()} [{group}]")
            print(f"Messages: {len(messages)}")
            print(f"Working dir: {WORK_DIR}")
            if active_ce_mode:
                print(f"CE Mode: {active_ce_mode}")
            if workflow_state.active:
                wait_state = "waiting for user" if workflow_state.awaiting_user else "running"
                print(f"Managed flow: {wait_state}")
                if workflow_state.completed_phases:
                    print(f"Completed phases: {', '.join(workflow_state.completed_phases)}")
            if SESSION_TODOS:
                done = sum(1 for t in SESSION_TODOS if t["done"])
                print(f"Todos: {done}/{len(SESSION_TODOS)} complete")
            print(tool_git_status())
            continue
        elif user_input == "/ce:flow" or user_input.startswith("/ce:flow "):
            objective = user_input[len("/ce:flow "):].strip() if user_input.startswith("/ce:flow ") else "Drive the full Compound Engineering workflow for the current task."
            workflow_state = CEWorkflowState(
                active=True,
                objective=objective,
                manager_messages=[
                    {"role": "system", "content": MANAGER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"User request:\n{objective}\n\n"
                            f"Current workflow artifacts:\n{_format_workflow_artifacts()}\n\n"
                            "Start orchestrating the Compound Engineering flow."
                        ),
                    },
                ],
            )
            active_ce_mode = "flow"
            print("\n\033[1;35m--- Entering FLOW mode ---\033[0m")
            iterations = 0
            while workflow_state.active:
                iterations += 1
                if iterations > 80:
                    workflow_state.active = False
                    active_ce_mode = None
                    print("Managed flow stopped after too many recovery iterations.")
                    break
                _ensure_model_plain()
                try:
                    decision, raw_text = run_manager_turn(workflow_state.manager_messages, _get_model())
                except Exception as exc:
                    decision = None
                    raw_text = f"[manager exception] {exc}"
                if not decision:
                    workflow_state.manager_failures += 1
                    decision = _manager_autorecover_decision(workflow_state, raw_text)
                    workflow_state.manager_messages.append({
                        "role": "user",
                        "content": f"[Manager autorecovery triggered] {decision['message']}",
                    })
                if decision["action"] == "complete":
                    override = _verify_plain_completion(workflow_state, decision)
                    if override is not None:
                        decision = override
                        workflow_state.rejected_completions += 1
                        workflow_state.manager_messages.append({
                            "role": "user",
                            "content": (
                                "[Completion verifier rejected]\n"
                                f"Reason: {override.get('reason', '')}\n"
                                f"Resuming with phase: {override.get('phase', '')}."
                            ),
                        })
                        print(f"\n[Manager] Completion rejected: {override.get('reason', '')}")
                    else:
                        workflow_state.active = False
                        workflow_state.awaiting_user = False
                        active_ce_mode = None
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] \033[1;32mAgent:\033[0m {decision['message'] or 'Managed Compound Engineering flow complete.'}")
                        break
                if decision["action"] == "ask_user":
                    workflow_state.awaiting_user = True
                    workflow_state.pending_question = decision["question"]
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] \033[1;32mAgent:\033[0m {decision['question']}")
                    break

                phase = decision["phase"]
                phase_succeeded = True
                try:
                    result = _run_plain_phase_from_flow(phase, decision["phase_input"], workflow_state.manager_messages)
                    workflow_state.phase_failures = 0
                except Exception as exc:
                    phase_succeeded = False
                    workflow_state.phase_failures += 1
                    result = f"[Phase {phase} exception] {exc}"
                if phase_succeeded:
                    workflow_state.completed_phases.append(phase)
                workflow_state.awaiting_user = False
                workflow_state.pending_question = ""
                workflow_state.manager_messages.append({
                    "role": "user",
                    "content": (
                        f"Phase {'completed' if phase_succeeded else 'failed'}: {phase}\n"
                        f"Completed phases: {', '.join(workflow_state.completed_phases)}\n"
                        f"Workflow artifacts:\n{_format_workflow_artifacts()}\n\n"
                        f"Phase result:\n{result[:4000]}"
                    ),
                })
            continue
        elif user_input == "/ce:learnings":
            print(tool_ce_read_learnings())
            continue
        elif user_input == "/ce:todos":
            print(tool_ce_manage_todos("list"))
            continue

        if workflow_state.active and active_ce_mode == "flow":
            workflow_state.awaiting_user = False
            workflow_state.pending_question = ""
            workflow_state.manager_messages.append({
                "role": "user",
                "content": f"User response for the managed workflow:\n{user_input}",
            })
            iterations = 0
            while workflow_state.active:
                iterations += 1
                if iterations > 80:
                    workflow_state.active = False
                    active_ce_mode = None
                    print("Managed flow stopped after too many recovery iterations.")
                    break
                active_ce_mode = "flow"
                _ensure_model_plain()
                try:
                    decision, raw_text = run_manager_turn(workflow_state.manager_messages, _get_model())
                except Exception as exc:
                    decision = None
                    raw_text = f"[manager exception] {exc}"
                if not decision:
                    workflow_state.manager_failures += 1
                    decision = _manager_autorecover_decision(workflow_state, raw_text)
                    workflow_state.manager_messages.append({
                        "role": "user",
                        "content": f"[Manager autorecovery triggered] {decision['message']}",
                    })
                if decision["action"] == "complete":
                    override = _verify_plain_completion(workflow_state, decision)
                    if override is not None:
                        decision = override
                        workflow_state.rejected_completions += 1
                        workflow_state.manager_messages.append({
                            "role": "user",
                            "content": (
                                "[Completion verifier rejected]\n"
                                f"Reason: {override.get('reason', '')}\n"
                                f"Resuming with phase: {override.get('phase', '')}."
                            ),
                        })
                        print(f"\n[Manager] Completion rejected: {override.get('reason', '')}")
                    else:
                        workflow_state.active = False
                        workflow_state.awaiting_user = False
                        active_ce_mode = None
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] \033[1;32mAgent:\033[0m {decision['message'] or 'Managed Compound Engineering flow complete.'}")
                        break
                if decision["action"] == "ask_user":
                    workflow_state.awaiting_user = True
                    workflow_state.pending_question = decision["question"]
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] \033[1;32mAgent:\033[0m {decision['question']}")
                    break
                phase = decision["phase"]
                phase_succeeded = True
                try:
                    result = _run_plain_phase_from_flow(phase, decision["phase_input"], workflow_state.manager_messages)
                    workflow_state.phase_failures = 0
                except Exception as exc:
                    phase_succeeded = False
                    workflow_state.phase_failures += 1
                    result = f"[Phase {phase} exception] {exc}"
                if phase_succeeded:
                    workflow_state.completed_phases.append(phase)
                workflow_state.awaiting_user = False
                workflow_state.pending_question = ""
                workflow_state.manager_messages.append({
                    "role": "user",
                    "content": (
                        f"Phase {'completed' if phase_succeeded else 'failed'}: {phase}\n"
                        f"Completed phases: {', '.join(workflow_state.completed_phases)}\n"
                        f"Workflow artifacts:\n{_format_workflow_artifacts()}\n\n"
                        f"Phase result:\n{result[:4000]}"
                    ),
                })
            continue

        ce_cmd = None
        ce_extra = ""
        for cmd_name in all_ce_modes():
            if user_input == f"/ce:{cmd_name}":
                ce_cmd = cmd_name
                break
            elif user_input.startswith(f"/ce:{cmd_name} "):
                ce_cmd = cmd_name
                ce_extra = user_input[len(f"/ce:{cmd_name} "):]
                break
            elif f"/ce:{cmd_name}" in user_input:
                ce_cmd = cmd_name
                import re
                ce_extra = re.sub(r'\(?\s*(?:using\s+)?/ce:' + re.escape(cmd_name) + r'\s*\)?', '', user_input).strip()
                break

        if ce_cmd:
            if ce_cmd != "setup" and not _ce_setup_warned[0]:
                try:
                    _status = ce_setup_status(WORK_DIR)
                    if not _status["ok"]:
                        for _ln in format_ce_setup_warning(_status):
                            print(f"\033[33m{_ln}\033[0m")
                        _ce_setup_warned[0] = True
                except Exception:
                    pass
            active_ce_mode = ce_cmd
            mode_prompt = get_ce_prompt(ce_cmd)
            messages.append({
                "role": "system",
                "content": f"[Entering {ce_cmd.upper()} mode]\n{mode_prompt}",
            })
            print(f"\n\033[1;35m--- Entering {ce_cmd.upper()} mode ---\033[0m")

            if ce_extra:
                user_content = ce_extra
            else:
                user_content = f"I am ready to {ce_cmd}. What should we work on?"
                if ce_cmd == "review":
                    user_content = "Review the current changes in this branch."
                elif ce_cmd == "compound":
                    user_content = "Document learnings from the recent work."
                elif ce_cmd == "ideate":
                    user_content = "Analyze this project and suggest improvements."

            messages.append({"role": "user", "content": user_content})
            print()
            _ensure_model_plain()
            response = agent_turn(messages, _get_model())
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] \033[1;32mAgent:\033[0m {response}")
            continue

        if user_input == "/ce:done":
            if active_ce_mode:
                print(f"\033[1;35m--- Exiting {active_ce_mode.upper()} mode ---\033[0m")
                active_ce_mode = None
                workflow_state = CEWorkflowState()
            else:
                print("Not in a CE mode.")
            continue

        messages.append({"role": "user", "content": user_input})
        print()
        _ensure_model_plain()
        response = agent_turn(messages, _get_model())
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] \033[1;32mAgent:\033[0m {response}")


if __name__ == "__main__":
    main()
