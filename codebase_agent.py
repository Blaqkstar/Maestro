#!/usr/bin/env python3
"""
codebase_agent.py — multi-file codebase editing with optional approved-plan ingestion.

Examples:
    python codebase_agent.py . "Add retry logic to every HTTP client"
    python codebase_agent.py . "Fix the navbar collapse CSS" --dry-run
    python codebase_agent.py . "Rename UserService to AccountService" --files src/user.py src/api.py
    python codebase_agent.py . "..." --mode grep        # skip the LLM planner
    python codebase_agent.py . "..." --list-only        # just show selection

    # Execute a saved plan from plan_agent.py / chat_agent.py:
    python codebase_agent.py . "Implement the newsletter signup" ^
        --plan-file plans/20261109_143012_newsletter-signup.md --dry-run
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import requests

from agents_md import load_rules, rules_for

HERE = Path(__file__).resolve().parent

try:
    from agent import AGENT_DIR  # noqa: F401
except ImportError:
    AGENT_DIR = HERE / ".llm-orc"

LLM_URL       = "http://localhost:1234/v1/chat/completions"
PLANNER_MODEL = "qwen2.5-3b-instruct"

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", "out", "bin", "obj",
    ".llm-orc", ".idea", ".vscode", "coverage", "htmlcov",
}
IGNORE_EXTS = {
    # Compiled / binary
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".bin",
    # Raster + vector images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
    ".webp", ".avif", ".heic", ".heif", ".tiff", ".tif",
    ".psd", ".ai", ".eps", ".indd", ".sketch", ".fig",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    # Packaged / minified
    ".lock", ".min.js", ".min.css", ".map", ".pdf", ".zip", ".tar",
    ".gz", ".7z", ".rar", ".mp3", ".mp4", ".mov", ".wav", ".webm",
    # Backup / temp / patch files — never auto-edit these
    ".bak", ".backup", ".old", ".orig", ".tmp", ".swp", ".swo",
    ".log", ".diff", ".patch", ".rej", ".save",
}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".mdx", ".txt",
    ".sh", ".bash", ".bat", ".ps1",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".lua", ".swift", ".scala",
    ".sql", ".graphql", ".proto",
}

MAX_FILE_BYTES     = 40_000
MAX_TOTAL_BYTES    = 200_000
MAX_PLANNER_LIST   = 400
MAX_PLAN_BYTES     = 8_000
MAX_PLANNER_INJECT = 4_000

STOPWORDS = set("""the a an and or but if then else for to of in on at by with
from is are was were be been being do does did this that these those it its
as into than such can could should would will shall may might must not no
all any some each every other more most few also just only very
implement add create build make fix update change modify remove delete
insert append prepend replace rename move copy set get put use using
new old current existing start stop begin end via per
""".split())


# ---------------------------------------------------------------------------
# 1. Discovery
# ---------------------------------------------------------------------------

def _load_gitignore(root: Path):
    gi = root / ".gitignore"
    pats = []
    if gi.exists():
        for line in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            pats.append((line.rstrip("/"), neg))
    return pats


def _ignored(rel: str, pats) -> bool:
    ignored = False
    parts = rel.split("/")
    for pat, neg in pats:
        hit = (fnmatch(rel, pat) or fnmatch(rel, pat + "/*")
               or any(fnmatch(p, pat) for p in parts))
        if hit:
            ignored = not neg
    return ignored


def discover_files(root: Path, includes=None, excludes=None):
    root = root.resolve()
    pats = _load_gitignore(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and not d.startswith(".")]
        for name in filenames:
            p   = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.suffix.lower() in IGNORE_EXTS:
                continue
            if _ignored(rel, pats):
                continue
            if includes and not any(fnmatch(rel, g) for g in includes):
                continue
            if excludes and any(fnmatch(rel, g) for g in excludes):
                continue
            out.append(rel)
    return sorted(out)


# ---------------------------------------------------------------------------
# 2. Selection — plan-file extraction, planner (LLM), keyword fallback
# ---------------------------------------------------------------------------

def extract_keywords(text: str):
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    return {w.lower() for w in words if w.lower() not in STOPWORDS}


_SRC_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".scala", ".lua",
}
_CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
_DOC_EXTS    = {".md", ".mdx", ".rst", ".txt"}


def _file_weight(rel: str) -> float:
    ext = Path(rel).suffix.lower()
    if ext in _SRC_EXTS:    return 1.5
    if ext in _CONFIG_EXTS: return 1.0
    if ext in _DOC_EXTS:    return 0.3
    return 1.0


def score_files(root: Path, rel_paths, keywords):
    scored = []
    for rel in rel_paths:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = text.lower()
        raw_score = sum(1 for kw in keywords if kw in low)
        if any(kw in rel.lower() for kw in keywords):
            raw_score += 5
        if not raw_score:
            continue
        scored.append((rel, raw_score * _file_weight(rel)))
    scored.sort(key=lambda x: -x[1])
    return scored


def select_from_plan(plan_text, all_files):
    """Extract mentioned filenames from an approved plan."""
    if not plan_text:
        return None

    full_matches = [rel for rel in all_files if rel in plan_text]

    seen = set(full_matches)
    base_matches = []
    for rel in all_files:
        if rel in seen:
            continue
        base = Path(rel).name
        if len(base) >= 5 and base in plan_text:
            base_matches.append(rel)

    mentioned = full_matches + base_matches
    if not mentioned:
        return None

    edit, context = [], []
    for rel in mentioned:
        if Path(rel).suffix.lower() in _SRC_EXTS:
            edit.append(rel)
        else:
            context.append(rel)

    if not edit:
        edit = mentioned[:3]
        context = mentioned[3:]

    return {"edit": edit[:8], "context": context[:8]}


PLANNER_SYSTEM = """You are a senior engineer planning a code change.
{rules}

Given a user instruction and a list of files, decide which files to EDIT and
which files to READ FOR CONTEXT.

Reply with valid JSON ONLY, no markdown, no explanation:
{{"edit": ["path1", "path2"], "context": ["path3"], "reasoning": "one sentence"}}

Rules:
- Paths must exactly match entries from the file list.
- "edit" should be as small as possible.
- "context" = files that inform the change but shouldn't be modified.
- If unsure, prefer fewer files.
- Respect any DEPRECATED FEATURES listed in the rules — never select files
  whose only plausible edit would re-add them."""


def plan_with_llm(instruction, rel_paths, raw_rules="", plan_text=""):
    scout_rules = rules_for(raw_rules, "scout", header=False)
    system      = PLANNER_SYSTEM.format(rules=scout_rules)

    listing = rel_paths[:MAX_PLANNER_LIST]
    listing_str = "\n".join(listing)

    plan_section = ""
    if plan_text:
        excerpt = plan_text[:MAX_PLANNER_INJECT]
        if len(plan_text) > MAX_PLANNER_INJECT:
            excerpt += "\n… [plan excerpt truncated]"
        plan_section = (f"\n\nAPPROVED PLAN (excerpt — for file-selection hints):\n"
                        f"{excerpt}\n")

    user_msg = (
        f"USER INSTRUCTION:\n{instruction}\n"
        f"{plan_section}\n"
        f"FILES ({len(rel_paths)} total, showing {len(listing)}):\n"
        f"{listing_str}"
    )

    if len(user_msg) > 12000:
        print(f"⚠️  Planner prompt is {len(user_msg)} chars (>12000). "
              f"Aborting planner call to avoid a 400 error.")
        return None

    try:
        r = requests.post(LLM_URL, json={
            "model": PLANNER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 600,
        }, timeout=60)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        m   = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        edit = [p for p in data.get("edit",    []) if p in rel_paths]
        ctx  = [p for p in data.get("context", []) if p in rel_paths and p not in edit]
        if not edit:
            return None
        print(f"   planner: {len(edit)} edit, {len(ctx)} context")
        return edit, ctx
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        print(f"⚠️  Planner HTTP {e.response.status_code}: {body}")
        print(f"⚠️  Prompt was {len(user_msg)} chars.")
        return None
    except Exception as e:
        print(f"⚠️  Planner failed ({e}) — falling back to keyword search")
        return None


def select_files(instruction, root, all_files, mode, explicit_files,
                 raw_rules="", plan_text=""):
    if mode == "explicit":
        if not explicit_files:
            sys.exit("❌ --mode explicit requires --files")
        edit = list(explicit_files)

        # Context = same-directory source files only. Docs, configs, and
        # binary assets don't help the coder understand the target.
        edit_dirs = {str(Path(f).parent) for f in edit}
        edit_set  = set(edit)
        ctx = [
            f for f in all_files
            if f not in edit_set
            and str(Path(f).parent) in edit_dirs
            and Path(f).suffix.lower() in _SRC_EXTS
        ][:8]
        return edit, ctx

    # Prefer extracting from the plan over re-planning with the 3B model
    if plan_text and not explicit_files:
        from_plan = select_from_plan(plan_text, all_files)
        if from_plan:
            print(f"   plan → {len(from_plan['edit'])} edit, "
                  f"{len(from_plan['context'])} context")
            return from_plan["edit"], from_plan["context"]
        print("⚠️  Plan mentions no files that exist in the repo; "
              "falling back to planner.")

    if mode == "grep":
        ranked = score_files(root, all_files, extract_keywords(instruction))
        if not ranked:
            sys.exit("❌ No files matched the instruction keywords.")
        top = ranked[:5]
        if not any(Path(r).suffix.lower() in _SRC_EXTS for r, _ in top):
            print("❌ Keyword fallback picked no source files.")
            print(f"   Top candidates: {[r for r, _ in top]}")
            print("   The instruction describes a concept, not a symbol.")
            print("   Use --files <path...> to specify targets explicitly.")
            sys.exit(1)
        return [r for r, _ in top], [r for r, _ in ranked[5:12]]

    plan = plan_with_llm(instruction, all_files, raw_rules, plan_text)
    if plan:
        return plan

    ranked = score_files(root, all_files, extract_keywords(instruction))
    if not ranked:
        sys.exit("❌ Planner failed and no keyword matches found.")
    return [r for r, _ in ranked[:5]], [r for r, _ in ranked[5:12]]


# ---------------------------------------------------------------------------
# 3. Prompt assembly
# ---------------------------------------------------------------------------

def read_capped(path: Path, cap=MAX_FILE_BYTES):
    try:
        raw = path.read_bytes()
    except OSError:
        return None, False
    trunc = len(raw) > cap
    if trunc:
        raw = raw[:cap]
    try:
        return raw.decode("utf-8"), trunc
    except UnicodeDecodeError:
        return None, False


def _render_group(root, rels, budget, header):
    chunks = [f"=== {header} ==="]
    used   = len(chunks[0])
    for rel in rels:
        text, trunc = read_capped(root / rel)
        if text is None:
            continue
        block = f"\n=== {rel} ===\n{text}"
        if trunc:
            block += f"\n… [truncated at {MAX_FILE_BYTES} bytes]"
        block += f"\n=== END {rel} ===\n"
        if used + len(block) > budget:
            chunks.append(f"\n… [skipped {rel}: context budget reached]")
            continue
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks), used


def _load_plan_body(plan_file) -> str:
    """Read the plan artifact and return its full body (uncapped)."""
    if not plan_file:
        return ""
    try:
        raw = Path(plan_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"⚠️  Could not read plan file ({plan_file}): {e}")
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "<details>" in text:
        text = text.split("<details>")[0]
    parts = text.split("\n---\n", 2)
    if len(parts) >= 3:
        text = parts[1]
    elif len(parts) == 2:
        text = parts[1]
    return text.strip()


def _format_plan_for_prompt(body, max_bytes=MAX_PLAN_BYTES) -> str:
    """Cap and wrap the plan body for prompt injection."""
    if not body:
        return ""
    capped = body
    if len(body) > max_bytes:
        print(f"⚠️  Plan body is {len(body)}B; capping to {max_bytes}B for prompt.")
        capped = body[:max_bytes] + f"\n… [plan truncated at {max_bytes} bytes]"
    return f"=== APPROVED PLAN ===\n{capped}\n=== END PLAN ==="


def build_prompt(root, instruction, raw_rules, edit, ctx, plan_section="",
                 ensemble="code-workflow"):
    budget = MAX_TOTAL_BYTES
    edit_section, edit_used = _render_group(root, edit, budget, "FILES TO EDIT")
    budget -= edit_used
    ctx_section, _ = _render_group(root, ctx, budget, "FILES FOR CONTEXT (read-only)")

    coder_rules = rules_for(raw_rules, "coder")
    plan_block  = f"\n{plan_section}\n" if plan_section else ""

    if ensemble == "search-replace-workflow":
        output_format = """OUTPUT FORMAT — SEARCH/REPLACE patches:

For each file you want to modify, emit one <<<PATCH>>> block:

<<<PATCH path="relative/path/to/file.ext">>>
<exact existing lines copied character-for-character from the file>
<<<END PATCH>>>

Rules:
- SEARCH must be copied EXACTLY from the file. Same indentation, same case.
- Include enough context that SEARCH appears EXACTLY ONCE in the file.
- REPLACE is what SEARCH becomes — includes the changed lines plus the
  anchor lines that must stay.
- Only edit paths under "FILES TO EDIT".
- Do NOT wrap in markdown fences.
- Do NOT re-emit the whole file. Only the lines that change.
- Emit ONLY <<<PATCH>>> blocks. No commentary.
"""
    else:
        output_format = """OUTPUT FORMAT:
For every file you want to modify, emit exactly one block:

<<<EDIT path="relative/path/to/file.ext">>>
<the COMPLETE new contents of the file>
<<<END EDIT>>>

Rules:
- Path must match a path from "FILES TO EDIT".
- Emit the full file content (not a diff, not a fragment).
- Do not wrap in markdown fences.
- If a file needs no change, do NOT emit a block for it.
"""

    return f"""USER INSTRUCTION:
{instruction}
{plan_block}{coder_rules}
{edit_section}

{ctx_section}

{output_format}
"""


# ---------------------------------------------------------------------------
# 4. Orchestrator invocation
# ---------------------------------------------------------------------------

ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def invoke_orchestrator(full_prompt, ensemble_name):
    venv = Path("H:/AI/Agents/SWE/.venv/Scripts/llm-orc.exe")
    exe  = str(venv) if venv.exists() else shutil.which("llm-orc")
    if not exe:
        sys.exit("❌ llm-orc not found")
    cmd = [exe, "invoke", ensemble_name, "--config-dir", str(AGENT_DIR)]
    env = os.environ.copy()
    env["PYTHONUTF8"]       = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    print(f"⏳ Running {ensemble_name} …")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=env, encoding="utf-8",
    )
    out, _ = proc.communicate(input=full_prompt)
    if proc.returncode != 0:
        print(out[-3000:])
        sys.exit(f"❌ Orchestrator exited with {proc.returncode}")
    return out


# ---------------------------------------------------------------------------
# 5. Parse + validate + apply
# ---------------------------------------------------------------------------

EDIT_RE = re.compile(r'<<<EDIT path="([^"]+)">>>\s*\n(.*?)\n<<<END EDIT>>>', re.DOTALL)


def _clean_orchestrator_output(raw: str) -> str:
    """Extract the final agent's response from llm-orc's stdout noise."""
    if "📊 Performance Summary" in raw:
        raw = raw.split("📊 Performance Summary")[0]
    blocks = re.split(r'✓ [a-zA-Z_]+ \([^)]+\)', raw)
    final = blocks[-1].strip() if blocks else raw.strip()
    final = re.sub(r'^-{3,}$', '', final, flags=re.MULTILINE).strip()
    if final.startswith("```"):
        lines = final.split("\n")
        if lines[0].startswith("```") and lines[-1].strip() == "```":
            final = "\n".join(lines[1:-1]).strip()
    return final


def parse_edits(raw, fallback_file=None):
    raw = ANSI.sub("", raw)
    edits = {}
    for path, body in EDIT_RE.findall(raw):
        edits[path.strip()] = body.rstrip("\n") + "\n"

    if not edits and fallback_file:
        cleaned = _clean_orchestrator_output(raw)
        if cleaned and len(cleaned) > 20:
            print(f"ℹ️  No <<<EDIT>>> markers in orchestrator output.")
            print(f"   Treating full output as replacement for: {fallback_file}")
            edits[fallback_file] = cleaned if cleaned.endswith("\n") else cleaned + "\n"

    return edits


def validate_edit(root: Path, path: str, content: str):
    p = (root / path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return False, "path escapes repository root"

    if not p.exists():
        if len(content) < 10:
            return False, "new file too short"
        return True, "ok"

    old = p.read_text(encoding="utf-8", errors="ignore")
    old_norm = old.replace("\r\n", "\n")
    new_norm = content.replace("\r\n", "\n")

    if len(old) > 800 and len(content) < len(old) * 0.5:
        return False, (f"output ({len(content)}B) < 50% of original "
                       f"({len(old)}B) — likely truncated")

    if p.suffix == ".html" and not content.rstrip().endswith("</html>"):
        return False, "HTML missing closing </html> — truncated"

    # Reformatting guard: catches whole-file re-emits that insert blank lines
    # or re-indent unchanged regions.
    if len(old_norm) > 500 and len(new_norm) > 500:
        matcher = difflib.SequenceMatcher(None, old_norm.splitlines(),
                                          new_norm.splitlines(), autojunk=False)
        ratio = matcher.ratio()
        size_ratio = len(new_norm) / max(len(old_norm), 1)

        if ratio < 0.75 and (size_ratio > 1.20 or size_ratio < 0.80):
            return False, (
                f"reformatting detected: only {ratio:.0%} of lines preserved, "
                f"size {size_ratio:.2f}x original. Refusing to write."
            )

    return True, "ok"


def apply_or_preview(root: Path, edits: dict, dry_run: bool):
    plan, errors = [], []
    for path, content in edits.items():
        ok, msg = validate_edit(root, path, content)
        (plan if ok else errors).append(
            (path, content) if ok else f"  ✗ {path}: {msg}"
        )

    if errors:
        print("\n❌ Aborting — validation failed:\n" + "\n".join(errors))
        sys.exit(1)

    if dry_run:
        print(f"\n🧪 DRY RUN — {len(plan)} file(s) would change:\n")
        for path, new in plan:
            p   = root / path
            old = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            ))
            print()
        return

    print(f"\n✍️  Writing {len(plan)} file(s) …")
    for path, new in plan:
        p = root / path
        if p.exists():
            old = p.read_text(encoding="utf-8", errors="ignore")
            if "\r\n" in old:
                new = new.replace("\n", "\r\n")
            bak = p.with_suffix(p.suffix + ".bak")
            if bak.exists():
                bak.unlink()
            p.rename(bak)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new, encoding="utf-8")
        print(f"  ✓ {path}")


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Multi-file codebase agent")
    ap.add_argument("repo",        help="repository root ('.' for cwd)")
    ap.add_argument("instruction")
    ap.add_argument("--mode", choices=["plan", "grep", "explicit"], default="plan")
    ap.add_argument("--files", nargs="+", help="explicit file list")
    ap.add_argument("--include", nargs="+", help="glob include filters")
    ap.add_argument("--exclude", nargs="+", help="glob exclude filters")
    ap.add_argument("--ensemble", default="search-replace-workflow",
                    help="llm-orc ensemble (default: search-replace-workflow)")
    ap.add_argument("--plan-file", default=None,
                    help="path to an approved plan .md (from plan_agent.py)")
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not root.is_dir():
        sys.exit(f"❌ Not a directory: {root}")

    # ── 1. Load plan (if given) ──
    plan_body = ""
    plan_section = ""
    if args.plan_file:
        plan_path = Path(args.plan_file)
        if not plan_path.is_absolute():
            plan_path = Path.cwd() / plan_path
        plan_path = plan_path.resolve()
        if not plan_path.is_file():
            sys.exit(f"❌ Plan file not found: {plan_path}")
        print(f"📋 Plan: {plan_path}")
        plan_body = _load_plan_body(str(plan_path))
        if not plan_body:
            print("⚠️  Plan body is empty after stripping — continuing without it.")
        else:
            plan_section = _format_plan_for_prompt(plan_body)
            print(f"   (plan: {len(plan_body)}B body, "
                  f"{len(plan_section)}B prompt section)")

    # ── 2. Discover files ──
    print(f"📂 Repo: {root}")
    all_files = discover_files(root, args.include, args.exclude)
    print(f"📁 {len(all_files)} candidate files")
    if not all_files:
        sys.exit("❌ No files found after filtering.")

    # ── 3. Load rules ──
    raw_rules, rules_src = load_rules(root, HERE)
    if rules_src:
        print(f"📄 Rules: {rules_src}")

    # ── 4. Choose files (edit + context) ──
    if args.files:
        args.mode = "explicit"

    edit, ctx = select_files(args.instruction, root, all_files,
                             args.mode, args.files, raw_rules, plan_body)
    print(f"\n✏️  Edit:    {edit}")
    print(f"👀 Context: {ctx}")

    if args.list_only:
        return

    # ── 5. Build prompt + invoke orchestrator ──
    prompt = build_prompt(root, args.instruction, raw_rules, edit, ctx,
                          plan_section=plan_section, ensemble=args.ensemble)
    print(f"🚦 Ensemble: {args.ensemble}")

    raw = invoke_orchestrator(prompt, args.ensemble)

    # ── search-replace-workflow: MUST produce <<<PATCH>>> blocks ──
    if args.ensemble == "search-replace-workflow":
        from patch_apply import parse_patches, apply_patches, PatchError

        def try_apply(response):
            patches = parse_patches(response)
            if not patches:
                return None, "no <<<PATCH>>> blocks found"
            requested_set = set(edit)
            filtered = {k: v for k, v in patches.items() if k in requested_set}
            if not filtered:
                return None, "patches for unrequested files only"
            return filtered, None

        patches, err = try_apply(raw)

        if err:
            # One retry with a corrective hint
            print(f"⚠️  First attempt: {err}. Retrying with whitespace correction…")
            retry_prompt = prompt + """
CORRECTION FROM PREVIOUS ATTEMPT:
The SEARCH block you produced did not match the file. The most common
cause is whitespace differences — extra or missing blank lines, or
different indentation. Re-read the FILES TO EDIT section EXACTLY.
Copy each line character-for-character. If there is no blank line
between two lines in the file, do NOT insert one in your SEARCH block.

Produce the patch again.
"""
            raw = invoke_orchestrator(retry_prompt, args.ensemble)
            Path("codebase_agent_raw_output.txt").write_text(raw, encoding="utf-8")
            patches, err = try_apply(raw)

        if err:
            Path("codebase_agent_raw_output.txt").write_text(raw, encoding="utf-8")
            sys.exit(f"❌ search-replace-workflow failed after retry: {err}")

        apply_patches(root, patches, args.dry_run)
        return

    # ── Whole-file ensembles: <<<EDIT>>> blocks or fallback ──
    fallback = edit[0] if len(edit) == 1 else None
    edits = parse_edits(raw, fallback_file=fallback)

    if not edits:
        out = Path("codebase_agent_raw_output.txt")
        out.write_text(raw, encoding="utf-8")
        sys.exit(f"❌ No <<<EDIT>>> blocks found. Raw output saved to {out}")

    # ── 6. Filter to requested files, then apply ──
    all_edits = dict(edits)
    requested = set(edit)
    edits = {k: v for k, v in all_edits.items() if k in requested}
    if not edits:
        print("❌ Orchestrator returned edits for files outside the edit set.")
        print(f"   Requested:  {sorted(requested)}")
        print(f"   Returned:   {sorted(all_edits.keys())}")
        print("   Root cause is almost always a bad planner pick.")
        print("   Try: --files <path> ... , or fix the LLM planner.")
        sys.exit(1)

    apply_or_preview(root, edits, args.dry_run)


if __name__ == "__main__":
    main()