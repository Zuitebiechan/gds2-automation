---
name: daily-report
description: Generate a daily work report (日报) for the boss summarizing today's project progress, impact, decisions, and blockers. Use when the user asks for 日报, daily report, 写日报, 今天做了什么, summarize today's work, end of day summary, or similar work-summary requests.
---

# Daily Report

Generate a concise boss-facing daily report in Chinese. Focus on business progress and project impact, not code-level implementation details.

## Project Context

Use this project context to frame the report, but do not restate it unless it materially matters for today's work:

- Goal: build a cloud remote vehicle diagnostics platform
- Cloud side runs OEM diagnostic software, APIs, and automation
- Local side runs the VCI proxy client and tray diagnostics UI
- GDS2 is the first fully implemented backend; the codebase is being shaped for more OEM backends through `diagnostic_platform/` and `backends/`

## Workflow

1. Collect evidence.
   - Run `git log --since="midnight" --oneline --all` to see today's commits.
   - Run `git log --since="midnight" --all --format="%h %s%n%b"` for more detail.
   - Check current conversation context for uncommitted work, decisions, and blockers.
   - If today's work is mainly local and uncommitted, also inspect `git status --short` and relevant diffs.

2. Translate technical work into manager-readable progress.
   - Summarize what was accomplished, why it mattered, and what changed for the product or delivery path.
   - Call out important decisions and tradeoffs when they explain the work.
   - Identify blockers, risks, and the next planned move.
   - If multiple approaches were compared, use a compact Markdown table.

3. Output in the format below.

## Output Format

```markdown
YYYY/M/D

## 今天的进展和遇到的问题

进展

- [做了什么事，达到了什么效果]
  - [补充说明：为什么要做、解决了什么问题]
- [另一件事]

问题

- [遇到了什么困难 -> 怎么解决的 / 下一步打算]
```

## Writing Rules

- Write primarily in Chinese.
- Keep technical product names in English when appropriate, for example `GDS2`, `VCI Proxy`, `USB over IP`.
- Optimize for a boss or manager, not for an engineer.
- Do not list files, functions, line counts, or low-level implementation details.
- Prefer outcome statements such as product behavior, stability, delivery progress, architectural direction, or risk reduction.
- Keep it concise enough to read in about one minute.

## Good vs Bad

Bad:

- Modified `device_explorer.py`
- Changed `LVM_FIRST+45` to `LVM_FIRST+115`
- Added a retry loop
- 15 files changed, `+743/-170`

Good:

- Fixed incorrect cloud-side device listing so the system now shows the actual connected device
- Improved VCI Proxy stability by reconnecting automatically after short network drops
- Completed cloud deployment of the Web UI so GDS2 can be controlled remotely from a phone browser

## Table Pattern

```markdown
| 方案 | 原理 | 优点 | 缺点 |
|------|------|------|------|
| A    | 一句话说明 | ... | ... |
| B    | 一句话说明 | ... | ... |
```
