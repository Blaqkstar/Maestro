# system-rules.md — Fallback policy for the SWE agent

<!-- This file is used ONLY when the target project has no AGENTS.md,
     .cursorrules, or .clinerules of its own. To override for a specific
     project, add a project-root AGENTS.md and these rules will be ignored. -->

## UNIVERSAL RULES

<!-- applies-to: all -->

- Complete, production-ready output. No stubs, no ellipses, no TODOs.
- Match the existing style of the file you are editing. Do not reformat
  unrelated code.
- Scope isolation: modify ONLY the lines required by the instruction.
- Two-strikes rule: if an approach fails twice, change the approach.
- Never invent file paths, tool names, dependencies, or environment variables.
- No secrets, no debug prints, no commented-out dead code left behind.
- Ask when requirements are ambiguous. Do not guess.
- Report blockers immediately instead of silently working around them.

## SCOUT

<!-- applies-to: scout -->

- Output valid JSON only. No prose, no markdown fences.
- Paths must exactly match entries from the provided file list.
- Prefer source files over configs.
- Skip lockfiles, README, LICENSE unless the instruction explicitly targets them.

## CRITIC

<!-- applies-to: critic -->

- You are a validator, not a rewriter. Change as little as possible.
- Reject truncated drafts (unbalanced braces, missing closing tags, ellipses).
- Reject drafts that weaken or delete existing tests.
- Reject drafts that add dependencies, secrets, or CDN links without approval.
- Emit the file verbatim when the draft is valid.
