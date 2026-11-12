"""
patch_apply.py — parse and apply SEARCH/REPLACE patches.

Format (Aider-compatible), wrapped for multi-file:

    <<<PATCH path="relative/path.ext">>>
    <<<<<<< SEARCH
    exact existing text
    =======
    replacement text
    >>>>>>> REPLACE
    <<<END PATCH>>>

One <<<PATCH>>> block per file. Multiple SEARCH/REPLACE pairs per file, applied
in order. Each SEARCH must match the file content exactly once.

MATCHING TIERS (tried in order, most strict first):
  1. EXACT              — byte-for-byte. Preferred.
  2. TRAILING_WS        — ignores trailing whitespace on each line.
                          Emits a warning when it fires.
  3. FULL_REPLACE_FUZZY — SEARCH is >50% of the file and doesn't match
                          byte-for-byte. Treated as a full-file rewrite.
                          Emits a loud warning.
  4. STRUCTURAL         — ignores blank-line differences. Helpers exist but
                          are NOT wired into _locate.

EMPTY SEARCH is the intended way to create a new file OR replace an existing
file's full contents. It resolves to FULL_REPLACE (no warning).

ENCODING HANDLING:
  - File content is read with utf-8-sig, which strips a leading BOM.
  - Both file content and SEARCH/REPLACE strings are normalized to \\n before
    matching. A CRLF file is patchable by a model that emitted LF.
  - On write, the file's original line-ending style (CRLF vs LF) is preserved.
  - BOMs on SEARCH/REPLACE strings are stripped during parse.

RULES ENFORCED ON APPLY:
  - SEARCH must be unique.
  - Result must be at least 50% of original byte size (truncation guard).
  - HTML results must still end with </html>.
  - All patches must validate before any file is written (atomic batch).
  - Identity patches (SEARCH == REPLACE, or every pair a no-op) never write.

ON FAILURE: dumps a per-patch repr (first 80 chars, char count, line count)
of every SEARCH/REPLACE so the raw content is visible for debugging.

AUTO-REPAIR:
  When a <<<PATCH>>> block contains a SEARCH section and a REPLACE body but is
  missing the closing `>>>>>>> REPLACE` marker (a common Qwen failure on long
  patches), the marker is appended automatically so the pair still parses.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path
import ast


ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Cap on how many (search, replace) pairs one <<<PATCH>>> block may contain.
# A well-behaved edit is 1–5 pairs. Hundreds means the model is looping and
# the block is not a real edit.
MAX_PAIRS_PER_BLOCK = 20

PATCH_BLOCK_RE = re.compile(
    r'<<<\s*PATCH\s+path\s*=\s*["\']?([^"\'>\n]+)["\']?\s*>+\s*'
    r'(.*?)'
    r'(?=(?:<<<\s*PATCH\s+path\s*=)|(?:<<<\s*END\s*PATCH\s*>+)|$)',
    re.DOTALL | re.IGNORECASE,
)

SR_RE = re.compile(
    r'<{5,}\s*SEARCH\s*(.*?)\s*={5,}\s*(.*?)\s*>{5,}\s*REPLACE',
    re.DOTALL,
)

REPLACE_MARKER_RE = re.compile(r">{5,}\s*REPLACE", re.IGNORECASE)


class PatchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Encoding normalization
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Canonicalize BOM and line endings for reliable matching."""
    if text and text[0] == "\ufeff":
        text = text[1:]
    # Collapse runs of CR immediately followed by LF ("\r+\n") to a single
    # LF. This repairs double-CRLF corruption (\r\r\n) introduced by text-
    # mode writes on Windows, and also handles any n-CR variant. Bare \r
    # mid-line (rare) is still normalized below — no data is lost.
    text = re.sub(r'\r+\n', '\n', text)
    return text.replace("\r", "\n")

def _split_for_diff(s: str) -> list[str]:
    """Split into lines, ensuring every line ends with a newline.

    difflib.unified_diff emits lines verbatim; if the source content
    lacks a trailing newline, its last line is emitted without one and
    the next line's prefix concatenates onto it ("-old+new"). Appending
    a trailing newline before splitting prevents this.
    """
    if not s:
        return []
    if not s.endswith("\n"):
        s = s + "\n"
    return s.splitlines(keepends=True)

# ---------------------------------------------------------------------------
# Auto-repair
# ---------------------------------------------------------------------------

def _repair_missing_replace_marker(body: str) -> str:
    """Append '>>>>>>> REPLACE' when the model forgot it."""
    if "<<<<<<<" not in body:
        return body
    if "=======" not in body:
        return body
    if REPLACE_MARKER_RE.search(body):
        return body
    return body.rstrip() + "\n>>>>>>> REPLACE"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_patches(raw: str) -> dict[str, list[tuple[str, str]]]:
    """Return {path: [(search, replace), ...]} with normalized strings.

    Raises PatchError when a block is malformed in a way that indicates the
    model is looping or issuing conflicting edits (hundreds of pairs,
    empty SEARCH combined with other pairs).
    """
    raw = ANSI.sub("", raw)
    raw = _normalize(raw)
    
    # Qwen sometimes closes its own markers with HTML-style closing tags,
    # especially when the task involves HTML/CSS/JS files. Strip them.
    raw = re.sub(r'</\s*(?:file|patch|read|list|end[\s_]*patch)\s*>',
             '', raw, flags=re.IGNORECASE)

    # Normalize the model's common malformed closing tags
    raw = re.sub(r'<\s*/\s*PATCH\s*>{2,}', '<<<END PATCH>>>', raw, flags=re.IGNORECASE)

    # Fix common Qwen formatting bugs: squashed newlines around markers
    raw = raw.replace("<<<<<<< SEARCH ", "<<<<<<< SEARCH\n")
    raw = raw.replace(" =======", "\n=======")
    raw = raw.replace("======= ", "=======\n")
    raw = raw.replace(" >>>>>>> REPLACE", "\n>>>>>>> REPLACE")
    raw = raw.replace("<<<PATCH path=", "\n<<<PATCH path=")

    result: dict[str, list[tuple[str, str]]] = {}

    for path, body in PATCH_BLOCK_RE.findall(raw):
        path = path.strip()
        body = _repair_missing_replace_marker(body)
        raw_pairs = SR_RE.findall(body)
        if not raw_pairs:
            continue

        pairs = [(_normalize(s), _normalize(r)) for s, r in raw_pairs]

        # Drop no-ops where both SEARCH and REPLACE are empty. These are
        # noise the model sometimes emits after a legitimate patch.
        pairs = [(s, r) for s, r in pairs if s.strip() or r.strip()]
        if not pairs:
            continue

        # Hard cap: hundreds of pairs = model looping, not a real edit.
        if len(pairs) > MAX_PAIRS_PER_BLOCK:
            raise PatchError(
                f"{path}: {len(pairs)} SEARCH/REPLACE pairs "
                f"(max {MAX_PAIRS_PER_BLOCK}). Model is looping — "
                f"refusing to parse this block."
            )

        # Empty SEARCH means "replace the whole file." It cannot be combined
        # with other pairs; the model would be issuing two conflicting edits
        # to the same content.
        if any(not s.strip() for s, _ in pairs) and len(pairs) > 1:
            raise PatchError(
                f"{path}: empty SEARCH block cannot be combined with other "
                f"SEARCH/REPLACE pairs. Use either a single full-rewrite "
                f"(empty SEARCH) OR targeted edits."
            )

        result.setdefault(path, []).extend(pairs)

    return result


# ---------------------------------------------------------------------------
# Tier 1 & 2 — char-offset matchers
# ---------------------------------------------------------------------------

def _match_trailing_ws_insensitive(content: str, search: str):
    """Trailing-whitespace-insensitive line match. Returns [(start, end), ...]."""
    content_lines = content.split("\n")
    search_lines = [ln.rstrip() for ln in search.split("\n")]
    n = len(search_lines)
    if n == 0 or n > len(content_lines):
        return []

    offsets = []
    pos = 0
    for ln in content_lines:
        offsets.append(pos)
        pos += len(ln) + 1

    matches = []
    for i in range(len(content_lines) - n + 1):
        window = [l.rstrip() for l in content_lines[i:i + n]]
        if window == search_lines:
            start = offsets[i]
            if i + n < len(content_lines):
                end = offsets[i + n] - 1
            else:
                end = len(content)
            matches.append((start, end))
    return matches


# ---------------------------------------------------------------------------
# Tier 3 — structural (blank-line-insensitive) matcher (not wired in)
# ---------------------------------------------------------------------------

def _structural_find(content_lines, search_lines):
    search_nonblank = [l for l in search_lines if l.strip()]
    if not search_nonblank:
        return None

    content_nonblank_idx = [i for i, l in enumerate(content_lines) if l.strip()]
    content_nonblank = [content_lines[i] for i in content_nonblank_idx]

    n = len(search_nonblank)
    m = len(content_nonblank)
    if n > m:
        return None

    matches = []
    for i in range(m - n + 1):
        if content_nonblank[i:i + n] == search_nonblank:
            matches.append(i)

    if len(matches) != 1:
        return None

    start = content_nonblank_idx[matches[0]]
    end = content_nonblank_idx[matches[0] + n - 1]
    return start, end


def _structural_apply(content_lines, start_line, end_line, search_lines, replace_lines):
    span = content_lines[start_line:end_line + 1]
    span_nonblank_idx = [i for i, l in enumerate(span) if l.strip()]

    search_nonblank = [l for l in search_lines if l.strip()]
    replace_nonblank = [l for l in replace_lines if l.strip()]

    matcher = difflib.SequenceMatcher(
        None, search_nonblank, replace_nonblank, autojunk=False
    )
    opcodes = matcher.get_opcodes()

    new_span: list[str] = []
    span_idx = 0

    for tag, i1, i2, j1, j2 in opcodes:
        if i1 < len(span_nonblank_idx):
            target_span_idx = span_nonblank_idx[i1]
        else:
            target_span_idx = len(span)

        while span_idx < target_span_idx:
            new_span.append(span[span_idx])
            span_idx += 1

        if tag == "equal":
            if i2 > i1:
                end_span_idx = span_nonblank_idx[i2 - 1]
            else:
                end_span_idx = target_span_idx - 1
            for k in range(target_span_idx, end_span_idx + 1):
                new_span.append(span[k])
                span_idx = k + 1
        elif tag in ("replace", "insert"):
            for k in range(j1, j2):
                new_span.append(replace_nonblank[k])
            if i2 > i1:
                span_idx = span_nonblank_idx[i2 - 1] + 1
        elif tag == "delete":
            if i2 > i1:
                span_idx = span_nonblank_idx[i2 - 1] + 1

    while span_idx < len(span):
        new_span.append(span[span_idx])
        span_idx += 1

    return content_lines[:start_line] + new_span + content_lines[end_line + 1:]


# ---------------------------------------------------------------------------
# Locate — returns (method_name, start, end)
# ---------------------------------------------------------------------------

def _locate(content: str, search: str) -> tuple[str, int, int]:
    """Return (method, start, end)."""

    # Empty SEARCH == "replace the entire file with REPLACE content."
    # Covers both new-file creation (content == "") and full rewrites.
    if not search.strip():
        return ("full_replace", 0, len(content))

    idx = content.find(search)
    if idx >= 0:
        if content.find(search, idx + 1) >= 0:
            first = search.splitlines()[0][:80]
            raise PatchError(
                f"SEARCH appears more than once. Add more context. "
                f"First line: {first!r}"
            )
        return ("exact", idx, idx + len(search))

    matches = _match_trailing_ws_insensitive(content, search)
    if len(matches) == 1:
        start, end = matches[0]
        return ("trailing_ws", start, end)
    if len(matches) > 1:
        raise PatchError(
            f"SEARCH matches {len(matches)} times (whitespace-insensitive). "
            f"Add more context."
        )

    # Fuzzy fallback: huge SEARCH that didn't match byte-for-byte usually
    # means the model intended a full-file rewrite but paraphrased the old
    # content (dropped blank lines, stripped ## markers, etc.).
    if len(content) > 500 and len(search) > len(content) * 0.5:
        return ("full_replace_fuzzy", 0, len(content))

    first = search.splitlines()[0][:80]
    raise PatchError(
        f"SEARCH block not found in file. "
        f"First line: {first!r}"
    )


# ---------------------------------------------------------------------------
# Applying to a single file's content
# ---------------------------------------------------------------------------

def apply_patches_to_file(
    content: str,
    pairs: list[tuple[str, str]],
) -> tuple[str, list[str]]:
    """Apply (search, replace) pairs in order. Return (new_content, warnings)."""
    warnings: list[str] = []
    original = content

    for i, (search, replace) in enumerate(pairs, 1):
        try:
            method, start, end = _locate(content, search)
        except PatchError as e:
            raise PatchError(f"patch #{i}: {e}") from e

        if method == "full_replace":
            # Intended: empty SEARCH means "replace the whole file."
            content = replace

        elif method == "full_replace_fuzzy":
            warnings.append(
                f"patch #{i}: SEARCH block did not match byte-for-byte "
                f"({len(search)}B SEARCH vs {len(content)}B file); "
                f"APPLIED AS FULL-FILE REPLACEMENT. Review the diff carefully."
            )
            content = replace

        elif method == "exact":
            content = content[:start] + replace + content[end:]

        elif method == "trailing_ws":
            content = content[:start] + replace + content[end:]
            warnings.append(
                f"patch #{i}: matched with trailing-whitespace normalization"
            )

        # "structural" is documented but not wired into _locate yet.

    if pairs and content == original:
        warnings.append("all patches were identity — no change written")

    return content, warnings


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _preview(s: str, n: int = 80) -> str:
    head = s[:n]
    preview = repr(head)
    if len(s) > n:
        preview += f"… (+{len(s) - n} chars)"
    return preview


def _dump_patch_repr(patches: dict[str, list[tuple[str, str]]]) -> None:
    for path, pairs in patches.items():
        print(f"\n  ── received patches for {path} ──")
        for i, (s, r) in enumerate(pairs, 1):
            s_lines = s.count("\n") + 1
            r_lines = r.count("\n") + 1
            print(f"  #{i} SEARCH  ({len(s)} chars, {s_lines} lines): {_preview(s)}")
            print(f"  #{i} REPLACE ({len(r)} chars, {r_lines} lines): {_preview(r)}")


# ---------------------------------------------------------------------------
# Batch apply to a repo
# ---------------------------------------------------------------------------

def apply_patches(
    root: Path,
    patches: dict[str, list[tuple[str, str]]],
    dry_run: bool = False,
) -> None:
    root = root.resolve()
    plan: list[tuple[str, str, str, bool]] = []   # (path, old, new, had_crlf)
    errors: list[str] = []

    for path, pairs in patches.items():
        p = (root / path).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            errors.append(f"{path}: path escapes repository root")
            continue

        # Read raw bytes (newline="" disables text-mode translation) so
        # _normalize() can repair \r\r\n corruption before matching.
        if p.exists():
            with open(p, "r", encoding="utf-8-sig", errors="ignore", newline="") as fh:
                raw_old = fh.read()
        else:
            raw_old = ""

        had_crlf = "\r\n" in raw_old
        old = _normalize(raw_old)

        try:
            new, warns = apply_patches_to_file(old, pairs)
        except PatchError as e:
            errors.append(f"{path}: {e}")
            continue

        for w in warns:
            print(f"  ⚠️  {path}: {w}")

        # Identity patches (SEARCH == REPLACE, or every pair a no-op) never
        # touch disk and never produce a .bak.
        if new == old:
            continue

        if len(old) > 800 and len(new) < len(old) * 0.5:
            errors.append(
                f"{path}: result ({len(new)}B) < 50% of original ({len(old)}B). "
                f"Refusing to write."
            )
            continue

        if p.suffix == ".html" and not new.rstrip().endswith("</html>"):
            errors.append(f"{path}: HTML missing closing </html> after patch.")
            continue

        # 🛡️ Python syntax check
        if p.suffix == ".py":
            try:
                ast.parse(new)
            except SyntaxError as e:
                errors.append(
                    f"{path}: Python SyntaxError at line {e.lineno}: {e.msg}"
                )
                continue

        plan.append((path, old, new, had_crlf))

    if errors:
        print("\n❌ Aborting — patch application failed:\n")
        for e in errors:
            print(f"  ✗ {e}")
        _dump_patch_repr(patches)
        print()
        sys.exit(1)

    n_patches = sum(len(pairs) for pairs in patches.values())
    print(f"\n✅ {n_patches} patch(es) across {len(plan)} file(s) validated.")

    if dry_run:
        print(f"\n🧪 DRY RUN — {len(plan)} file(s) would change:\n")
        for path, old, new, _ in plan:
            sys.stdout.writelines(difflib.unified_diff(
                _split_for_diff(old),
                _split_for_diff(new),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            ))
            print()
        return

    print(f"\n✍️  Writing {len(plan)} file(s) …")
    for path, old, new, had_crlf in plan:
        p = root / path
        if had_crlf:
            new = new.replace("\n", "\r\n")

        # 1. Ensure parent directory exists (for new folders)
        p.parent.mkdir(parents=True, exist_ok=True)

        # 2. Only backup if the file already exists
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            if bak.exists():
                bak.unlink()
            p.rename(bak)

        # 3. Write via open(newline="") so text-mode doesn't re-translate
        #    \n → \r\n and produce \r\r\n. This is the fix.
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
        print(f"  ✓ {path}")