"""agents_md.py — per-role filtering of AGENTS.md / .cursorrules policy files.

Sections in policy files may be tagged with:

    <!-- applies-to: coder, writer -->

Untagged sections implicitly apply to every role. Valid roles currently in use:
conductor, scout, coder, writer, critic, cline, all.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

SECTION_TAG_RE = re.compile(
    r'<!--\s*applies-to\s*:\s*([a-z0-9,\s_\-]+?)\s*-->',
    re.IGNORECASE,
)
SECTION_HEAD_RE = re.compile(r'^##\s+', re.MULTILINE)

RULE_FILES = ("AGENTS.md", ".cursorrules", ".clinerules")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def split_sections(text: str) -> Iterable[tuple[str, str, set[str]]]:
    """Yield (title, body_without_tag, applies_to_set) for each ## section.

    Any content before the first `## ` heading is yielded as PRELUDE and
    applies to everyone. Tag-less sections also default to {'all'}.
    """
    heads = list(SECTION_HEAD_RE.finditer(text))
    if not heads:
        yield ("PRELUDE", text, {"all"})
        return

    if heads[0].start() > 0:
        yield ("PRELUDE", text[: heads[0].start()], {"all"})

    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunk = text[m.start():end]
        first_nl = chunk.find("\n")
        if first_nl >= 0:
            title = chunk[2:first_nl].strip()
            body = chunk[first_nl + 1:]
        else:
            title = chunk.lstrip("#").strip()
            body = ""

        tag = SECTION_TAG_RE.search(body[:400])
        if tag:
            applies = {s.strip().lower() for s in tag.group(1).split(",") if s.strip()}
        else:
            applies = {"all"}

        clean_body = SECTION_TAG_RE.sub("", body, count=1)
        yield (title, clean_body, applies)


def filter_for_role(text: str, role: str) -> str:
    role = role.lower()
    # Legacy path: Cline plays every role at once and should see the full file.
    # Bypass filtering entirely for it.
    if role == "cline":
        return "\n\n".join(
            f"## {title}\n{body.strip()}"
            for title, body, _applies in split_sections(text)
        )

    out: list[str] = []
    for title, body, applies in split_sections(text):
        if "all" in applies or role in applies:
            out.append(f"## {title}\n{body.strip()}")
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Discovery (walk up from a target path; fall back to SWE/system-rules.md)
# ---------------------------------------------------------------------------

def load_rules(start_path, script_dir: Path) -> tuple[str, str | None]:
    """Find the nearest project rules file, or the SWE fallback.

    Returns (raw_text, source_path_or_None). Empty text means no rules found.
    """
    p = Path(start_path)
    if p.is_file():
        p = p.parent
    try:
        p = p.resolve()
    except OSError:
        return "", None

    while True:
        for cf in RULE_FILES:
            candidate = p / cf
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8"), str(candidate)
                except OSError:
                    continue
        if p == p.parent:
            break
        p = p.parent

    fallback = Path(script_dir) / "system-rules.md"
    if fallback.is_file():
        try:
            return fallback.read_text(encoding="utf-8"), str(fallback)
        except OSError:
            pass

    return "", None


# ---------------------------------------------------------------------------
# Prompt-ready formatter
# ---------------------------------------------------------------------------

def rules_for(text: str, role: str, header: bool = True) -> str:
    """Return a prompt-ready policy block filtered for `role`."""
    if not text:
        return ""
    filtered = filter_for_role(text, role)
    if not filtered:
        return ""
    if not header:
        return filtered
    return (
        f"\n--- PROJECT RULES (role: {role}) ---\n"
        f"{filtered}\n"
        f"--- END PROJECT RULES ---\n"
    )