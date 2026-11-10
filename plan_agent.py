#!/usr/bin/env python3
"""
plan_agent.py — read-only planning CLI. Produces a plan, does not edit files.

Examples:
    python plan_agent.py "G:\\Personal\\Dev\\Web\\Drone_Website" ^
        "How should I add a newsletter signup to the homepage?"

    python plan_agent.py . "Rename UserService to AccountService" --save
    python plan_agent.py . "..." --max-turns 8 --ensemble plan-workflow

The plan is printed to stdout. If --save is passed, also writes a Markdown
artifact under SWE/plans/ that codebase_agent.py can ingest via --plan-file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from agents_md import load_rules, rules_for

HERE = Path(__file__).resolve().parent

try:
    from agent import AGENT_DIR
except ImportError:
    AGENT_DIR = HERE / ".llm-orc"

PLANS_DIR = HERE / "plans"
PLANS_DIR.mkdir(exist_ok=True)

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", "out", "bin", "obj",
    ".llm-orc", ".idea", ".vscode", "coverage", "htmlcov",
}
IGNORE_EXTS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".lock", ".min.js", ".min.css", ".map", ".pdf", ".zip", ".tar",
    ".gz", ".7z", ".mp3", ".mp4", ".mov", ".wav",
}
MAX_FILE_BYTES  = 40_000
MAX_TOTAL_BYTES = 160_000
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
READ_RE = re.compile(r'<<<READ\s+path="([^"]+)"\s*>>>')
LIST_RE = re.compile(r'<<<LIST\s+dir="([^"]+)"\s*>>>')


# ---------------------------------------------------------------------------
# Discovery (mirrors codebase_agent.py)
# ---------------------------------------------------------------------------

def discover_files(root: Path, max_files=500):
    root = root.resolve()
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and not d.startswith(".")]
        for name in filenames:
            if len(out) >= max_files:
                return sorted(out)
            p = Path(dirpath) / name
            if p.suffix.lower() in IGNORE_EXTS:
                continue
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


def read_capped(path: Path, cap=MAX_FILE_BYTES):
    try:
        raw = path.read_bytes()
    except OSError as e:
        return None, f"<read error: {e}>"
    trunc = len(raw) > cap
    if trunc:
        raw = raw[:cap]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "<binary or non-UTF8 file>"
    if trunc:
        text += f"\n… [truncated at {cap} bytes]"
    return text, None


# ---------------------------------------------------------------------------
# Read/LIST protocol
# ---------------------------------------------------------------------------

def fetch_requested(markers, root: Path, budget=MAX_TOTAL_BYTES):
    """Fetch content for READ/LIST markers. Returns (text_block, remaining_budget)."""
    chunks = []
    used = 0

    for rel in markers["reads"]:
        if used >= budget:
            chunks.append(f"\n=== {rel} ===\n<skipped: budget exhausted>")
            continue
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            chunks.append(f"\n=== {rel} ===\n<path escapes repo root; refused>")
            continue
        if not p.exists() or not p.is_file():
            chunks.append(f"\n=== {rel} ===\n<file not found>")
            continue
        text, err = read_capped(p)
        if err:
            chunks.append(f"\n=== {rel} ===\n{err}")
            continue
        block = f"\n=== {rel} ===\n{text}\n=== END {rel} ===\n"
        if used + len(block) > budget:
            chunks.append(f"\n=== {rel} ===\n<skipped: budget exhausted>")
            continue
        chunks.append(block)
        used += len(block)

    for rel in markers["lists"]:
        p = (root / rel).resolve() if rel not in ("", ".") else root
        try:
            p.relative_to(root)
        except ValueError:
            chunks.append(f"\n=== LIST {rel} ===\n<path escapes repo root; refused>")
            continue
        if not p.is_dir():
            chunks.append(f"\n=== LIST {rel} ===\n<not a directory>")
            continue
        entries = sorted(
            f"{'d' if c.is_dir() else 'f'} {c.name}"
            for c in p.iterdir()
            if not c.name.startswith(".") and c.name not in IGNORE_DIRS
        )
        chunks.append(f"\n=== LIST {rel or '.'} ===\n" + "\n".join(entries)
                      + f"\n=== END LIST {rel or '.'} ===")

    return "\n".join(chunks), budget - used


def parse_markers(text: str):
    return {
        "reads": [m.strip() for m in READ_RE.findall(text)],
        "lists": [m.strip() for m in LIST_RE.findall(text)],
    }


# ---------------------------------------------------------------------------
# Orchestrator invocation
# ---------------------------------------------------------------------------

def invoke_planner(full_prompt, ensemble):
    venv = Path("H:/AI/Agents/SWE/.venv/Scripts/llm-orc.exe")
    exe = str(venv) if venv.exists() else shutil.which("llm-orc")
    if not exe:
        sys.exit("❌ llm-orc not found")
    cmd = [exe, "invoke", ensemble, "--config-dir", str(AGENT_DIR)]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=env, encoding="utf-8",
    )
    out, _ = proc.communicate(input=full_prompt)
    if proc.returncode != 0:
        print(out[-3000:])
        sys.exit(f"❌ Orchestrator exited with {proc.returncode}")
    # Strip ANSI + perf summary if present
    out = ANSI_RE.sub("", out)
    if "📊 Performance Summary" in out:
        out = out.split("📊 Performance Summary")[0]
    return out.strip()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_initial_prompt(root, instruction, tree, raw_rules):
    planner_rules = rules_for(raw_rules, "planner")
    listing = "\n".join(tree[:400])
    if len(tree) > 400:
        listing += f"\n… [{len(tree) - 400} more files omitted]"

    return f"""USER INSTRUCTION:
{instruction}
{planner_rules}
REPOSITORY: {root}
FILE TREE (relative paths — you may request specific files with <<<READ>>>):
{listing}

Analyze the instruction and produce your first response.
"""


def build_followup_prompt(requested_content):
    return (
        "The runtime fetched the files you requested. Continue your analysis.\n\n"
        + requested_content
    )


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

MAX_PLAN_ARTIFACT = 20_000    # cap on the plan body written to disk


def save_plan(root, instruction, history, plan_md):
    slug = re.sub(r"[^a-z0-9]+", "-", instruction.lower())[:40].strip("-") or "plan"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PLANS_DIR / f"{ts}_{slug}.md"

    body = plan_md
    if len(body) > MAX_PLAN_ARTIFACT:
        print(f"⚠️  Plan body is {len(body)}B — capping artifact at {MAX_PLAN_ARTIFACT}B.",
              file=sys.stderr)
        body = body[:MAX_PLAN_ARTIFACT] + "\n\n… [plan body truncated]"

    conv = "\n\n".join(f"### {m['role'].upper()}\n{m['content']}"
                       for m in history if m["role"] != "system")
    out.write_text(
        f"# Plan: {instruction}\n\n"
        f"**Repo:** `{root}`\n"
        f"**Generated:** {datetime.now().isoformat()}\n\n"
        f"---\n\n{body}\n\n---\n\n"
        f"<details>\n<summary>Conversation history</summary>\n\n"
        f"{conv}\n\n</details>\n",
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Read-only planning agent")
    ap.add_argument("repo")
    ap.add_argument("instruction")
    ap.add_argument("--ensemble", default="plan-workflow")
    ap.add_argument("--max-turns", type=int, default=5,
                    help="max planner<->read round trips (default 5)")
    ap.add_argument("--save", action="store_true",
                    help="save plan artifact under SWE/plans/")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-turn progress to stderr")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not root.is_dir():
        sys.exit(f"❌ Not a directory: {root}")

    raw_rules, rules_src = load_rules(root, HERE)
    if rules_src and not args.quiet:
        print(f"📄 Rules: {rules_src}", file=sys.stderr)

    tree = discover_files(root)
    if not tree:
        sys.exit("❌ No files found.")

    history = [
        {"role": "system",
         "content": "You are a planning agent in read-only mode."},
        {"role": "user",
         "content": build_initial_prompt(root, args.instruction, tree, raw_rules)},
    ]

    last_response = ""
    for turn in range(1, args.max_turns + 1):
        if not args.quiet:
            print(f"\n⏳ Turn {turn} …", file=sys.stderr)

        prompt = "\n\n".join(
            f"### {m['role'].upper()}\n{m['content']}" for m in history
        )
        response = invoke_planner(prompt, args.ensemble)
        history.append({"role": "assistant", "content": response})
        last_response = response

        markers = parse_markers(response)
        if not markers["reads"] and not markers["lists"]:
            break

        if not args.quiet:
            req = markers["reads"] + markers["lists"]
            print(f"   → reads: {req}", file=sys.stderr)

        content, _ = fetch_requested(markers, root)
        history.append({"role": "user", "content": build_followup_prompt(content)})
    else:
        if not args.quiet:
            print(f"⚠️  Hit --max-turns ({args.max_turns}); returning last response.",
                  file=sys.stderr)

    print(last_response)

    if args.save:
        path = save_plan(root, args.instruction, history, last_response)
        print(f"\n💾 Saved: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()