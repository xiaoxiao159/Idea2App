---
name: code-review
description: Review code for correctness bugs when the user asks to review, check, or audit code or a diff. Reads every file in scope first, verifies each suspicion against the actual code path, and reports findings ranked by severity with file:line references.
---

# Code review workflow

1. **Scope.** Ask for or infer which files or diff to review. If unclear, ask before reading.
2. **Read everything first.** Read every file in scope with the read tool before judging any of it. No verdicts from memory or filenames.
3. **Verify, don't speculate.** For each suspected bug, trace the actual code path — inputs, state, branches. If you cannot construct a concrete failure scenario, it is not a finding.
4. **Checklist pass.** Before reporting, read `checklist.md` in this skill's directory and re-check every finding against each item.
5. **Report.** Findings ranked most-severe first. Each finding: `file:line`, a one-sentence claim, and the concrete failure scenario. No findings → say so plainly.
