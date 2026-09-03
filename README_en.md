<div align="center">

> English | [简体中文](./README.md)

<img src="de-run.svg" alt="de-run" width="320">

# de-run — Huawei DevEco Code as sub-agents for any harness

![Python](https://img.shields.io/badge/Python-3.9%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Deps](https://img.shields.io/badge/Dependencies-zero-brightgreen) ![Model](https://img.shields.io/badge/Model-GLM--5.1_free-orange)

**A free model channel just for signing in — your main agent commands, deveco sub-agents do the work, at zero model cost.**

</div>

---

## What it is

```
  Your main agent (ZCode / Claude Code / Codex …)
              │  de-run --dir A --prompt "…" --dir B --prompt "…"
              ▼
        de-run dispatcher ──parallel──▶ deveco sub-agent A (GLM-5.1 free)
              │                          └── deveco sub-agent B (GLM-5.1 free)
              ▼
       Structured summary: session / tool actions / final report
```

de-run is a thin adapter: give it a set of "working directory + prompt" tasks, and it dispatches them in parallel to isolated sub-agents (up to 6), then returns a structured summary. Sub-agents do all the searching, code reading and thinking in their own context — yours stays clean. `--session` resumes the same sub-agent so it keeps its memory across iterations. Pure Python standard library, zero third-party dependencies.

Under the hood is **DevEco Code (deveco)** — Huawei's official AI coding agent for HarmonyOS, extended from OpenCode. **Signing in with a Huawei account unlocks a free GLM-5.1 channel (50 requests/min per account, no API key of your own required)**, plus the official HarmonyOS toolchain: ArkTS syntax checking, offline HarmonyOS docs, build & run on devices/emulators.

Sibling project: [oc-run](https://github.com/RayMorTwinkle/oc-run) (OpenCode as sub-agents, model freedom). The two share an identical CLI interface — pick whichever fits the task.

## ✨ Highlights

- 🆓 **Zero-cost model**: free GLM-5.1 after Huawei account sign-in — "token outsourcing" is finally actually free
- 🎛️ **Swappable models**: deveco accepts opencode-style config (`deveco.jsonc`); switch with `--model`
- ⚡ **Parallel dispatch**: up to 6 sub-agents at once, each with its own directory and prompt
- 🔁 **Multi-round iteration**: `--session` keeps a sub-agent's memory; loop until the report is good enough
- 📊 **Auto summary**: session ID, tool action counts, final report and tokens per sub-agent
- 🔎 **Cross-project history**: `--sessions` lists sessions from all projects (reads deveco's SQLite directly)
- 🏗️ **Official HarmonyOS toolchain**: sub-agents natively use `arkts_check`, `build_project`, `start_app`, `hdc_log`, `verify_ui`, and `devecocli` (create/build/docs/signature/emulator)
- 🧩 **Runs anywhere**: built-in binary discovery works even with a minimal PATH (cron / scripts / agent subprocesses)

## 🔧 Install

### For AI agents (one-shot install, recommended)

**Paste this prompt into your local AI agent (ZCode / Claude Code / Codex etc.) and it will handle everything:**

````markdown
Please install the de-run skill (GitHub: https://github.com/RayMorTwinkle/de-run).

Background: de-run lets any main agent (ZCode/Claude Code/Codex etc.) use the local Huawei DevEco Code (deveco) as a sub-agent — parallel dispatch, auto summaries, --session resume, and **a free GLM-5.1 model channel just for signing in with a Huawei account (no API key needed)**.
Prerequisites: deveco CLI installed (npm i -g @deveco/deveco-code), `deveco auth login` completed, and python3.

Steps:
1. Download and extract (skip if ~/.agents/skills/de-run-subagent already exists):
   curl -L -o /tmp/de-run.zip https://github.com/RayMorTwinkle/de-run/archive/refs/heads/main.zip
   unzip -o /tmp/de-run.zip -d /tmp/ && mv /tmp/de-run-main ~/.agents/skills/de-run-subagent
   Note: ~/.agents/skills/ is a shared skill directory; use your platform's directory if different
   (Claude Code: ~/.claude/skills/, OpenCode: ~/.config/opencode/skills/, etc.).
2. Verify skill layout: ~/.agents/skills/de-run-subagent/SKILL.md and scripts/de-run.py exist.
3. (Optional but recommended) symlink the command into PATH:
   ln -sf ~/.agents/skills/de-run-subagent/scripts/de-run.py ~/.local/bin/de-run
4. Check deveco: `deveco --version` prints a version; `deveco providers list` shows the Huawei credential.
   If missing, install with `npm i -g @deveco/deveco-code` (Node.js 22+);
   if not signed in, ask the user to run `deveco auth login` in their own terminal (interactive).
5. Verify: de-run --help prints a Chinese usage guide; otherwise use python3 ~/.agents/skills/de-run-subagent/scripts/de-run.py --help.
6. Smoke test: de-run --sessions 3 lists the 3 most recent sessions across projects.
7. Confirm success and summarize capabilities: parallel dispatch / resume (--session) /
   cross-project history (--sessions) / free GLM-5.1 (50 req/min) / HarmonyOS tooling (optional).
````

### For humans

1. Clone or download:
   ```bash
   git clone https://github.com/RayMorTwinkle/de-run.git
   # or: https://github.com/RayMorTwinkle/de-run/archive/refs/heads/main.zip
   ```
2. Install deveco and sign in:
   ```bash
   npm install -g @deveco/deveco-code   # requires Node.js 22+
   deveco auth login                    # Huawei account (interactive; unlocks free GLM-5.1)
   ```
3. Put the `de-run` directory into your agent's skill directory and **rename it to `de-run-subagent`** (Claude Code: `~/.claude/skills/`; OpenCode: `~/.config/opencode/skills/`; shared: `~/.agents/skills/`) — the directory name must match the `name` in SKILL.md
4. (Optional) symlink into PATH: `ln -s "$(pwd)/de-run/scripts/de-run.py" ~/.local/bin/de-run`

## 🚀 Quick start

```bash
# Single task (blocking, prints a summary when done)
de-run --dir /path/to/project --prompt "Analyze this project's tech stack"

# Multiple tasks: directories and prompts pair one-to-one (main usage; default parallelism 6, cap 6)
de-run --dir /path/A --prompt "Analyze project A" --dir /path/B --prompt "Analyze project B"

# One prompt broadcast to all directories
de-run --dir /path/A --dir /path/B --prompt "Summarize this project in English"

# Batch task file (custom dir / prompt / title per task)
de-run --tasks tasks.json   # [{"dir": "...", "prompt": "...", "title": "..."}, ...]

# Resume: continue a session with its memory
de-run --sessions                                  # list recent sessions (across projects)
de-run --dir /path/A --session ses_xxx --prompt "Continue the previous analysis"

# Machine-readable output (for LLMs / scripts)
de-run --dir /path/A --prompt "..." --json
```

Full options: `de-run --help` (LLM-oriented usage guide in Chinese).

## 🏗️ HarmonyOS development

Sub-agents inherit deveco's full HarmonyOS capabilities. Install the [HarmonyOS Command Line Tools](https://developer.huawei.com/consumer/cn/download/command-line-tools-for-hmos) and set `DEVECO_CLI_CLT_PATH`, then:

```bash
# Syntax check + fix + build in one shot
de-run --dir /path/to/harmonyos-project --prompt "Run ArkTS syntax check, fix errors, build a HAP, report the artifact path"

# Parallel module reviews
de-run --dir /path/A --prompt "Review this module for performance issues" --dir /path/B --prompt "Review this module for performance issues"
```

## 🧠 Orchestration tips (for LLMs)

1. **Token outsourcing (single round) — read a lot, report briefly**: dispatch throwaway sub-agents to digest massive material (docs/code/logs); ask for concise conclusions + sources. Their context is isolated; GLM-5.1 is free, so cost is a non-issue.
2. **Master–slave loop (multi-round) — strong model commands weak model**: resume the same sub-agent with `--session`, iterate on its report until done. Two iron rules:
   - Be detailed: the sub-agent has no view of your world; the prompt is its entire world
   - Demand a format: conclusions + sources + uncertainties + unfinished items + key functions

Both patterns work in parallel (≤6) and asynchronously (via your harness's background task mechanism).

## ❓ FAQ

**Why not just use native `deveco run`?**
The native command only "runs once". de-run adds what a main agent actually needs: **context isolation**, **parallel dispatch + structured summaries**, and **reliable resume that works around upstream defects** (see below).

**What's the deal with "free GLM-5.1"?**
deveco is Huawei's official tool; signing in with a Huawei account unlocks an official free GLM-5.1 channel (50 req/min/account). No API key, no billing. Need more throughput? Configure third-party providers in `deveco.jsonc` and switch with `--model`.

**How does de-run relate to oc-run?**
Sibling projects by the same author with identical CLI interfaces: [oc-run](https://github.com/RayMorTwinkle/oc-run) drives OpenCode (model freedom); de-run drives Huawei deveco (free model + HarmonyOS toolchain). They coexist peacefully.

**Naming?**
Command: `de-run`. Skill name: `de-run-subagent` (directory must match SKILL.md `name`). Repo: `de-run`.

## ⚠️ Known limitations

- **deveco 0.1.10's `run --attach` has a broken event relay** (replies are generated but only step_start reaches the client): de-run resumes via `deveco serve` + the HTTP API instead — verified stable
- **Native `deveco run --session` hangs on non-interactive resume**: not used by de-run
- **A stuck/spinning deveco TUI blocks new `deveco serve` instances** (SQLite lock): close it before batch runs
- **Free tier rate limit**: 50 req/min/account; use `--max-parallel 2~3` for heavy batches
- **Platforms**: deveco ships for macOS (Apple Silicon / Intel) and Windows; no Linux build yet

## File structure

```
de-run/                      # repo name; rename to de-run-subagent when installing as a skill
├── SKILL.md                 # agent-facing skill definition (name: de-run-subagent)
├── README.md                # Chinese readme (primary)
├── README_en.md             # English readme
├── LICENSE                  # MIT
├── de-run.svg               # icon
├── scripts/
│   └── de-run.py            # main script (pure Python stdlib, zero deps)
└── examples/
    └── tasks.example.json   # batch task file template
```

## License

MIT
