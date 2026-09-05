---
name: de-run-subagent
description: 让任意 Harness（ZCode/Claude Code/Codex…）把华为 DevEco Code（deveco）当子 Agent 用：免登录即送的免费 GLM-5.1 模型（无需自己的 API key，单账号 50 次/分钟）、并行派活（≤6）、--session 续跑迭代、跨项目历史查询、鸿蒙官方工具链（ArkTS 检查/编译构建/真机模拟器）。当用户提到 de-run、deveco 中转站、并行派子 Agent、批量调度 deveco、deveco 子 Agent、续跑 deveco session、鸿蒙批量任务、免费模型外包时使用。
metadata:
  version: 1.0.0
compatibility:
  - zcode
  - claude-code
  - codex
  - cursor
  - opencode
  - gemini-cli
license: MIT
---

# de-run — 把华为 DevEco Code 变成任意 Harness 的子 Agent

主 Agent 负责指挥，deveco 子 Agent 负责干活。deveco（DevEco Code）是华为官方的 HarmonyOS AI 开发 Agent，基于 OpenCode 扩展，**登录华为账号即送免费 GLM-5.1 模型通道（无需自己的 API key）**——零成本把"大量读"的活外包出去，还自带鸿蒙官方开发能力（ArkTS 语法检查、离线文档检索、编译构建、真机/模拟器运行）。

技术上是主 Agent 与 deveco 子 Agent 之间的调度接口：给出若干"工作区目录 + 提示词"，它并行派发给独立子 Agent（≤6 个），完成后返回结构化汇总（每个子 Agent 的 session、动作次数、最终报告）。子 Agent 的搜索、读码、思考都在隔离环境完成，不占主 Agent 上下文；支持 `--session` 续跑同一子 Agent，保持它的记忆做多轮迭代。

姊妹项目：[oc-run](https://github.com/RayMorTwinkle/oc-run)（把 OpenCode 当子 Agent，模型自由）。de-run 与其用法完全一致，区别仅在底层换成了 deveco 与免费模型通道。

## 安装

前置要求：deveco CLI（`npm i -g @deveco/deveco-code`）+ 已完成 `deveco auth login`（华为账号，登录后免费模型可用）+ Python 3.9+。

```bash
# 把 de-run 放进 PATH（示例：软链到 ~/.local/bin）
ln -s "$(pwd)/scripts/de-run.py" ~/.local/bin/de-run
```

## 快速开始

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

完整参数见 `de-run --help`（输出面向 LLM 的中文使用说明）。

## 鸿蒙开发场景（可选增强）

子 Agent 继承 deveco 的全部鸿蒙能力。若需编译/运行鸿蒙工程，给子 Agent 环境装好 [DevEco Studio](https://developer.huawei.com/consumer/cn/download/deveco-studio)（≥6.1，deveco-code 的硬性要求；装在 /Applications 可自动识别，或设 `DEVECO_HOME` 指向安装目录），然后直接下任务：

```bash
de-run --dir /path/to/harmonyos-project --prompt "检查 ArkTS 语法错误并修复，然后编译出 HAP 产物，回报产物路径"
```

子 Agent 会自行调用 deveco 内置的 `arkts_check` / `build_project` / `start_app` 等工具，或使用 deveco 内嵌命令行（create/build/docs/skills/emulator 全套）。

## 从 LLM / Agent 中调用

把 de-run 当作可外包的执行单元，报告是唯一接口。两种推荐用法：

1. **token 外包（单轮）——大量读、简洁报**：派临时子 Agent 去读海量资料（鸿蒙文档/代码/日志），回报只要简洁结论+来源。子 Agent 独立上下文，读再多也不占你的上下文；GLM-5.1 免费，成本可忽略。
2. **主从循环（多轮）——强模型指挥弱模型**：用 `--session` 续跑同一子 Agent（保持记忆），按每次回报决定下一轮，循环直到结果达标。两条铁律：
   - 任务描述要详细：子 Agent 没有你的全局视野，prompt 就是它的世界
   - 回报格式要明确：你只能看到报告/最后发言——回报至少包含 结论 + 来源 + 不确定性 + 未完成项 + 关键函数或举措

两种用法均可并行（一次派多个，≤6）、可异步（借宿主环境如 ZCode / Claude Code 的后台任务机制，完成自动通知）。

## 已知坑（deveco 版本相关，de-run 已内置对策）

- **deveco 0.1.10 的 `deveco run --attach <url>` 事件回传损坏**（只吐 step_start，实际回复已生成）：de-run 的续跑不走 attach，改为 `deveco serve` + `POST /session/:id/message` 直连 HTTP API，实测稳定。
- **原生 `deveco run --session <id>` 非交互续跑会挂起**（与 opencode 同源问题）：de-run 不使用该路径。
- **卡死/spinning 的 deveco TUI 会阻塞新 `deveco serve` 启动**（占用 SQLite 锁）：批量派活前先关掉卡住的 deveco 交互窗口。
- **免费 GLM-5.1 限单账号 50 次/分钟**：高并发批量时建议 `--max-parallel 2~3`，或在 `deveco.jsonc`（兼容 opencode 配置格式）里配置第三方 provider。
- **`deveco db` 子命令无 JSON 输出格式**：de-run 的 `--sessions` 直接以只读方式查询 deveco 的 SQLite（`~/.local/share/deveco/deveco.db`）跨项目列出全部。

## License

MIT
