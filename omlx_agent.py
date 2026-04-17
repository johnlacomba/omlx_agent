#!/usr/bin/env python3
"""
oMLX Coding Agent - Local LLM agent with tool calling, GitHub sync,
and Compound Engineering workflows.

Usage: python3 ~/omlx_agent.py [--repo /path/to/repo]
"""

import curses
import json
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from datetime import datetime

API_URL = "http://localhost:8000/v1"
API_KEY = os.environ.get("OMLX_API_KEY", "omlx-80ktncu2cdui9fal")
MAX_TOKENS = 49152  # Cap output tokens to prevent KV cache OOM on 48GB systems
MAX_TOOL_ROUNDS = 40
EMERGENCY_TRIM_DROP = 20  # Messages to drop when oMLX returns "Prompt too long"
LEARNINGS_FILE = "compound-engineering.local.md"
INPUT_HISTORY_FILE = os.path.expanduser("~/.omlx/input_history.json")
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
]

WORK_DIR = os.getcwd()
SESSION_TODOS = []


def set_work_dir(path: str):
    global WORK_DIR
    WORK_DIR = os.path.abspath(path)
    os.chdir(WORK_DIR)


def resolve(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(WORK_DIR, path)


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
}


# ── Compound Engineering Workflow Prompts ────────────────────────────────────

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

Solution Context (run BEFORE documenting):
If .temp_relevantSolutions exists, read it and load the listed solution files as reference, then skip to step 3.
Otherwise, if docs/solutions/ or referenceDocs/solutions/ exists, run this pipeline:
  a. Call ce_scan_solution_headers() with no args to get the solutions directory tree. Write one repo-relative file path per line to .temp_solutionDirs.
  b. For subdirectories whose names look relevant to the recent changes (check git_log), call ce_scan_solution_headers(directory=<subdir>) to get headers. Extract tags and write each as "path: tag1, tag2" to .temp_tagList.
  c. Select tags relevant to the recent changes. Write selected tags (one per line) to .temp_relevantTags.
  d. Find solution files matching any selected tag. Write matching paths to .temp_relevantSolutions.
  e. Read the matched solution files as reference to avoid duplicating existing documentation.
If no solutions directory exists, skip the pipeline entirely.

3. Check for any review docs with ce_list_docs(doc_type="reviews") -- if a review doc exists, read it and work through its findings
4. Identify learnings:
   - What problem was solved?
   - What was the root cause?
   - What was the solution?
   - What patterns/gotchas should be remembered?
5. Write each learning using ce_write_learning (this updates compound-engineering.local.md)
6. If working from a review doc, use ce_mark_step to check off each finding as you process it
7. Check if existing learnings need updating -- use ce_read_learnings and look for entries that should be revised or merged
8. Save a solution summary using ce_save_doc(doc_type="solution", content=...)
9. IMPORTANT: After writing solution docs, always update compound-engineering.local.md with key learnings so other modes can reference them without scanning solution files

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

def api_call(messages: list, model: str) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": MAX_TOKENS,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
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
    req = urllib.request.Request(
        f"{API_URL.replace('/v1', '')}/admin/api/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            for header in resp.headers.get_all("Set-Cookie") or []:
                if "omlx_admin_session=" in header:
                    _omlx_session_cookie = header.split("omlx_admin_session=")[1].split(";")[0]
                    return _omlx_session_cookie
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


# CE modes that use the thinking model vs the work model
THINKING_MODES = {"ideate", "brainstorm", "plan"}
WORK_MODES = {"work", "review", "compound", "debug"}


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

        choice = response["choices"][0]
        msg = choice["message"]

        if msg.get("reasoning_content"):
            rc = msg["reasoning_content"]
            display = f"{rc[:300]}..." if len(rc) > 300 else rc
            _p = tui_print if _tui_instance else print
            _ts = datetime.now().strftime("%H:%M:%S")
            _p(f"[{_ts}] [thinking] {display}", C_DIM)

        if choice["finish_reason"] == "tool_calls" and msg.get("tool_calls"):
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg["tool_calls"],
            })
            if msg.get("content"):
                _ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n\033[33m[{_ts}] {msg['content']}\033[0m")
            for tc in msg["tool_calls"]:
                result = execute_tool(tc["function"]["name"], tc["function"]["arguments"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue

        # Fallback: parse <tool_call> tags from text content
        text = msg.get("content", "")
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
        if choice.get("finish_reason") == "length":
            _p = tui_print if _tui_instance else print
            _ts = datetime.now().strftime("%H:%M:%S")
            _p(f"[{_ts}] [Generation hit token limit -- auto-continuing]", C_YELLOW if _tui_instance else 0)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "[System: Your response was truncated because it hit the generation token limit. Continue EXACTLY where you left off. Do NOT repeat what you already said. If you were about to make a tool call, make it now.]"})
            continue

        messages.append({"role": "assistant", "content": text})
        return text

    return "[Max tool rounds reached]"


def print_help():
    print("""
\033[1mCommands:\033[0m
  /model              Switch model
  /clear              Clear conversation
  /status             Show git status and session info
  /quit               Exit

\033[1mCompound Engineering:\033[0m
  /ce:brainstorm      Explore ideas and requirements
  /ce:plan            Create a structured implementation plan
  /ce:work            Execute work with task tracking
  /ce:review          Multi-perspective code review
  /ce:compound        Document learnings from recent work
  /ce:debug           Systematic bug investigation
  /ce:ideate          Discover project improvements
  /ce:done            Exit current CE mode
  /ce:learnings       View all project learnings
  /ce:todos           View current todo list
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
        self.thinking_model = model_config["thinking"]
        self.thinking_concurrency = model_config.get("thinking_concurrency", 1)
        self.thinking_memory = model_config.get("thinking_memory")
        self.work_model = model_config["work"]
        self.work_concurrency = model_config.get("work_concurrency", 1)
        self.work_memory = model_config.get("work_memory")
        self._current_concurrency = None
        self._current_memory = None
        self.messages = messages
        self.active_ce_mode = active_ce_mode
        self.models = models or []

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
    def active_model(self) -> str:
        """Return the right model based on current CE mode."""
        if self.active_ce_mode in THINKING_MODES:
            return self.thinking_model
        return self.work_model

    def _ensure_concurrency(self):
        """Set oMLX concurrency and memory limit for the active model if changed."""
        is_thinking = self.active_ce_mode in THINKING_MODES
        target_conc = self.thinking_concurrency if is_thinking else self.work_concurrency
        target_mem = self.thinking_memory if is_thinking else self.work_memory
        settings = {}
        if target_conc != self._current_concurrency:
            settings["max_concurrent_requests"] = target_conc
            self._current_concurrency = target_conc
        if target_mem and target_mem != self._current_memory:
            settings["max_model_memory"] = target_mem
            self._current_memory = target_mem
        if settings:
            set_omlx_settings(settings)

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
            hints = "[Tab: Actions | ^C Cancel | ^O Todos]"
            bar = f"{status}{hints}"
            color = C_YELLOW
        else:
            mode_tag = f" [{self.active_ce_mode}]" if self.active_ce_mode else ""
            status = f" Ready{mode_tag}  Model: {self.active_model[:40]} "
            nl_hint = "Ret:newline | ^S:submit"
            hints = f"[{nl_hint} | ^O Todos | ^L Clear | ^D Quit]"
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

        # ── Action menu popup (over output area, above status bar) ────
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
                    self.stdscr.addnstr(popup_top, 0, top_border[:pw], pw, curses.color_pair(C_CYAN))
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
                            self.stdscr.addnstr(screen_row, 0, row_str[:pw], pw, curses.color_pair(color))
                            # Draw border chars in cyan
                            self.stdscr.addnstr(screen_row, 0, "|", 1, curses.color_pair(C_CYAN))
                            self.stdscr.addnstr(screen_row, pw - 1, "|", 1, curses.color_pair(C_CYAN))
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
                    self.stdscr.addnstr(popup_bottom, 0, bot_border[:pw], pw, curses.color_pair(C_CYAN))
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
                # max popup height = output_height area (rough estimate)
                max_popup_h = max(4, h - 8)
                self._open_todo_popup(w, max_popup_h)
            return

        # --- Escape: record time for Alt+Enter detection ---
        if key == 27:
            self._last_escape_time = now
            return

        # --- Enter ---
        if key in (curses.KEY_ENTER, 10, 13):
            # Alt+Enter (Esc then Enter): submit
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
            # Regular Enter: insert newline
            self._insert_newline()
            return

        # --- Arrow keys: Left / Right ---
        if key == curses.KEY_LEFT:
            if self.input_col > 0:
                self.input_col -= 1
            elif self.input_row > 0:
                # Wrap to end of previous line
                self.input_row -= 1
                self.input_col = len(self.input_lines[self.input_row])
            return
        elif key == curses.KEY_RIGHT:
            if self.input_col < len(self.input_lines[self.input_row]):
                self.input_col += 1
            elif self.input_row < len(self.input_lines) - 1:
                # Wrap to start of next line
                self.input_row += 1
                self.input_col = 0
            return
        elif key == curses.KEY_HOME or key == 1:  # Ctrl+A
            self.input_col = 0
            return
        elif key == curses.KEY_END or key == 5:  # Ctrl+E
            self.input_col = len(self.input_lines[self.input_row])
            return

        # --- Up / Down: todo popup scroll takes priority ---
        if self.todo_popup_anim_state == "shown" and self._todo_popup_scrollable:
            if key == curses.KEY_UP:
                self.todo_popup_scroll = max(0, self.todo_popup_scroll - 1)
                return
            if key == curses.KEY_DOWN:
                self.todo_popup_scroll += 1
                self.needs_redraw = True
                return

        # --- Up / Down: navigate within lines first, then history ---
        if key == curses.KEY_UP:
            if self.input_row > 0:
                # Move up within multiline input
                self.input_row -= 1
                self.input_col = min(self.input_col, len(self.input_lines[self.input_row]))
                self._at_top_edge = False
            elif self._at_top_edge:
                # Already at top and pressed Up again: browse history
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
                # Move down within multiline input
                self.input_row += 1
                self.input_col = min(self.input_col, len(self.input_lines[self.input_row]))
                self._at_bottom_edge = False
            elif self._at_bottom_edge:
                # Already at bottom and pressed Down again: browse history forward
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
                # Join with previous line
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
                # Join with next line
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
            SESSION_TODOS.clear()
            with self.output_lock:
                self.output_lines.clear()
            self.add_output("Conversation, todos, and critical context cleared.", C_GREEN)
            return
        elif text == "/status":
            lines = [
                f"Thinking model: {self.thinking_model} (concurrency: {self.thinking_concurrency})",
                f"Work model: {self.work_model} (concurrency: {self.work_concurrency})",
                f"Active model: {self.active_model}",
                f"Messages: {len(self.messages)}",
                f"Working dir: {WORK_DIR}",
            ]
            if self.active_ce_mode:
                lines.append(f"CE Mode: {self.active_ce_mode}")
            if SESSION_TODOS:
                done = sum(1 for t in SESSION_TODOS if t["done"])
                lines.append(f"Todos: {done}/{len(SESSION_TODOS)} complete")
            lines.append(tool_git_status())
            self.add_output("\n".join(lines), C_DEFAULT)
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
            else:
                self.add_output("Not in a CE mode.", C_DEFAULT)
            return

        # CE workflow commands -- match /ce:cmd at start, as prefix, or inline
        ce_cmd = None
        ce_extra = ""
        for cmd_name in CE_PROMPTS:
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
            self.active_ce_mode = ce_cmd
            mode_prompt = CE_PROMPTS[ce_cmd]
            self.messages.append({
                "role": "system",
                "content": f"[Entering {ce_cmd.upper()} mode]\n{mode_prompt}",
            })
            self.add_output(f"--- Entering {ce_cmd.upper()} mode ---", C_MAGENTA)

            if ce_extra:
                user_content = ce_extra
            else:
                defaults = {
                    "review": "Review the current changes in this branch.",
                    "compound": "Document learnings from the recent work.",
                    "ideate": "Analyze this project and suggest improvements.",
                }
                user_content = defaults.get(ce_cmd, f"I am ready to {ce_cmd}. What should we work on?")

            self.messages.append({"role": "user", "content": user_content})
            self._run_agent_async()
            return

        # Normal message
        self.messages.append({"role": "user", "content": text})
        self._run_agent_async()

    def _handle_model_switch(self):
        """Show model list; user types a number to switch thinking or work model."""
        if not self.models:
            self.add_output("No models available.", C_RED)
            return
        lines = [
            "Current models:",
            f"  Thinking: {self.thinking_model}",
            f"  Work: {self.work_model}",
            "",
            "Available Models:",
        ]
        for i, m in enumerate(self.models, 1):
            lines.append(f"  {i}. {m}")
        lines.append("")
        lines.append("Type 't<N>' to set thinking model, 'w<N>' for work model:")
        lines.append("  e.g. t1 = set thinking to model 1, w2 = set work to model 2")
        self.add_output("\n".join(lines), C_DEFAULT)
        self._pending_model_select = True

    def _try_model_select(self, text: str) -> bool:
        """If we're in model select mode, try to handle the input."""
        if not getattr(self, '_pending_model_select', False):
            return False
        self._pending_model_select = False
        text = text.strip().lower()
        if len(text) >= 2 and text[0] in ('t', 'w'):
            try:
                idx = int(text[1:]) - 1
                if 0 <= idx < len(self.models):
                    chosen = self.models[idx]
                    if text[0] == 't':
                        self.thinking_model = chosen
                        self.add_output(f"Thinking model: {chosen}", C_GREEN)
                        # Ask for concurrency
                        self.add_output("Concurrency for this model? (Enter a number, default=1):", C_DEFAULT)
                        self._pending_concurrency = "thinking"
                    else:
                        self.work_model = chosen
                        self.add_output(f"Work model: {chosen}", C_GREEN)
                        self.add_output("Concurrency for this model? (Enter a number, default=1):", C_DEFAULT)
                        self._pending_concurrency = "work"
                    return True
            except ValueError:
                pass
        self.add_output("Invalid choice. Use t<N> or w<N> (e.g. t1, w2).", C_YELLOW)
        return True

    def _try_concurrency_select(self, text: str) -> bool:
        """Handle concurrency setting after model switch."""
        pending = getattr(self, '_pending_concurrency', None)
        if not pending:
            return False
        self._pending_concurrency = None
        try:
            n = int(text.strip())
            if n < 1:
                n = 1
        except ValueError:
            n = 1
        if pending == "thinking":
            self.thinking_concurrency = n
            self.add_output(f"Thinking concurrency: {n}", C_GREEN)
        else:
            self.work_concurrency = n
            self.add_output(f"Work concurrency: {n}", C_GREEN)
        return True

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

                self._ensure_concurrency()
                self._activity = "calling API..."
                self.needs_redraw = True
                response = api_call(self.messages, self.active_model)
                if response and response.get("_context_overflow"):
                    self.messages = emergency_trim(self.messages)
                    continue
                if not response:
                    return "[API call failed]"

                choice = response["choices"][0]
                msg = choice["message"]

                # Show usage info if available
                usage = response.get("usage", {})
                if usage:
                    prompt_tok = usage.get("prompt_tokens", 0)
                    gen_tok = usage.get("completion_tokens", 0)
                    self._activity = f"tokens: {prompt_tok}p/{gen_tok}g"
                    self.needs_redraw = True

                if msg.get("reasoning_content"):
                    self._activity = "thinking..."
                    self.needs_redraw = True
                    rc = msg["reasoning_content"]
                    display = f"{rc[:300]}..." if len(rc) > 300 else rc
                    _ts = datetime.now().strftime("%H:%M:%S")
                    tui_print(f"[{_ts}] [thinking] {display}", C_DIM)

                # Structured tool calls
                if choice["finish_reason"] == "tool_calls" and msg.get("tool_calls"):
                    self.messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": msg["tool_calls"],
                    })
                    if msg.get("content"):
                        _ts = datetime.now().strftime("%H:%M:%S")
                        tui_print(f"[{_ts}] {msg['content']}", C_YELLOW)
                    for tc in msg["tool_calls"]:
                        result = execute_tool(tc["function"]["name"], tc["function"]["arguments"])
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })
                    continue

                # Fallback: parse <tool_call>/<tool_use> tags from text content
                text = msg.get("content", "")
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
                if choice.get("finish_reason") == "length":
                    _ts = datetime.now().strftime("%H:%M:%S")
                    tui_print(f"[{_ts}] [Generation hit token limit -- auto-continuing]", C_YELLOW)
                    self.messages.append({"role": "assistant", "content": text})
                    self.messages.append({"role": "user", "content": "[System: Your response was truncated because it hit the generation token limit. Continue EXACTLY where you left off. Do NOT repeat what you already said. If you were about to make a tool call, make it now.]"})
                    continue

                # Final text response
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
        self.add_output(f"Thinking: {self.thinking_model} (concurrency: {self.thinking_concurrency})", C_GREEN)
        self.add_output(f"Work: {self.work_model} (concurrency: {self.work_concurrency})", C_GREEN)
        self.add_output("Type /help for commands. Start typing to chat.", C_DIM)
        self.add_output("-" * 60, C_DIM)

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

                # Check model select or concurrency select mode first
                if key_int in (curses.KEY_ENTER, 10, 13):
                    if getattr(self, '_pending_concurrency', None):
                        text = self.get_input_text().strip()
                        self.clear_input()
                        self._try_concurrency_select(text)
                        continue
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
  /model              Switch thinking/work model (t<N> or w<N>)
  /clear              Clear conversation
  /status             Show git status and session info
  /quit               Exit (also Ctrl+D)

Compound Engineering:
  /ce:brainstorm      Explore ideas and requirements
  /ce:plan            Create a structured implementation plan
  /ce:work            Execute work with task tracking
  /ce:review          Multi-perspective code review
  /ce:compound        Document learnings from recent work
  /ce:debug           Systematic bug investigation
  /ce:ideate          Discover project improvements
  /ce:done            Exit current CE mode
  /ce:learnings       View all project learnings
  /ce:todos           View current todo list

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
  Ctrl+U/Ctrl+K       Kill line before/after cursor
  PageUp/PageDn       Scroll output
  Ctrl+L              Clear output"""


def _estimate_model_defaults(model_name: str, role: str) -> tuple:
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
    # Concurrency: larger models get fewer concurrent requests
    if role.lower() == "thinking":
        conc = 1  # thinking = single focused request
    elif weight_gb >= 20:
        conc = 1  # large model, keep it simple
    else:
        conc = 2  # small model can handle 2

    return conc, f"{mem_gb}GB"


def _select_model_with_concurrency(models: list, role: str) -> tuple:
    """Prompt user to select a model and concurrency for a role. Returns (model_name, concurrency, memory)."""
    print(f"\n\033[1mSelect {role} model:\033[0m")
    for i, m in enumerate(models, 1):
        print(f"  {i}. {m}")
    model = None
    while not model:
        try:
            choice = input(f"\n{role} model [1-{len(models)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                model = models[idx]
        except (ValueError, EOFError):
            pass
        if not model:
            print("Invalid choice, try again.")

    default_conc, default_mem = _estimate_model_defaults(model, role)

    concurrency = default_conc
    try:
        c = input(f"Max concurrent requests for {model[:40]}? [{default_conc}]: ").strip()
        if c:
            concurrency = max(1, int(c))
    except (ValueError, EOFError):
        pass

    memory = default_mem
    try:
        m = input(f"Max model memory for {model[:40]}? [{default_mem}]: ").strip()
        if m:
            memory = m
    except (ValueError, EOFError):
        pass

    print(f"  -> {model} (concurrency: {concurrency}, memory: {memory})")
    return model, concurrency, memory


def main():
    import argparse
    parser = argparse.ArgumentParser(description="oMLX Coding Agent")
    parser.add_argument("--repo", default=os.getcwd(), help="Path to git repo (default: cwd)")
    parser.add_argument("--model", help="Model name (skip dual selection, use for both)")
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
        model_config = {
            "thinking": args.model, "thinking_concurrency": 1,
            "work": args.model, "work_concurrency": 1,
        }
    else:
        print("\n\033[1m--- Model Configuration ---\033[0m")
        print("Thinking model: used for /ce:ideate, /ce:brainstorm, /ce:plan")
        print("Work model: used for /ce:work, /ce:review, /ce:compound, /ce:debug, and general chat")
        t_model, t_conc, t_mem = _select_model_with_concurrency(models, "Thinking")
        w_model, w_conc, w_mem = _select_model_with_concurrency(models, "Work")
        model_config = {
            "thinking": t_model, "thinking_concurrency": t_conc, "thinking_memory": t_mem,
            "work": w_model, "work_concurrency": w_conc, "work_memory": w_mem,
        }
    # Set initial settings for the work model (default at startup)
    startup_settings = {"max_concurrent_requests": model_config["work_concurrency"]}
    if model_config.get("work_memory"):
        startup_settings["max_model_memory"] = model_config["work_memory"]
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
    print(f"\nThinking: \033[1;32m{model_config['thinking']}\033[0m")
    print(f"Work: \033[1;32m{model_config['work']}\033[0m")
    print("\nType /help for commands. Start typing to chat.")
    print("-" * 60)

    active_ce_mode = None

    def _get_model():
        if active_ce_mode in THINKING_MODES:
            return model_config["thinking"]
        return model_config["work"]

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
            SESSION_TODOS.clear()
            print("Conversation, todos, and critical context cleared.")
            continue
        elif user_input == "/status":
            print(f"Thinking model: {model_config['thinking']}")
            print(f"Work model: {model_config['work']}")
            print(f"Active model: {_get_model()}")
            print(f"Messages: {len(messages)}")
            print(f"Working dir: {WORK_DIR}")
            if active_ce_mode:
                print(f"CE Mode: {active_ce_mode}")
            if SESSION_TODOS:
                done = sum(1 for t in SESSION_TODOS if t["done"])
                print(f"Todos: {done}/{len(SESSION_TODOS)} complete")
            print(tool_git_status())
            continue
        elif user_input == "/ce:learnings":
            print(tool_ce_read_learnings())
            continue
        elif user_input == "/ce:todos":
            print(tool_ce_manage_todos("list"))
            continue

        ce_cmd = None
        ce_extra = ""
        for cmd_name in CE_PROMPTS:
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
            active_ce_mode = ce_cmd
            mode_prompt = CE_PROMPTS[ce_cmd]
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
            response = agent_turn(messages, _get_model())
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] \033[1;32mAgent:\033[0m {response}")
            continue

        if user_input == "/ce:done":
            if active_ce_mode:
                print(f"\033[1;35m--- Exiting {active_ce_mode.upper()} mode ---\033[0m")
                active_ce_mode = None
            else:
                print("Not in a CE mode.")
            continue

        messages.append({"role": "user", "content": user_input})
        print()
        response = agent_turn(messages, _get_model())
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] \033[1;32mAgent:\033[0m {response}")


if __name__ == "__main__":
    main()
