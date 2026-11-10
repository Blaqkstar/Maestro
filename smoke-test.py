"""smoke_test.py — verify agents_md role filtering end-to-end.

Usage:
    python smoke_test.py "C:\\Users\\you\\path\\to\\website"
"""
import sys
from pathlib import Path

from agents_md import load_rules, rules_for, split_sections, RULE_FILES

ROLES = ["conductor", "scout", "coder", "writer", "critic", "cline"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python smoke_test.py <project-path>")
        sys.exit(1)

    project = Path(sys.argv[1]).resolve()
    if not project.exists():
        print(f"❌ Path does not exist: {project}")
        sys.exit(1)

    print(f"🔍 Starting from: {project}")
    print(f"   Is dir? {project.is_dir()}")

    text, src = load_rules(project, Path(__file__).resolve().parent)
    print(f"\n📄 Rules source: {src or '(none found — agents will use defaults)'}")

    if not text:
        print("\n⚠️  No rules text loaded. Check that AGENTS.md exists somewhere")
        print("    up the tree, or that SWE/system-rules.md exists.")
        sys.exit(2)

    print(f"   Raw size: {len(text)} chars")

    # --- Section breakdown ---
    print("\n📑 Section breakdown:")
    for title, _body, applies in split_sections(text):
        tag = ",".join(sorted(applies)) if applies else "(none)"
        print(f"   [{tag:30s}]  {title}")

    # --- Per-role filtered sizes ---
    print("\n🎭 Role-filtered views:")
    print(f"   {'role':10s}  {'chars':>6s}  first section")
    print(f"   {'-'*10}  {'-'*6}  {'-'*40}")
    for role in ROLES:
        block = rules_for(text, role, header=False)
        if block:
            first = block.splitlines()[0].lstrip("#").strip()
        else:
            first = "(empty — no matching sections)"
        print(f"   {role:10s}  {len(block):6d}  {first}")

    # --- Sanity checks ---
    print("\n✅ Sanity checks:")
    cline = rules_for(text, "cline", header=False)
    coder = rules_for(text, "coder", header=False)
    scout = rules_for(text, "scout", header=False)

    ok = True
    if len(cline) < len(coder):
        print("   ✗ cline view is smaller than coder — expected cline ⊇ coder")
        ok = False
    else:
        print("   ✓ cline view ⊇ coder view")

    if "Tool Discipline" in coder or "TOOL DISCIPLINE" in coder:
        print("   ⚠️  coder view contains Cline-only Tool Discipline section")
        print("      (check the <!-- applies-to --> tags in §6)")
        ok = False
    else:
        print("   ✓ coder view excludes Cline-only Tool Discipline")

    if "6,000" in coder or "6K CHARACTER" in coder.upper():
        print("   ⚠️  coder view contains Cline-only 6k-limit section")
        ok = False
    else:
        print("   ✓ coder view excludes Cline-only 6k limit")

    if "SCOUT-SPECIFIC" in coder or "Scout-specific" in coder:
        print("   ⚠️  coder view contains scout-only rules")
        ok = False
    else:
        print("   ✓ coder view excludes scout-only rules")

    if "SCOUT-SPECIFIC" not in scout and "Scout-specific" not in scout:
        print("   ⚠️  scout view is missing scout-specific rules")
        ok = False
    else:
        print("   ✓ scout view includes scout-specific rules")

    print("\n" + ("🎉 All checks passed." if ok else "❌ Some checks failed — see above."))
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()