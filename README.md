<div align="center">

> [English](./README_en.md) | 简体中文

<!-- 图标占位：后续加入 de-run.svg 后，在此处添加 <img src="de-run.svg" alt="de-run" width="320"> -->

# de-run — 把华为 DevEco Code 变成任意 Harness 的子 Agent

![Python](https://img.shields.io/badge/Python-3.9%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Deps](https://img.shields.io/badge/Dependencies-zero-brightgreen) ![Model](https://img.shields.io/badge/Model-GLM--5.1_free-orange)

**登录华为账号就有免费模型——主 Agent 负责指挥，deveco 子 Agent 负责干活，不花一分钱模型费。**

</div>

---

## 它是什么

```
  你的主 Agent（ZCode / Claude Code / Codex …）
              │  de-run --dir A --prompt "…" --dir B --prompt "…"
              ▼
        de-run 调度器 ──并行──▶ deveco 子 Agent A（GLM-5.1 免费）
              │                  └── deveco 子 Agent B（GLM-5.1 免费）
              ▼
       结构化汇总：session / 动作次数 / 最终报告
```

de-run 是一层薄薄的适配器：给出若干"工作区目录 + 提示词"，它并行派发给独立子 Agent（≤6 个），完成后返回结构化汇总。子 Agent 的搜索、读码、思考都在隔离环境完成，不占主 Agent 上下文；支持 `--session` 续跑同一子 Agent，保持记忆做多轮迭代。纯 Python 标准库，零第三方依赖。

底层是华为官方的 **DevEco Code（deveco）**——面向 HarmonyOS 开发的 AI Agent，基于 OpenCode 扩展。**登录华为账号即送免费 GLM-5.1 通道（单账号 50 次/分钟，无需自己的 API key）**，还内置鸿蒙官方开发能力：ArkTS 语法检查、HarmonyOS 离线文档、编译构建、真机/模拟器运行。

姊妹项目：[oc-run](https://github.com/RayMorTwinkle/oc-run)（把 OpenCode 当子 Agent，模型自由）。两者用法完全一致，按需选用。

## ✨ 它能干什么

- 🆓 **零成本模型**：登录华为账号即用免费 GLM-5.1，不用自己的 API key，不绑定付费供应商——"token 外包"这件事第一次真正免费
- 🎛️ **模型可换**：deveco 兼容 opencode 配置格式（`deveco.jsonc`），可接第三方 provider，`--model` 任意切
- ⚡ **并行派活**：一次最多 6 个子 Agent 同时干活，各自独立工作目录与提示词
- 🔁 **多轮迭代**：`--session` 续跑同一子 Agent，保持它的记忆，按报告循环指挥直到达标
- 📊 **自动汇总**：每个子 Agent 的 session ID、动作次数（按工具分组）、最终报告、tokens，一目了然
- 🔎 **跨项目历史**：`--sessions` 列出所有项目的 session（直读 deveco 的 SQLite）
- 🏗️ **鸿蒙官方工具链**：子 Agent 天然会 `arkts_check`、`build_project`、`start_app`、`hdc_log`、`verify_ui`，也能调 `devecocli`（工程创建/编译/离线文档/签名/模拟器）
- 🧩 **任意环境可跑**：内置探测，PATH 精简环境（cron / 脚本 / Agent 子进程）也能找到 deveco

## 🔧 安装

### 面向 AI 智能体（一键安装，推荐）

**把下面这段提示词直接复制给你的本地 AI Agent（ZCode / Claude Code / Codex 等），它就能自动完成安装：**

````markdown
请帮我安装 de-run skill（GitHub: https://github.com/RayMorTwinkle/de-run）。

背景：de-run 让任何主 Agent（ZCode/Claude Code/Codex 等）都能把本机的华为 DevEco Code（deveco）当子 Agent 指挥——并行派活、自动汇总报告、--session 续跑，且**登录华为账号即可免费用 GLM-5.1 模型，无需自己的 API key**。
它依赖本机已安装的 deveco CLI（npm i -g @deveco/deveco-code）、已完成 `deveco auth login`（华为账号登录，免费模型通道的前置条件）和 python3。

安装步骤：
1. 下载并解压（如果 ~/.agents/skills/de-run-subagent 已存在则先跳过，直接验证）：
   curl -L -o /tmp/de-run.zip https://github.com/RayMorTwinkle/de-run/archive/refs/heads/main.zip
   unzip -o /tmp/de-run.zip -d /tmp/ && mv /tmp/de-run-main ~/.agents/skills/de-run-subagent
   说明：~/.agents/skills/ 是多个 AI 工具共用的 skill 目录；如果你的平台用别的目录
   （如 Claude Code: ~/.claude/skills/，OpenCode: ~/.config/opencode/skills/），装到对应目录即可。
2. 验证 skill 结构：确认 ~/.agents/skills/de-run-subagent/SKILL.md 和 scripts/de-run.py 存在。
3. （可选但推荐）把 de-run 命令放进 PATH，方便直接调用：
   ln -sf ~/.agents/skills/de-run-subagent/scripts/de-run.py ~/.local/bin/de-run
4. 检查 deveco 环境：deveco --version 应输出版本号；deveco providers list 应显示已登录的华为账号凭据。
   若未安装 deveco，先 `npm i -g @deveco/deveco-code`（需 Node.js 22+）；
   若未登录，提示用户在自己的终端执行 `deveco auth login`（交互式华为账号登录）。
5. 验证命令：de-run --help 应输出中文使用说明；若 PATH 里没有，用 python3 ~/.agents/skills/de-run-subagent/scripts/de-run.py --help。
6. 端到端测试：de-run --sessions 3 应列出最近 3 个 session（跨所有项目）。
7. 向用户确认安装成功，并简述 de-run 的能力：并行派活 / 续跑（--session）/ 跨项目历史（--sessions）/
   免费 GLM-5.1（50 次/分钟）/ 鸿蒙开发能力（可选，需 HarmonyOS Command Line Tools）。
````

### 面向人类用户

1. 克隆或下载仓库：
   ```bash
   git clone https://github.com/RayMorTwinkle/de-run.git
   # 或下载 zip: https://github.com/RayMorTwinkle/de-run/archive/refs/heads/main.zip
   ```
2. 安装 deveco 并登录：
   ```bash
   npm install -g @deveco/deveco-code   # 需 Node.js 22+
   deveco auth login                    # 华为账号登录（交互式，送免费 GLM-5.1）
   ```
3. 将 `de-run` 目录放入智能体的 skill 目录并**重命名为 `de-run-subagent`**（Claude Code: `~/.claude/skills/`；OpenCode: `~/.config/opencode/skills/`；通用共享: `~/.agents/skills/`）——skill 目录名须与 SKILL.md 的 `name` 一致
4. （可选）软链命令到 PATH：`ln -s "$(pwd)/de-run/scripts/de-run.py" ~/.local/bin/de-run`

## 🚀 快速开始

```bash
# 单个任务（阻塞执行，跑完输出汇总）
de-run --dir /path/to/project --prompt "分析这个项目的技术栈"

# 多个任务：工作目录与提示词一一对应（主用法，并行度默认 6、上限 6）
de-run --dir /path/A --prompt "分析项目A" --dir /path/B --prompt "分析项目B"

# 多个目录共用同一个提示词（广播）
de-run --dir /path/A --dir /path/B --prompt "用中文简述这个项目"

# 批量任务文件（每任务自定义 dir / prompt / title）
de-run --tasks tasks.json   # 文件为 [{"dir": "...", "prompt": "...", "title": "..."}, ...]

# 续跑：接着某个 session 的上下文继续跑
de-run --sessions                                  # 查看历史 session（跨所有项目）
de-run --dir /path/A --session ses_xxx --prompt "继续上次的分析"

# 机器可读输出（给 LLM / 脚本消费）
de-run --dir /path/A --prompt "..." --json
```

完整参数与推荐用法见 `de-run --help`（输出面向 LLM 的中文使用说明）。

## 🏗️ 鸿蒙开发场景

deveco 是华为鸿蒙官方 AI Agent，子 Agent 继承其全部鸿蒙能力。给子 Agent 环境装好 [HarmonyOS Command Line Tools](https://developer.huawei.com/consumer/cn/download/command-line-tools-for-hmos) 并设置 `DEVECO_CLI_CLT_PATH` 后：

```bash
# 语法检查 + 修复 + 编译一条龙
de-run --dir /path/to/harmonyos-project --prompt "运行 ArkTS 语法检查并修复错误，然后编译出 HAP，回报产物路径"

# 并行审查多个模块
de-run --dir /path/A --prompt "审查这个模块的性能问题" --dir /path/B --prompt "审查这个模块的性能问题"

# 查鸿蒙文档（deveco 内置离线文档检索，免联网）
de-run --dir /path/to/project --prompt "搜索 deveco docs 里关于后台任务的指南并总结要点"
```

## 🧠 推荐用法（给 LLM 的编排建议）

1. **token 外包（单轮）——大量读、简洁报**：派临时子 Agent 去读海量资料（网页/代码/文档），回报只要简洁结论+来源。子 Agent 独立上下文，读再多也不占你的上下文；GLM-5.1 免费，成本可忽略。
2. **主从循环（多轮）——强模型指挥弱模型**：用 `--session` 续跑同一子 Agent（保持记忆），按每次回报决定下一轮，循环直到结果达标。两条铁律：
   - 任务描述要详细：子 Agent 没有你的全局视野，prompt 就是它的世界
   - 回报格式要明确：你只能看到报告/最后发言——回报至少包含 结论 + 来源 + 不确定性 + 未完成项 + 关键函数或举措

两种用法均可并行（一次派多个，≤6）、可异步（借宿主环境如 ZCode / Claude Code 的后台任务机制，完成自动通知）。

## ❓ 常见疑问

**为什么不用原生 `deveco run` 直接跑？**
原生命令只解决"跑一次"；de-run 补上三件主 Agent 真正需要的事：**上下文隔离**（子 Agent 读几十万 tokens 资料，你的上下文一滴不占）、**并行调度 + 结构化汇总**（一次派 6 个，统一收报告）、**绕开上游缺陷的可靠续跑**（见下）。

**"免费 GLM-5.1" 是怎么回事？**
deveco（DevEco Code）是华为官方工具，用华为账号登录后官方免费提供 GLM-5.1 模型通道，单账号限 50 次/分钟——这是官方通道，不需要你自己申请 API key 或充值。配额内完全零成本；需要更高吞吐时可在 `deveco.jsonc` 配置第三方 provider，再用 `--model` 切换。

**de-run 和 oc-run 什么关系？**
同一作者的姊妹项目，命令行接口完全一致：[oc-run](https://github.com/RayMorTwinkle/oc-run) 调度 OpenCode（模型自由，适合已有 opencode 配置的用户）；de-run 调度华为 deveco（免费模型 + 鸿蒙工具链，适合零成本外包和鸿蒙开发）。两者可共存，按任务选工具。

**de-run、de-run-subagent、仓库名是什么关系？**
命令叫 `de-run`，skill 名叫 `de-run-subagent`（skill 目录名与 SKILL.md 的 `name` 一致），GitHub 仓库名 `de-run`。装好 skill 后，用命令、用 skill 触发都指向同一个工具。

## ⚠️ 已知限制

- **deveco 0.1.10 的 `run --attach` 事件回传损坏**（回复已生成但事件流只吐 step_start）：de-run 的续跑改为 `deveco serve` + HTTP API 直连，已实测稳定；deveco 后续版本修复后不受影响
- **原生 `deveco run --session` 非交互续跑会挂起**：de-run 不使用该路径
- **卡死/spinning 的 deveco TUI 会阻塞 `deveco serve` 启动**：批量派活前关掉卡住的 deveco 交互窗口
- **免费通道限速**：50 次/分钟/账号，高并发批量建议 `--max-parallel 2~3`
- **平台**：deveco 目前提供 macOS（Apple Silicon / Intel）与 Windows 版本，暂无 Linux

## 文件结构

```
de-run/                      # 仓库名；作为 skill 安装时重命名为 de-run-subagent
├── SKILL.md                 # 面向 AI 智能体的 skill 定义（name: de-run-subagent）
├── README.md                # 简体中文说明文件（主文档）
├── README_en.md             # 英文说明文件
├── LICENSE                  # MIT
├── scripts/
│   └── de-run.py            # 主脚本（纯 Python 标准库，零依赖）
└── examples/
    └── tasks.example.json   # 批量任务文件模板
```

## License

MIT
