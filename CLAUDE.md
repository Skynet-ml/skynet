# CLAUDE.md — Skynet

Claude Code reads this file. All project rules are in `AGENTS.md`, which the
next line imports. Read it first, and open the skill or reference file that
its table names when the task needs it.

@AGENTS.md

`.claude/skills/` holds one symlink for each repo skill in `.agents/skills/`,
so Claude Code lists them. When you add a skill, add its link and the
matching `!.claude/skills/<name>` line in `.gitignore`.
