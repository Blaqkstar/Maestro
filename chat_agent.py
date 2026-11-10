#!/usr/bin/env python3
"""
chat_agent.py — interactive terminal planning session.

Examples:
    python chat_agent.py "G:\\Personal\\Dev\\Web\\Drone_Website"
    python chat_agent.py . "Let's plan adding a newsletter signup"

Commands inside the REPL:
    /read <path>     manually add a file to context
    /list [dir]      manually list a directory
    /tree            print the repo file tree
    /save            save the plan artifact
    /reset           clear conversation history
    /help            show this help
    /exit            quit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents_md import load_rules, rules_for
from plan_agent import (
    HERE, discover_files, fetch_requested, parse_markers,
    invoke_planner, save_plan, build_initial_prompt,
    build_followup_prompt,
)


def run_turn(history, ensemble, root, quiet=False):
    prompt = "\n\n".join(
        f"### {m['role'].upper()}\n{m['content']}" for m in history
    )
    response = invoke_planner(prompt, ensemble)
    history.append({"role": "assistant", "content": response})

    # Handle automated READ/LIST markers
    hops = 0
    while hops < 5:
        markers = parse_markers(response)
        if not markers["reads"] and not markers["lists"]:
            break
        req = markers["reads"] + markers["lists"]
        print(f"\n  📖 reading: {', '.join(req)}")
        content, _ = fetch_requested(markers, root)
        history.append({"role": "user", "content": build_followup_prompt(content)})
        prompt = "\n\n".join(
            f"### {m['role'].upper()}\n{m['content']}" for m in history
        )
        response = invoke_planner(prompt, ensemble)
        history.append({"role": "assistant", "content": response})
        hops += 1

    return response


def main():
    ap = argparse.ArgumentParser(description="Interactive planning REPL")
    ap.add_argument("repo")
    ap.add_argument("instruction", nargs="?", default="")
    ap.add_argument("--ensemble", default="plan-workflow")
    ap.add_argument("--save-on-exit", action="store_true")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not root.is_dir():
        sys.exit(f"❌ Not a directory: {root}")

    raw_rules, rules_src = load_rules(root, HERE)
    tree = discover_files(root)
    if not tree:
        sys.exit("❌ No files found.")

    print(f"📂 {root}")
    if rules_src:
        print(f"📄 {rules_src}")
    print(f"📁 {len(tree)} files")
    print("   Type /help for commands, /exit to quit.\n")

    history = [{"role": "system",
                "content": "You are a planning agent in read-only mode."}]

    # Optional opening instruction
    if args.instruction:
        history.append({
            "role": "user",
            "content": build_initial_prompt(root, args.instruction, tree, raw_rules),
        })
        print("planner> ", end="", flush=True)
        response = run_turn(history, args.ensemble, root)
        print(response)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        low = user_input.lower()

        if low in ("/exit", "/quit", "exit", "quit"):
            break

        if low == "/help":
            print(__doc__)
            continue

        if low == "/tree":
            for f in tree[:200]:
                print(" ", f)
            if len(tree) > 200:
                print(f"  … [{len(tree) - 200} more]")
            continue

        if low == "/reset":
            history = [{"role": "system",
                        "content": "You are a planning agent in read-only mode."}]
            print("🔄 Conversation cleared.")
            continue

        if low.startswith("/read "):
            rel = user_input[6:].strip()
            content, _ = fetch_requested({"reads": [rel], "lists": []}, root)
            history.append({"role": "user", "content": f"<manually added>\n{content}"})
            print(f"📖 Added {rel} to context.")
            continue

        if low.startswith("/list"):
            rel = user_input[5:].strip() or ""
            content, _ = fetch_requested({"reads": [], "lists": [rel]}, root)
            history.append({"role": "user", "content": f"<manually added>\n{content}"})
            print(content)
            continue

        if low == "/save":
            plan = next(
                (m["content"] for m in reversed(history) if m["role"] == "assistant"),
                "",
            )
            out = save_plan(root, args.instruction or "(interactive)", history, plan)
            print(f"💾 Saved: {out}")
            continue

        # Normal message
        history.append({"role": "user", "content": user_input})
        print("\nplanner> ", end="", flush=True)
        response = run_turn(history, args.ensemble, root)
        print(response)

    if args.save_on_exit:
        plan = next(
            (m["content"] for m in reversed(history) if m["role"] == "assistant"),
            "",
        )
        if plan:
            out = save_plan(root, args.instruction or "(interactive)", history, plan)
            print(f"\n💾 Saved: {out}")


if __name__ == "__main__":
    main()