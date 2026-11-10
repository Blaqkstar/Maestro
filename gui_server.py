from flask import Flask, request, Response, send_file, jsonify
import requests
import json
import os
import difflib
import re
import contextlib
import io
from agents_md import load_rules, rules_for
from datetime import datetime
from pathlib import Path

from agents_md import load_rules, rules_for

try:
    from ddgs import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

app = Flask(__name__)
LM_STUDIO_URL = "http://localhost:1234/v1"
SCRIPT_DIR = Path(__file__).parent
GUI_HTML = SCRIPT_DIR / "gui.html"
HISTORY_DIR = SCRIPT_DIR / "history"
HISTORY_DIR.mkdir(exist_ok=True)

MODELS = {
    "conductor": "qwen2.5-3b-instruct",
    "scout": "qwen.qwen3-coder-30b-a3b-instruct",
    "writer": "gemma-3-12b-it",
    "coder": "qwen.qwen3-coder-30b-a3b-instruct",
    "critic": "google/gemma-4-12b-qat",
}

SKIP_DIRS = {"node_modules", "dist", "build", ".git", "__pycache__", ".venv",
             ".cline", ".next", "coverage", "target", ".vscode"}
TEXT_EXTS = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".json",
             ".md", ".py", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp",
             ".cs", ".php", ".sh", ".ps1", ".yaml", ".yml", ".toml", ".txt", ".xml",
             ".svg", ".vue", ".svelte"}
MAX_BATCH_FILES = 8

STATE = {"stop": False}


# ───────────────────────────────────────────────────────────────────────
# Utilities
# ───────────────────────────────────────────────────────────────────────

def sse(data):
    return f"data: {json.dumps(data)}\n\n"


def now_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def safe_read(path):
    try:
        p = Path(path)
        if p.suffix.lower() not in TEXT_EXTS:
            return None
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def web_search(query, max_results=5):
    if not SEARCH_AVAILABLE:
        return []
    try:
        with DDGS() as ddgs:
            return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
                    for r in ddgs.text(query, max_results=max_results)]
    except Exception:
        return []


def find_project_rules(start_path):
    """Locate the nearest AGENTS.md / .cursorrules / .clinerules."""
    text, src = load_rules(start_path, SCRIPT_DIR)
    if src:
        print(f"📄 Rules: {src}")
    return text


def build_tree(root, max_files=100):
    """Return a compact list of relative file paths inside root."""
    root = Path(root)
    files = []
    for entry in root.rglob("*"):
        if len(files) >= max_files:
            break
        if not entry.is_file():
            continue
        rel_parts = entry.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel_parts):
            continue
        if entry.suffix.lower() not in TEXT_EXTS:
            continue
        files.append(str(entry.relative_to(root)).replace("\\", "/"))
    return sorted(files)


def clean_output(raw, is_html):
    final = raw.replace("<!-- APPROVED -->", "").strip()
    if final.startswith("```"):
        lines = final.split("\n")
        if len(lines) > 2:
            final = "\n".join(lines[1:-1]).strip()
    if is_html:
        m = re.search(r"(<!doctype html>.*</html>)", final, re.DOTALL | re.IGNORECASE)
        if m:
            final = m.group(1).strip()
    return final


def make_diff(original, final, name):
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        final.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=3,
    ))


# ───────────────────────────────────────────────────────────────────────
# Agent streaming primitive
# ───────────────────────────────────────────────────────────────────────

def stream_agent(model, system_prompt, user_prompt, agent_name, tag=""):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "temperature": 0.2,
    }
    start = datetime.now()
    yield sse({"event": "agent_start", "agent": agent_name, "model": model, "tag": tag})

    content_acc = ""
    reasoning_acc = ""
    try:
        with requests.post(f"{LM_STUDIO_URL}/chat/completions", json=payload,
                           stream=True, timeout=1800) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if STATE["stop"]:
                    yield sse({"event": "agent_stopped", "agent": agent_name, "tag": tag})
                    return
                if not line:
                    continue
                line = line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    rc = delta.get("reasoning_content") or ""
                    cc = delta.get("content") or ""
                    if rc:
                        reasoning_acc += rc
                        yield sse({"event": "reasoning", "agent": agent_name,
                                   "text": rc, "tag": tag})
                    if cc:
                        content_acc += cc
                        yield sse({"event": "content", "agent": agent_name,
                                   "text": cc, "tag": tag})
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        yield sse({"event": "agent_error", "agent": agent_name,
                   "error": str(e), "tag": tag})
        return

    elapsed = (datetime.now() - start).total_seconds()
    yield sse({
        "event": "agent_end", "agent": agent_name, "tag": tag,
        "content": content_acc, "reasoning": reasoning_acc,
        "elapsed": elapsed,
        "chars": len(content_acc), "reasoning_chars": len(reasoning_acc),
    })


# ───────────────────────────────────────────────────────────────────────
# Core pipeline (single file)
# ───────────────────────────────────────────────────────────────────────

def _with_rules(base_prompt: str, raw_rules: str, role: str) -> str:
    block = rules_for(raw_rules, role)
    return base_prompt + ("\n\n" + block if block else "")


def run_pipeline_on_content(original_content, file_name, file_path,
                            instruction, project_rules="", tree_context=""):
    tag = file_path or file_name

    conductor_system = _with_rules(
        "Output EXACTLY one word: 'CODE' if the request involves programming, "
        "HTML structure, CSS, or bug fixes. Output 'COPY' if it involves "
        "marketing text, SEO, or prose.",
        project_rules, "conductor",
    )
    cond_content = ""
    for evt in stream_agent(
        MODELS["conductor"], conductor_system, instruction,
        "conductor", tag=tag,
    ):
        yield evt
        try:
            obj = json.loads(evt[6:].strip())
            if obj.get("event") == "agent_end" and obj.get("agent") == "conductor":
                cond_content = obj.get("content", "")
        except Exception:
            pass
    if STATE["stop"]:
        return

    workflow = "code" if "CODE" in cond_content.upper() else "copy"

    if workflow == "copy":
        agent, model = "writer", MODELS["writer"]
        sys_prompt = _with_rules(
            "You are a professional copywriter and editor. "
            "Rewrite the file following the USER INSTRUCTION. "
            "Output ONLY the complete updated file. No markdown fences, no explanations. "
            "Preserve ALL existing Tailwind classes, HTML structure, scripts, and forms exactly.",
            project_rules, "writer",
        )
    else:
        agent, model = "coder", MODELS["coder"]
        sys_prompt = _with_rules(
            "You are an expert software engineer. "
            "Output ONLY the complete updated file. No markdown fences, no explanations.",
            project_rules, "coder",
        )

    writer_prompt = (
        f"USER INSTRUCTION: {instruction}\n\n"
        f"FOLDER FILES:\n{tree_context}\n\n"
        f"TARGET FILE ({file_name}):\n{original_content}"
    )

    writer_content = ""
    for evt in stream_agent(model, sys_prompt, writer_prompt, agent, tag=tag):
        yield evt
        try:
            obj = json.loads(evt[6:].strip())
            if obj.get("event") == "agent_end" and obj.get("agent") == agent:
                writer_content = obj.get("content", "")
        except Exception:
            pass
    if STATE["stop"]:
        return

    critic_system = _with_rules(
        "You are an automated formatting validator. "
        "Output the ENTIRE file exactly as received. No conversational text, no fences. "
        "If perfect, first line `<!-- APPROVED -->` then the raw file. "
        "If wrong, output ONLY the corrected raw file.",
        project_rules, "critic",
    )

    critic_content = ""
    for evt in stream_agent(
        MODELS["critic"], critic_system,
        f"Original instruction: {instruction}\n\nUpdated File:\n{writer_content}",
        "critic", tag=tag,
    ):
        yield evt
        try:
            obj = json.loads(evt[6:].strip())
            if obj.get("event") == "agent_end" and obj.get("agent") == "critic":
                critic_content = obj.get("content", "")
        except Exception:
            pass
    if STATE["stop"]:
        return

    final = clean_output(critic_content, file_name.lower().endswith((".html", ".htm")))
    if not final:
        yield sse({"event": "file_error", "file": file_path, "name": file_name,
                   "error": "Empty critic output"})
        return

    if file_name.lower().endswith((".html", ".htm")) and \
            not final.lower().rstrip().endswith("</html>"):
        yield sse({"event": "file_error", "file": file_path, "name": file_name,
                   "error": "SAFETY: truncated output (missing </html>)"})
        return

    diff = make_diff(original_content, final, file_name)
    yield sse({
        "event": "file_complete",
        "file": file_path,
        "name": file_name,
        "workflow": workflow,
        "output": final,
        "diff": diff,
        "bytes": len(final),
    })


# ───────────────────────────────────────────────────────────────────────
# Scout
# ───────────────────────────────────────────────────────────────────────

SCOUT_PROMPT = """You are choosing which files to edit based on a user instruction.

{rules}

Output ONLY a JSON object. No prose, no markdown fences.

Instruction: {instruction}

File tree (relative paths):
{tree}

Select up to {max_files} files that this instruction plausibly requires editing. Prefer source files over configs. Skip README, LICENSE, lockfiles unless the instruction explicitly targets them. Respect any DEPRECATED FEATURES in the rules above.

Output schema:
{{"files": ["relative/path.html"], "reasoning": "one sentence"}}
"""


def _run_scout(folder, instruction):
    tree = build_tree(folder, max_files=120)
    if not tree:
        return [], "No text files found in folder.", []

    raw_rules = find_project_rules(folder)
    scout_rules = rules_for(raw_rules, "scout", header=False)

    prompt = SCOUT_PROMPT.format(
        rules=scout_rules,
        instruction=instruction,
        tree="\n".join(tree),
        max_files=MAX_BATCH_FILES,
    )

    try:
        r = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": MODELS["scout"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        plan = json.loads(raw)
    except Exception as e:
        return tree[:5], f"Scout failed ({e}); falling back to first files.", tree

    valid = []
    for f in plan.get("files", [])[:MAX_BATCH_FILES]:
        if f in tree and f not in valid:
            valid.append(f)

    return valid, plan.get("reasoning", ""), tree


# ───────────────────────────────────────────────────────────────────────
# Flask routes
# ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(GUI_HTML)


@app.route("/api/health")
def health():
    lm_ok = False
    try:
        lm_ok = requests.get(f"{LM_STUDIO_URL}/models", timeout=2).ok
    except Exception:
        pass
    return jsonify({
        "lm_studio": lm_ok,
        "search_available": SEARCH_AVAILABLE,
        "models": MODELS,
    })


@app.route("/api/files")
def files():
    root = request.args.get("root", str(Path.home()))
    try:
        root_path = Path(root).resolve()
        if not root_path.exists() or not root_path.is_dir():
            return jsonify({"error": "Not a directory"}), 400
        entries = []
        for entry in sorted(root_path.iterdir(),
                            key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            try:
                st = entry.stat()
                entries.append({
                    "name": entry.name, "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": st.st_size, "mtime": st.st_mtime,
                })
            except OSError:
                continue
        parent = str(root_path.parent) if root_path.parent != root_path else None
        return jsonify({"root": str(root_path), "parent": parent, "entries": entries})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan-folder", methods=["POST"])
def scan_folder():
    data = request.json or {}
    folder = data.get("folder")
    if not folder or not Path(folder).is_dir():
        return jsonify({"error": "Not a folder"}), 400
    tree = build_tree(folder)
    return jsonify({"folder": folder, "tree": tree, "count": len(tree)})


@app.route("/api/scout", methods=["POST"])
def scout():
    data = request.json or {}
    folder = data.get("folder")
    instruction = (data.get("instruction") or "").strip()
    if not folder or not instruction:
        return jsonify({"error": "Missing folder or instruction"}), 400
    if not Path(folder).is_dir():
        return jsonify({"error": "Folder not found"}), 404

    valid, reasoning, tree = _run_scout(folder, instruction)
    return jsonify({
        "folder": folder,
        "files": valid,
        "reasoning": reasoning,
        "available": tree,
        "max": MAX_BATCH_FILES,
    })


# ───────────────────────────────────────────────────────────────────────
# Batch run (web GUI)
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/run-batch", methods=["POST"])
def run_batch():
    data = request.json or {}
    folder = data.get("folder")
    files = data.get("files", [])
    instruction = (data.get("instruction") or "").strip()
    use_search = data.get("use_search", False)

    if not folder or not Path(folder).is_dir():
        return jsonify({"error": "Folder not found"}), 404
    if not files or not instruction:
        return jsonify({"error": "Missing files or instruction"}), 400
    files = files[:MAX_BATCH_FILES]

    def gen():
        STATE["stop"] = False
        project_rules = find_project_rules(folder)
        tree_context = "\n".join(build_tree(folder, max_files=120))

        search_block = ""
        if use_search:
            yield sse({"event": "search_start", "query": instruction})
            results = web_search(instruction)
            if results:
                search_block = "\n\n=== WEB SEARCH RESULTS ===\n" + "\n".join(
                    f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}"
                    for i, r in enumerate(results, 1)
                )
            yield sse({"event": "search_end", "count": len(results)})

        effective_instruction = instruction
        if search_block:
            effective_instruction = f"{instruction}\n{search_block}"

        results_summary = []
        for idx, rel in enumerate(files, 1):
            if STATE["stop"]:
                yield sse({"event": "stopped"})
                return

            abs_path = str(Path(folder) / rel)
            yield sse({"event": "file_start", "file": abs_path, "name": rel,
                       "index": idx, "total": len(files)})

            original = safe_read(abs_path)
            if original is None:
                yield sse({"event": "file_error", "file": abs_path, "name": rel,
                           "error": "Binary or unreadable file"})
                results_summary.append({"file": abs_path, "name": rel,
                                        "output": None, "diff": None,
                                        "error": "Unreadable file"})
                continue

            file_result = {"file": abs_path, "name": rel,
                           "output": None, "diff": None, "error": None}
            for evt in run_pipeline_on_content(
                original, Path(rel).name, abs_path,
                effective_instruction, project_rules, tree_context,
            ):
                yield evt
                try:
                    obj = json.loads(evt[6:].strip())
                    if obj.get("event") == "file_complete":
                        file_result["output"] = obj["output"]
                        file_result["diff"] = obj["diff"]
                        file_result["bytes"] = obj["bytes"]
                    elif obj.get("event") == "file_error":
                        file_result["error"] = obj["error"]
                except Exception:
                    pass
            results_summary.append(file_result)
            yield sse({"event": "file_end", "file": abs_path, "name": rel})

        run_id = now_id()
        try:
            (HISTORY_DIR / f"{run_id}.json").write_text(json.dumps({
                "id": run_id, "timestamp": datetime.now().isoformat(),
                "folder": folder, "instruction": instruction,
                "results": results_summary,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

        yield sse({"event": "batch_complete", "id": run_id,
                   "results": results_summary})

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ───────────────────────────────────────────────────────────────────────
# VS Code extension: single-file
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/extension/run", methods=["POST"])
def extension_run():
    data = request.json or {}
    target_content = data.get("target_content")
    target_path = data.get("target_path")
    user_prompt = (data.get("prompt") or "").strip()
    project_rules = data.get("project_rules", "") or ""
    use_search = data.get("use_search", False)

    if target_content is None or not target_path or not user_prompt:
        return jsonify({"error": "Missing target_content, target_path, or prompt"}), 400

    file_name = Path(target_path).name

    if not project_rules:
        try:
            project_rules = find_project_rules(target_path)
        except Exception:
            pass

    tree_context = ""
    try:
        parent = Path(target_path).parent
        if parent.is_dir():
            tree_context = "\n".join(build_tree(parent, max_files=80))
    except Exception:
        pass

    def gen():
        STATE["stop"] = False

        search_block = ""
        if use_search:
            yield sse({"event": "search_start", "query": user_prompt})
            results = web_search(user_prompt)
            if results:
                search_block = "\n\n=== WEB SEARCH RESULTS ===\n" + "\n".join(
                    f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}"
                    for i, r in enumerate(results, 1)
                )
            yield sse({"event": "search_end", "count": len(results)})

        effective_instruction = user_prompt
        if search_block:
            effective_instruction = f"{user_prompt}\n{search_block}"

        yield sse({"event": "file_start", "file": target_path,
                   "name": file_name, "index": 1, "total": 1})

        file_result = {"file": target_path, "name": file_name,
                       "output": None, "diff": None, "error": None}
        for evt in run_pipeline_on_content(
            target_content, file_name, target_path,
            effective_instruction, project_rules, tree_context,
        ):
            yield evt
            try:
                obj = json.loads(evt[6:].strip())
                if obj.get("event") == "file_complete":
                    file_result["output"] = obj["output"]
                    file_result["diff"] = obj["diff"]
                    file_result["bytes"] = obj["bytes"]
                elif obj.get("event") == "file_error":
                    file_result["error"] = obj["error"]
            except Exception:
                pass

        yield sse({"event": "file_end", "file": target_path, "name": file_name})
        yield sse({"event": "batch_complete", "id": now_id(),
                   "results": [file_result]})

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ───────────────────────────────────────────────────────────────────────
# VS Code extension: prompt-only
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/extension/agent", methods=["POST"])
def extension_agent():
    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()
    workspace_root = data.get("workspace_root")
    explicit_files = data.get("files") or []
    use_search = data.get("use_search", False)

    if not prompt or not workspace_root:
        return jsonify({"error": "Missing prompt or workspace_root"}), 400
    if not Path(workspace_root).is_dir():
        return jsonify({"error": f"Workspace root not found: {workspace_root}"}), 404

    def gen():
        STATE["stop"] = False
        files = explicit_files
        scout_reasoning = ""

        raw_rules = find_project_rules(workspace_root)

        if not files:
            yield sse({"event": "scout_start"})
            files, scout_reasoning, tree = _run_scout(workspace_root, prompt)
            if not files:
                yield sse({"event": "error",
                           "error": scout_reasoning or "Scout found no matching files"})
                return
            yield sse({
                "event": "scout_end",
                "files": files,
                "reasoning": scout_reasoning,
                "available": tree,
            })

        tree_context = "\n".join(build_tree(workspace_root, max_files=120))

        search_block = ""
        if use_search:
            yield sse({"event": "search_start", "query": prompt})
            results = web_search(prompt)
            if results:
                search_block = "\n\n=== WEB SEARCH RESULTS ===\n" + "\n".join(
                    f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}"
                    for i, r in enumerate(results, 1)
                )
            yield sse({"event": "search_end", "count": len(results)})

        effective_instruction = prompt
        if search_block:
            effective_instruction = f"{prompt}\n{search_block}"

        results_summary = []
        for idx, rel in enumerate(files, 1):
            if STATE["stop"]:
                yield sse({"event": "stopped"})
                return

            abs_path = str(Path(workspace_root) / rel)
            yield sse({"event": "file_start", "file": abs_path, "name": rel,
                       "index": idx, "total": len(files)})

            original = safe_read(abs_path)
            if original is None:
                yield sse({"event": "file_error", "file": abs_path, "name": rel,
                           "error": "Binary or unreadable file"})
                results_summary.append({"file": abs_path, "name": rel,
                                        "output": None, "diff": None,
                                        "error": "Unreadable file"})
                continue

            file_result = {"file": abs_path, "name": rel,
                           "output": None, "diff": None, "error": None}
            for evt in run_pipeline_on_content(
                original, Path(rel).name, abs_path,
                effective_instruction, raw_rules, tree_context,
            ):
                yield evt
                try:
                    obj = json.loads(evt[6:].strip())
                    if obj.get("event") == "file_complete":
                        file_result["output"] = obj["output"]
                        file_result["diff"] = obj["diff"]
                        file_result["bytes"] = obj["bytes"]
                    elif obj.get("event") == "file_error":
                        file_result["error"] = obj["error"]
                except Exception:
                    pass
            results_summary.append(file_result)
            yield sse({"event": "file_end", "file": abs_path, "name": rel})

        yield sse({"event": "batch_complete", "results": results_summary})

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ───────────────────────────────────────────────────────────────────────
# Save / stop / history
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/save-batch", methods=["POST"])
def save_batch():
    data = request.json or {}
    items = data.get("items", [])
    saved = []
    for item in items:
        f = item.get("file")
        out = item.get("output")
        if not f or out is None:
            continue
        try:
            backup = f + ".bak"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(f, backup)
            Path(f).write_text(out, encoding="utf-8")
            saved.append({"file": f, "bytes": len(out)})
        except Exception as e:
            saved.append({"file": f, "error": str(e)})
    return jsonify({"saved": saved})


@app.route("/api/stop", methods=["POST"])
def stop():
    STATE["stop"] = True
    return jsonify({"ok": True})


@app.route("/api/history")
def history():
    entries = []
    for f in sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "id": d.get("id"),
                "timestamp": d.get("timestamp"),
                "folder": d.get("folder"),
                "instruction": (d.get("instruction") or "")[:100],
                "file_count": len(d.get("results", [])),
            })
        except Exception:
            continue
    return jsonify(entries)


@app.route("/api/history/<run_id>")
def history_item(run_id):
    safe = "".join(c for c in run_id if c.isalnum() or c in "_-")
    p = HISTORY_DIR / f"{safe}.json"
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify(json.loads(p.read_text(encoding="utf-8")))

# ═══════════════════════════════════════════════════════════════════════
# Chat panel — sessions, streaming, plan/act modes
# ═══════════════════════════════════════════════════════════════════════

CHAT_HTML = SCRIPT_DIR / "chat.html"
CHAT_SESSIONS_DIR = SCRIPT_DIR / "history" / "chat_sessions"
CHAT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
CHAT_STOP: dict[str, bool] = {}

CHAT_MODELS = {
    "planner": "qwen.qwen3-coder-30b-a3b-instruct",
    "coder":   "qwen.qwen3-coder-30b-a3b-instruct",
    "writer":  "gemma-3-12b-it",
    "critic":  "google/gemma-4-12b-qat",
}

MAX_CONTEXT_TOKENS = 32768
CONDENSE_THRESHOLD = 0.80


def _condense_session(session: dict) -> bool:
    """Summarize the oldest messages to free up context window space."""
    keep_count = 4
    if len(session["messages"]) <= keep_count:
        return False

    to_summarize = session["messages"][:-keep_count]
    to_keep = session["messages"][-keep_count:]

    history_text = ""
    for m in to_summarize:
        role = m["role"]
        if m.get("is_tool_result"):
            role = "tool_result"
        history_text += f"\n[{role.upper()}]: {m['content']}\n"

    prompt = f"""You are an AI assistant. The following conversation is getting too long.
Summarize the conversation so far.
Focus on:
1. What the user originally asked for.
2. Technical decisions made and approach chosen.
3. Code that has been created or modified.
4. Any pending tasks, errors, or blockers.

Be concise but comprehensive.

HISTORY:
{history_text}
"""

    model = "qwen2.5-3b-instruct"
    try:
        r = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1500,
                "stream": False,
            },
            timeout=120,
        )
        r.raise_for_status()
        summary = r.json()["choices"][0]["message"]["content"].strip()

        session["messages"] = [
            {
                "role": "user",
                "content": f"CONDENSED CONTEXT FROM PREVIOUS TURNS:\n{summary}",
                "is_tool_result": True,
                "timestamp": datetime.now().isoformat()
            }
        ] + to_keep

        _save_session(session)
        return True
    except Exception as e:
        print(f"⚠️ Context condensation failed: {e}")
        return False


# ─── Session persistence ──────────────────────────────────────────────

def _session_path(sid: str) -> Path:
    safe = "".join(c for c in sid if c.isalnum() or c in "_-")
    return CHAT_SESSIONS_DIR / f"{safe}.json"


def _load_session(sid: str):
    p = _session_path(sid)
    if not p.exists():
        return None
    try:
        session = json.loads(p.read_text(encoding="utf-8"))
        if session.get("mode") == "act":
            session["mode"] = "code"
        session.setdefault("critic_enabled", True)
        session.setdefault("total_tokens", 0)
        return session
    except Exception:
        return None


def _save_session(session: dict):
    session["updated_at"] = datetime.now().isoformat()
    _session_path(session["id"]).write_text(
        json.dumps(session, indent=2), encoding="utf-8"
    )


def _new_session(repo_root: str, mode: str = "plan") -> dict:
    sid = now_id()
    session = {
        "id": sid,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "repo_root": repo_root,
        "mode": mode,
        "model": CHAT_MODELS["planner"] if mode == "plan" else CHAT_MODELS["coder"],
        "temperature": 0.1,
        "max_tokens": 32768,
        "title": "(new chat)",
        "messages": [],
        "pending_patches": None,
        "pending_diff": None,
        "last_status": None,
        "total_tokens": 0,
        "critic_enabled": True,
    }
    _save_session(session)
    return session


# ─── File helpers ─────────────────────────────────────────────────────

def _safe_read_chat(repo_root: str, rel_path: str):
    try:
        p = (Path(repo_root) / rel_path).resolve()
        p.relative_to(Path(repo_root).resolve())
    except (ValueError, OSError):
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        raw = p.read_bytes()
        if len(raw) > 40000:
            raw = raw[:40000]
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return None


def _list_dir_chat(repo_root: str, rel_dir: str) -> str:
    try:
        p = (Path(repo_root) / rel_dir).resolve() if rel_dir else Path(repo_root).resolve()
        p.relative_to(Path(repo_root).resolve())
    except (ValueError, OSError):
        return "<path escapes repo>"
    if not p.is_dir():
        return "<not a directory>"
    entries = []
    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if c.name.startswith(".") or c.name in SKIP_DIRS:
            continue
        entries.append(f"{'d' if c.is_dir() else 'f'} {c.name}")
    return "\n".join(entries) if entries else "<empty>"


# ─── Marker parsing ───────────────────────────────────────────────────

_READ_RE = re.compile(r'<<<\s*READ\s+path\s*=\s*["\'\u201c\u201d\u2018\u2019]?([^"\'\u201c\u201d\u2018\u2019>\n]+)["\'\u201c\u201d\u2018\u2019]?\s*>>>', re.IGNORECASE)
_LIST_RE = re.compile(r'<<<\s*LIST\s+dir\s*=\s*["\'\u201c\u201d\u2018\u2019]?([^"\'\u201c\u201d\u2018\u2019>\n]*)["\'\u201c\u201d\u2018\u2019]?\s*>>>', re.IGNORECASE)


def _parse_chat_markers(text: str) -> dict:
    return {
        "reads": [m.strip() for m in _READ_RE.findall(text)],
        "lists": [m.strip() for m in _LIST_RE.findall(text)],
    }


def _extract_status(content: str) -> dict:
    for line in reversed(content.splitlines()):
        line = line.strip()
        if line.startswith("PLAN READY:"):
            return {"type": "plan_ready", "summary": line[len("PLAN READY:"):].strip()}
        if line.startswith("NEED INFO:"):
            return {"type": "need_info", "question": line[len("NEED INFO:"):].strip()}
        if line.startswith("OPTIONS:"):
            return {"type": "options", "summary": line[len("OPTIONS:"):].strip()}
            
    # FALLBACK: If the model generated a long response with list items or headers,
    # assume it generated a plan and just forgot the status line.
    if len(content) > 200 and ("1." in content or "- " in content or "##" in content):
        return {"type": "plan_ready", "summary": "Plan generated (missing status tag)."}
        
    return {"type": "unknown"}


# ─── Streaming turn ───────────────────────────────────────────────────

def _stream_chat_turn(model: str, system: str, messages: list,
                      session_id: str, temperature: float, max_tokens: int):
    """Stream a single chat turn. Yields dicts; caller wraps in SSE."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    content = ""
    reasoning = ""
    usage = None
    try:
        with requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json=payload, stream=True, timeout=1800,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if CHAT_STOP.get(session_id):
                    yield {"event": "stopped"}
                    break
                if not line:
                    continue
                line = line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    rc = delta.get("reasoning_content") or ""
                    cc = delta.get("content") or ""
                    if rc:
                        reasoning += rc
                        yield {"event": "thinking", "text": rc}
                    if cc:
                        content += cc
                        yield {"event": "content", "text": cc}
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        yield {"event": "error", "message": str(e)}

    if usage:
        yield {"event": "usage", "usage": usage}
    else:
        prompt_chars = len(system) + sum(len(m.get("content", "")) for m in messages)
        est_prompt = max(1, prompt_chars // 4)
        est_comp = max(1, len(content) // 4)
        yield {"event": "usage", "usage": {
            "prompt_tokens": est_prompt,
            "completion_tokens": est_comp,
            "total_tokens": est_prompt + est_comp,
            "estimated": True,
        }}


# ─── Patch preview ────────────────────────────────────────────────────

def _preview_patches(repo_root: str, patches: dict) -> dict:
    """Compute unified diff for each file.

    Returns {'diff': str, 'errors': [...], 'warnings': [...]}.
    """
    try:
        from patch_apply import apply_patches_to_file, PatchError
    except ImportError as e:
        return {"diff": "", "errors": [f"patch_apply import failed: {e}"],
                "warnings": []}

    diffs = []
    errors = []
    warnings_out = []
    for path, pairs in patches.items():
        p = Path(repo_root) / path

        old = ""
        if p.exists():
            old = p.read_text(encoding="utf-8-sig", errors="ignore")

        old_norm = old.replace("\r\n", "\n").replace("\r", "\n")

        try:
            new, warns = apply_patches_to_file(old_norm, pairs)

            for w in warns:
                warnings_out.append(f"{path}: {w}")

            diff = "".join(difflib.unified_diff(
                old_norm.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ))
            diffs.append(diff)
        except PatchError as e:
            errors.append(f"{path}: {e}")
        except Exception as e:
            errors.append(f"{path}: {e}")

    return {"diff": "\n".join(diffs), "errors": errors, "warnings": warnings_out}


def _run_critic(instruction: str, patches: dict, repo_root: str,
                project_rules: str, mode: str) -> dict:
    """Run the critic on validated patches. Returns {approved, reason, model}."""
    critic_role = "critic"

    patch_blocks = []
    for path, pairs in patches.items():
        buf = [f"=== {path} ==="]
        for i, (search, replace) in enumerate(pairs, 1):
            buf.append(f"--- SEARCH #{i} ---")
            buf.append(search)
            buf.append(f"--- REPLACE #{i} ---")
            buf.append(replace)
        patch_blocks.append("\n".join(buf))
    patches_text = "\n\n".join(patch_blocks)

    originals = []
    total_chars = 0
    for path in patches.keys():
        try:
            p = (Path(repo_root) / path).resolve()
            p.relative_to(Path(repo_root).resolve())
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if len(text) > 20000:
                text = text[:20000] + "\n… [truncated]"
            if total_chars + len(text) > 40000:
                break
            originals.append(f"=== {path} ===\n{text}")
            total_chars += len(text)
        except (OSError, ValueError):
            continue
    originals_text = "\n\n".join(originals)

    critic_rules = rules_for(project_rules, critic_role, header=False)

    focus = "Check for syntax errors, rule violations (§2b/§2c), or reformatting."

    system = f"""You are a strict senior reviewer evaluating a proposed change.

You will see:
  • USER INSTRUCTION (what the user asked for)
  • PATCHES (SEARCH/REPLACE pairs a worker produced)
  • ORIGINAL FILES (current content of the files being patched)

{critic_rules}

{focus}

Respond with EXACTLY ONE of:

  APPROVED

  FAIL: <one-sentence reason>

No prose, no markdown, no explanations beyond the reason line."""

    user = f"""USER INSTRUCTION:
{instruction}

PATCHES:
{patches_text}

ORIGINAL FILES:
{originals_text}

Review the patches."""

    model = CHAT_MODELS["critic"]
    try:
        r = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
                "max_tokens": 150,
                "stream": False,
            },
            timeout=180,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        raw = (msg.get("content") or "").strip()
        if not raw:
            raw = (msg.get("reasoning_content") or "").strip()
    except Exception as e:
        return {"approved": True, "reason": "", "model": model, "error": str(e)}

    raw_clean = raw.replace("<!-- APPROVED -->", "").strip()
    raw_clean = re.sub(r'^```[a-zA-Z]*\n?|\n?```$', '', raw_clean).strip()

    m = re.search(r"FAIL\s*:\s*(.+)", raw_clean, re.IGNORECASE | re.DOTALL)
    if m:
        return {"approved": False, "reason": m.group(1).strip(), "model": model}

    return {"approved": True, "reason": "", "model": model}


# ─── Plan mode generator ──────────────────────────────────────────────

def _chat_plan_stream(session: dict):
    yield sse({"event": "start", "mode": "plan"})

    repo_root = session["repo_root"]
    project_rules = find_project_rules(repo_root)
    tree = build_tree(repo_root, max_files=500)
    planner_rules = rules_for(project_rules, "planner", header=False)

    tree_listing = "\n".join(tree[:400])
    if len(tree) > 400:
        tree_listing += f"\n… [{len(tree) - 400} more files]"

    system = f"""You are a planning agent in READ-ONLY mode.

You help the user think through changes to their codebase:
  - analyze from first principles
  - present 2–3 concrete options when approaches differ
  - note tradeoffs, risks, edge cases
  - ask clarifying questions when ambiguous
  - request more file context when needed

{planner_rules}

CRITICAL RULE: You do NOT have write access. Do NOT output HTML, CSS, JavaScript, or implementation code. 
If the user asks you to "build", "create", or "write" a file, you must ONLY output a step-by-step plan on how to do it.

FILE ACCESS PROTOCOL:
You do not have direct file access. To read, emit markers on their own lines:
  <<<READ path="relative/file.py">>>
  <<<LIST dir="relative/dir">>>

The runtime will fetch content and inject it into your next turn.
Batch requests. Do not re-request the same path.

OUTPUT FORMAT:
Every response MUST end with exactly one status line:
  PLAN READY: <one-line summary>
  NEED INFO: <specific question>
  OPTIONS: <short description of choices>

Do NOT emit <<<EDIT>>> or <<<PATCH>>> blocks. You are a planner.

REPOSITORY: {repo_root}
FILE TREE:
{tree_listing}
"""

    hops = 0
    final_content = ""

    while hops < 5:
        current_tokens = sum(len(m.get("content", "")) // 4 for m in session["messages"])
        if current_tokens > (MAX_CONTEXT_TOKENS * CONDENSE_THRESHOLD):
            yield sse({"event": "thinking", "text": "\n\n*Context window at 80%. Condensing history...*\n\n"})
            if _condense_session(session):
                yield sse({"event": "thinking", "text": "*History condensed. Resuming...*\n\n"})
            else:
                yield sse({"event": "thinking", "text": "*Condensation failed. Proceeding...*\n\n"})

        turn_content = ""
        turn_thinking = ""
        turn_usage = None

        for evt in _stream_chat_turn(
            session["model"], system, session["messages"], session["id"],
            session.get("temperature", 0.2), session.get("max_tokens", 4096),
        ):
            if evt["event"] == "thinking":
                turn_thinking += evt["text"]
                yield sse(evt)
            elif evt["event"] == "content":
                yield sse(evt)
                turn_content += evt["text"]
            elif evt["event"] == "usage":
                turn_usage = evt["usage"]
                session["total_tokens"] = session.get("total_tokens", 0) + turn_usage.get("total_tokens", 0)
                yield sse(evt)
            elif evt["event"] == "stopped":
                _save_session(session)
                yield sse(evt)
                return
            elif evt["event"] == "error":
                _save_session(session)
                yield sse(evt)
                return

        session["messages"].append({
            "role": "assistant",
            "content": turn_content,
            "thinking": turn_thinking,
            "usage_total": turn_usage["total_tokens"] if turn_usage else 0,
            "timestamp": datetime.now().isoformat(),
        })
        final_content = turn_content

        markers = _parse_chat_markers(turn_content)
        if not markers["reads"] and not markers["lists"]:
            if "<<<READ" in turn_content or "<<<LIST" in turn_content:
                print("⚠️ WARNING: Model outputted a READ/LIST marker, but the regex failed to parse it.")
            break

        tool_parts = []
        total_tool_chars = 0
        MAX_TOOL_CHARS = 60000

        for path in markers["reads"]:
            if total_tool_chars > MAX_TOOL_CHARS:
                tool_parts.append(f"=== {path} ===\n<skipped: context limit reached>")
                continue

            yield sse({"event": "tool_call", "type": "read", "path": path})
            text = _safe_read_chat(repo_root, path)
            if text is None:
                yield sse({"event": "tool_result", "type": "read",
                           "path": path, "error": "not found"})
                tool_parts.append(f"=== {path} ===\n<file not found>")
            else:
                if len(text) > 20000:
                    text = text[:20000] + "\n… [file truncated to save context]"

                yield sse({"event": "tool_result", "type": "read",
                           "path": path, "preview": text[:300],
                           "size": len(text)})
                tool_parts.append(f"=== {path} ===\n{text}")
                total_tool_chars += len(text)

        for dir_path in markers["lists"]:
            yield sse({"event": "tool_call", "type": "list", "path": dir_path})
            listing = _list_dir_chat(repo_root, dir_path)
            yield sse({"event": "tool_result", "type": "list",
                       "path": dir_path, "preview": listing[:300]})
            tool_parts.append(f"=== LIST {dir_path or '.'} ===\n{listing}")
            total_tool_chars += len(listing)

        session["messages"].append({
            "role": "user",
            "content": "Runtime fetched:\n\n" + "\n\n".join(tool_parts),
            "is_tool_result": True,
            "timestamp": datetime.now().isoformat(),
        })
        hops += 1

    status = _extract_status(final_content)
    session["last_status"] = status
    _save_session(session)

    yield sse({"event": "status", "status": status})
    yield sse({"event": "complete"})


# ─── Worker system prompt builder ─────────────────────────────────────

def _build_worker_system(mode: str, project_rules: str, repo_root: str,
                        tree_listing: str) -> str:
    """Build the worker system prompt for code or copy mode."""
    if mode in ("act", "code"):
        worker_role = "coder"
        intro = "You are an expert software engineer editing files."
        purpose = (
            "You edit existing files by producing SEARCH/REPLACE patches — "
            "never by rewriting whole files."
        )
    else:  # copy
        worker_role = "writer"
        intro = "You are an expert copywriter and editor."
        purpose = (
            "You improve prose, marketing copy, and user-facing text by "
            "producing SEARCH/REPLACE patches. You preserve all HTML structure, "
            "Tailwind classes, and attributes exactly — you only change text "
            "content. If a new element is needed purely to hold text (e.g., an "
            "extra <p>), add it minimally."
        )

    worker_rules = rules_for(project_rules, worker_role, header=False)

    return f"""{intro}

{purpose}

{worker_rules}

FILE ACCESS PROTOCOL:
To read, emit markers on their own lines:
  <<<READ path="relative/file.py">>>
  <<<LIST dir="relative/dir">>>

CRITICAL: You MUST use <<<READ>>> to fetch the file content in the SAME turn you generate a patch.
If you do not have the exact, current file contents in the conversation history, you will hallucinate a patch.
Even if you think you already know what the file looks like, READ IT AGAIN.

PATCH FORMAT:
For each file you want to modify, emit one <<<PATCH>>> block.
You MUST use proper newlines (line breaks) inside the SEARCH and REPLACE blocks. Do not squash them onto one line.

  <<<PATCH path="relative/path.ext">>>
  <<<<<<< SEARCH
  <the EXACT existing lines you want to change>
  =======
  <the NEW lines that should replace it>
  >>>>>>> REPLACE
  <<<END PATCH>>>

EXAMPLE 1 — targeted edit. Add a comment above a line that already exists:

  <<<PATCH path="index.html">>>
  <<<<<<< SEARCH
  <!doctype html>
  =======
  <!-- my comment -->
  <!doctype html>
  >>>>>>> REPLACE
  <<<END PATCH>>>

EXAMPLE 2 — full-file rewrite (new file OR replacing an existing file's complete contents).
Leave the SEARCH block EMPTY. Do NOT copy the old contents into SEARCH — you will get the whitespace wrong.

  <<<PATCH path="README.md">>>
  <<<<<<< SEARCH
  =======
  # My Project
  ... full contents ...
  >>>>>>> REPLACE
  <<<END PATCH>>>

IMPORTANT: For any instruction that asks you to "rewrite", "regenerate", "recreate",
or "replace the whole file", you MUST use EXAMPLE 2 (empty SEARCH). Do not attempt to
reproduce a long existing file inside SEARCH — treat the task as a full rewrite.

Rules:
- For targeted edits, SEARCH must be copied EXACTLY from the file content you fetched.
- Include enough context that SEARCH appears EXACTLY ONCE.
- If SEARCH and REPLACE are identical, you are hallucinating. Check the file again.
- Obey AGENTS.md §2b/§2c.
- Only emit patches for files you have READ this turn.

REPOSITORY: {repo_root}
FILE TREE:
{tree_listing}
"""


# ─── Act mode generator ───────────────────────────────────────────────

def _chat_act_stream(session: dict):
    """Code mode AND copy mode share this pipeline; only the worker differs."""
    mode = session["mode"]
    if mode == "act":
        mode = "code"

    yield sse({"event": "start", "mode": mode})

    repo_root = session["repo_root"]
    project_rules = find_project_rules(repo_root)
    tree = build_tree(repo_root, max_files=500)
    tree_listing = "\n".join(tree[:400])

    worker_model = CHAT_MODELS["writer"] if mode == "copy" else CHAT_MODELS["coder"]
    system = _build_worker_system(mode, project_rules, repo_root, tree_listing)

    hops = 0
    final_content = ""

    while hops < 8:
        current_tokens = sum(len(m.get("content", "")) // 4 for m in session["messages"])
        if current_tokens > (MAX_CONTEXT_TOKENS * CONDENSE_THRESHOLD):
            yield sse({"event": "thinking", "text": "\n\n*Context window at 80%. Condensing history...*\n\n"})
            if _condense_session(session):
                yield sse({"event": "thinking", "text": "*History condensed. Resuming...*\n\n"})
            else:
                yield sse({"event": "thinking", "text": "*Condensation failed. Proceeding...*\n\n"})

        turn_content = ""
        turn_thinking = ""
        turn_usage = None

        for evt in _stream_chat_turn(
            worker_model, system, session["messages"], session["id"],
            session.get("temperature", 0.2), session.get("max_tokens", 4096),
        ):
            if evt["event"] == "thinking":
                turn_thinking += evt["text"]
                yield sse(evt)
            elif evt["event"] == "content":
                yield sse(evt)
                turn_content += evt["text"]
            elif evt["event"] == "usage":
                turn_usage = evt["usage"]
                session["total_tokens"] = session.get("total_tokens", 0) + turn_usage.get("total_tokens", 0)
                yield sse(evt)
            elif evt["event"] == "stopped":
                _save_session(session)
                yield sse(evt)
                return
            elif evt["event"] == "error":
                _save_session(session)
                yield sse(evt)
                return

        markers = _parse_chat_markers(turn_content)

        # 1. If the model asked to read files, fulfill the reads FIRST.
        if markers["reads"] or markers["lists"]:
            if "<<<PATCH" in turn_content.upper():
                turn_content = turn_content.split("<<<PATCH")[0].strip()

            session["messages"].append({
                "role": "assistant",
                "content": turn_content,
                "thinking": turn_thinking,
                "usage_total": turn_usage["total_tokens"] if turn_usage else 0,
                "timestamp": datetime.now().isoformat(),
            })

            tool_parts = []
            total_tool_chars = 0
            MAX_TOOL_CHARS = 60000

            for path in markers["reads"]:
                if total_tool_chars > MAX_TOOL_CHARS:
                    tool_parts.append(f"=== {path} ===\n<skipped: context limit reached>")
                    continue

                yield sse({"event": "tool_call", "type": "read", "path": path})
                text = _safe_read_chat(repo_root, path)
                if text is None:
                    yield sse({"event": "tool_result", "type": "read", "path": path, "error": "not found"})
                    tool_parts.append(f"=== {path} ===\n<file not found>")
                else:
                    if len(text) > 20000:
                        text = text[:20000] + "\n… [file truncated to save context]"

                    yield sse({"event": "tool_result", "type": "read", "path": path, "preview": text[:300], "size": len(text)})
                    tool_parts.append(f"=== {path} ===\n{text}")
                    total_tool_chars += len(text)

            for dir_path in markers["lists"]:
                yield sse({"event": "tool_call", "type": "list", "path": dir_path})
                listing = _list_dir_chat(repo_root, dir_path)
                yield sse({"event": "tool_result", "type": "list", "path": dir_path, "preview": listing[:300]})
                tool_parts.append(f"=== LIST {dir_path or '.'} ===\n{listing}")
                total_tool_chars += len(listing)

            session["messages"].append({
                "role": "user",
                "content": "Runtime fetched:\n\n" + "\n\n".join(tool_parts),
                "is_tool_result": True,
                "timestamp": datetime.now().isoformat(),
            })
            hops += 1
            continue

        # 2. No reads requested. Append message and check for patches.
        session["messages"].append({
            "role": "assistant",
            "content": turn_content,
            "thinking": turn_thinking,
            "usage_total": turn_usage["total_tokens"] if turn_usage else 0,
            "timestamp": datetime.now().isoformat(),
        })
        final_content = turn_content

        if "<<<PATCH" in turn_content.upper():
            break

        break

    # Auto-close if the model forgot the closing END PATCH tag.
    if "<<<PATCH" in final_content.upper() and "<<<END PATCH" not in final_content.upper():
        final_content = final_content.strip() + "\n<<<END PATCH>>>"
        if session["messages"] and session["messages"][-1]["role"] == "assistant":
            session["messages"][-1]["content"] = final_content

    try:
        from patch_apply import parse_patches
    except ImportError as e:
        yield sse({"event": "error", "message": f"patch_apply import failed: {e}"})
        return

    patches = parse_patches(final_content)
    if not patches:
        err_msg = "The model did not output a valid <<<PATCH>>> block."
        if not final_content.strip():
            err_msg = "The model returned an empty response. Check LM Studio logs for context limit errors."
        elif "<<<PATCH" in final_content and "<<<END PATCH" not in final_content:
            err_msg = "The model started a patch but ran out of tokens before finishing it. Increase your context length in LM Studio."

        session["last_error"] = [err_msg]
        _save_session(session)
        yield sse({"event": "status", "status": {"type": "no_patches"}})
        yield sse({"event": "complete"})
        return

    preview = _preview_patches(repo_root, patches)
    if preview["errors"]:
        session["last_error"] = preview["errors"]
        _save_session(session)
        yield sse({
            "event": "patch_errors",
            "errors": preview["errors"],
            "warnings": preview.get("warnings", []),
            "diff": preview["diff"],
        })
        yield sse({"event": "complete"})
        return

    session["last_error"] = None

    critic_status = "skipped"
    critic_reason = ""
    critic_model = None
    if session.get("critic_enabled", True):
        yield sse({"event": "critic_start", "mode": mode})
        user_msg = next(
            (m["content"] for m in reversed(session["messages"])
             if m["role"] == "user" and not m.get("is_tool_result")),
            "",
        )
        critic = _run_critic(user_msg, patches, repo_root, project_rules, mode)
        critic_model = critic.get("model")
        if critic["approved"]:
            critic_status = "approved"
            yield sse({"event": "critic_approved", "model": critic_model})
        else:
            critic_status = "rejected"
            critic_reason = critic["reason"]
            yield sse({
                "event": "critic_rejected",
                "reason": critic_reason,
                "model": critic_model,
            })

    session["pending_patches"] = patches
    session["pending_diff"] = preview["diff"]
    session["pending_critic_status"] = critic_status
    session["pending_critic_reason"] = critic_reason
    session["pending_critic_model"] = critic_model
    _save_session(session)

    yield sse({
        "event": "patch_preview",
        "files": list(patches.keys()),
        "diff": preview["diff"],
        "warnings": preview.get("warnings", []),
        "critic_status": critic_status,
        "critic_reason": critic_reason,
    })
    yield sse({"event": "complete"})


# ─── Routes ───────────────────────────────────────────────────────────

@app.route("/chat")
def chat_page():
    if not CHAT_HTML.exists():
        return "chat.html not found", 404
    return send_file(CHAT_HTML)


@app.route("/api/chat/sessions")
def chat_list_sessions():
    q = (request.args.get("q") or "").strip().lower()
    entries = []
    for f in sorted(CHAT_SESSIONS_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:200]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if q:
                haystack = (d.get("title", "") + " " +
                            " ".join(m.get("content", "") for m in d.get("messages", []))
                            ).lower()
                if q not in haystack:
                    continue
            entries.append({
                "id": d.get("id"),
                "title": d.get("title", "(untitled)"),
                "mode": d.get("mode"),
                "updated_at": d.get("updated_at"),
                "message_count": len(d.get("messages", [])),
                "repo_root": d.get("repo_root"),
                "total_tokens": d.get("total_tokens", 0),
            })
        except Exception:
            continue
    return jsonify(entries)


@app.route("/api/chat/session", methods=["POST"])
def chat_create_session():
    data = request.json or {}
    repo_root = (data.get("repo_root") or "").strip()
    mode = data.get("mode", "plan")
    if not repo_root or not Path(repo_root).is_dir():
        return jsonify({"error": "repo_root must be an existing directory"}), 400
    session = _new_session(repo_root, mode)
    return jsonify(session)


@app.route("/api/chat/session/<sid>", methods=["GET"])
def chat_get_session(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404
    return jsonify(session)


@app.route("/api/chat/session/<sid>", methods=["DELETE"])
def chat_delete_session(sid):
    p = _session_path(sid)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"ok": True})


@app.route("/api/chat/session/<sid>", methods=["PATCH"])
def chat_update_session(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404
    data = request.json or {}
    for key in ("title", "mode", "model", "temperature", "max_tokens",
                "total_tokens", "critic_enabled"):
        if key in data:
            session[key] = data[key]
    _save_session(session)
    return jsonify(session)


@app.route("/api/chat/session/<sid>/message", methods=["POST"])
def chat_send_message(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    if session.get("title") == "(new chat)":
        session["title"] = msg[:60]
    session["messages"].append({
        "role": "user",
        "content": msg,
        "timestamp": datetime.now().isoformat(),
    })
    session["last_error"] = None
    _save_session(session)
    CHAT_STOP[sid] = False

    def gen():
        try:
            if session["mode"] in ("act", "code", "copy"):
                yield from _chat_act_stream(session)
            else:
                yield from _chat_plan_stream(session)
        finally:
            CHAT_STOP.pop(sid, None)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/chat/session/<sid>/stop", methods=["POST"])
def chat_stop(sid):
    CHAT_STOP[sid] = True
    return jsonify({"ok": True})


@app.route("/api/chat/session/<sid>/files")
def chat_list_files(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404
    repo_root = session.get("repo_root")
    if not repo_root or not Path(repo_root).is_dir():
        return jsonify({"files": []})
    try:
        files = build_tree(repo_root, max_files=1000)
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"files": [], "error": str(e)})


@app.route("/api/chat/session/<sid>/apply", methods=["POST"])
def chat_apply(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404
    patches = session.get("pending_patches")
    if not patches:
        return jsonify({"error": "no pending patches"}), 400

    try:
        from patch_apply import apply_patches
    except ImportError as e:
        return jsonify({"error": f"patch_apply import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            apply_patches(Path(session["repo_root"]), patches, dry_run=False)
    except SystemExit:
        return jsonify({
            "error": "patch application failed",
            "log": buf.getvalue(),
        }), 400
    except Exception as e:
        return jsonify({"error": str(e), "log": buf.getvalue()}), 500

    applied = list(patches.keys())
    session["pending_patches"] = None
    session["pending_diff"] = None
    session["pending_critic_status"] = None
    session["pending_critic_reason"] = None
    session["pending_critic_model"] = None
    session["messages"].append({
        "role": "system",
        "content": f"Applied patches to: {', '.join(applied)}",
        "timestamp": datetime.now().isoformat(),
    })
    _save_session(session)

    return jsonify({"ok": True, "files": applied, "log": buf.getvalue()})


@app.route("/api/chat/session/<sid>/reject", methods=["POST"])
def chat_reject(sid):
    session = _load_session(sid)
    if not session:
        return jsonify({"error": "not found"}), 404

    had_patches = session.get("pending_patches") is not None
    critic_status = session.get("pending_critic_status") or "skipped"

    session["pending_patches"] = None
    session["pending_diff"] = None
    session["pending_critic_status"] = None
    session["pending_critic_reason"] = None
    session["pending_critic_model"] = None

    if had_patches:
        mode = session.get("mode", "code")
        if mode == "act":
            mode = "code"
        label = {"code": "code", "copy": "copy"}.get(mode, "code")
        critic_note = ""
        if critic_status == "rejected":
            critic_note = " (critic had concerns)"
        elif critic_status == "approved":
            critic_note = " (critic approved, but you rejected)"
        session["messages"].append({
            "role": "system",
            "content": f"Patches rejected by user{critic_note}. The {label} change was not applied.",
            "timestamp": datetime.now().isoformat(),
        })

    _save_session(session)
    return jsonify({"ok": True, "had_patches": had_patches})


if __name__ == "__main__":
    print(f"🎛️  GUI at http://localhost:5000")
    print(f"   Chat at http://localhost:5000/chat")
    print(f"   LM Studio: {LM_STUDIO_URL}")
    print(f"   Search: {'available' if SEARCH_AVAILABLE else 'unavailable (pip install ddgs)'}")
    app.run(host="127.0.0.1", port=5000, threaded=True)