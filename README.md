<div align="center">

> [English](./README_en.md) | **简体中文**

<img src="de-run.svg" alt="de-run" width="320">

# de-run — 可打断的 DevEco 子 Agent 调度

把 DevEco Code 接到任意主 Agent：后台派活、查询进度、打断纠偏、取消任务、收取报告。

Python 3.9+ · 零第三方依赖 · MIT
</div>

## 使用方式

前置条件：安装 DevEco Code，完成 `deveco auth login`，并确保 `deveco` 与 Python 3.9+ 可用。
本实现依据本机 DevEco **0.2.4** 的实际 `/doc` OpenAPI 验证；启动时检查必要接口，旧版不兼容时明确失败。
当前后台进程与文件锁实现支持 macOS/Linux；Windows 尚未适配。模型、配额与鸿蒙工具能力由 DevEco 提供，取决于账号与安装环境。

```bash
# 可直接运行，或建立软链
python3 scripts/de-run.py --help
ln -s "$(pwd)/scripts/de-run.py" ~/.local/bin/de-run

# 默认异步，立即返回 run_id 与每个 task_id
# de-run start、de-run run 与省略子命令完全等价
de-run --dir /path/to/project --prompt '分析代码结构，报告结论与来源' --json

# 找回任务：所有活跃任务 + 待处理异常 + 最近结束的 10 个
de-run list
de-run list --active
de-run list --recent 30
de-run list --run <run_id> --json

# 使用上一步实际返回的 ID 替换下面的占位符
de-run status <task_id> --json
de-run wait <run_id> --timeout 60 --json
de-run result <task_id> --json

# 默认打断当前轮；确认停止后，在同一个 session 发送新要求
de-run send <task_id> '优先定位数据库连接泄漏，暂停其他方向' --json

# 明确要求不打断：当前轮完成后执行
de-run send <task_id> '接着检查测试覆盖' --queue --json

# 取消运行中或尚在排队的任务
de-run cancel <task_id> --json

# 兼容原先需要阻塞的调用方
de-run run --dir /path/to/project --prompt '分析代码' --wait --json
```

**迁移注意：旧版调用默认阻塞；现在默认异步。** 旧脚本需要最终报告时，增加 `--wait`，或用启动回执中的 ID 调用 `wait` / `result`。

## 接口与语义

| 命令 | 行为 |
|---|---|
| `start` / `run` / 默认入口 | 持久化任务并启动独立后台 worker，立即返回回执 |
| `list` | 所有活跃任务、所有 orphaned 异常、最近结束的 10 个；`--active` 仅未结束任务，`--recent N` 调整历史数量，`--run RUN_ID` 按批次筛选 |
| `status ID` | 读取一个任务或整批任务的当前状态、session、动作、tokens、最新文本 |
| `wait ID` | 等待任务或整批任务结束，返回报告；`--timeout` 仅结束等待 |
| `send TASK TEXT` | 默认先 abort 当前轮，确认 session 空闲，再排入新指令；不等待模型完成 |
| `send TASK TEXT --queue` | 保留当前轮，完成后按顺序执行追加指令 |
| `cancel TASK` | 清空待执行指令，中止当前轮；确认停止后返回 |
| `result ID` | 读取持久化报告与逐轮历史；未结束返回退出码 2 |
| `--sessions [N]` | 只读查询 DevEco SQLite 中的跨项目历史会话 |

`send` 与 `cancel` 返回的是控制指令的确认。`send` 返回 `queued` 表示新指令已持久化，执行受本批并行度约束。
可以给已完成的任务 `send`：worker 会按需重新启动，在原 session 继续。无法确认停止时明确返回错误，不会冒险并发发送。
`--queue` 的后续轮只会在当前轮成功后自动执行；失败时保留待执行文本供检查，需显式 `send` 决定如何继续。

### 默认使用 task ID

普通输出只突出 task ID；`--verbose` 显示底层 session ID，`--json` 始终保留完整映射。`--sessions` 的用途就是查询 DevEco 历史对话，因此仍展示 session ID。

`list` 仅扫描 `DE_RUN_HOME` 中由 de-run 管理的任务，不启动 DevEco，也不查询其全部历史会话。活跃任务和 orphaned 异常不受最近十条限制；各组按最近更新时间排序，活跃组先显示执行中任务，再显示排队任务。worker 消失的旧运行状态会按 `status` 的规则显示为 orphaned。列表不展示提示词和报告全文；`--json` 保留完整任务数据。损坏的批次会明确报告在 `errors` 中，保留其余可读取的结果并返回退出码 1。

### 打断不是回滚

`send` 的实现顺序：

1. 调用 `POST /session/:id/abort`。
2. 要求 DevEco 返回确认，并查询 `/session/status`，等待 session 空闲。
3. 保存被打断轮的报告与统计，标记为 `interrupted`。
4. 用同一 session 的 `prompt_async` 提交新指令。

这会中止 DevEco 的模型/工具执行，但**已经落盘的代码、已经发生的外部操作不会撤销**。
实际工具能否停止取决于 DevEco 和工具自身，尤其是工具启动的独立后台进程。
不会把“HTTP 客户端断开”当作“任务已停止”。

## 批量任务与选项

```bash
# 目录与提示词按顺序配对；单个提示词广播到所有目录
de-run --dir /path/A --prompt '分析 A' --dir /path/B --prompt '分析 B'
de-run --dir /path/A --dir /path/B --prompt '只读审查，报告来源'

# JSON 数组，或 {"tasks": [...]}；每项可含 dir/prompt/title/session
de-run --tasks examples/tasks.example.json --max-parallel 3 --json

# 续跑已有 DevEco session；目录必须与 session 原属目录一致
de-run --dir /path/A --session ses_example --prompt '继续上次分析'

# 显式选择模型；不传时沿用 DevEco 的默认/会话配置
de-run --dir /path/A --prompt '分析' --model provider/model
```

| 选项 | 默认 / 说明 |
|---|---|
| `--max-parallel N` | 每批最多 6 个，默认 6；不是全局账号限流器 |
| `--timeout SEC`（启动时） | 每轮上限 900 秒，超时主动 abort |
| `--timeout SEC`（wait 时） | 默认一直等；超时不取消任务 |
| `--wait` | 启动后阻塞等待整批结果 |
| `--json` | 完整 JSON，适合主 Agent / 脚本 |
| `--verbose` | 普通输出中显示 session ID 等详细信息 |
| `--truncate N` | 仅缩短人类可读报告，不截断持久化数据 |

## 状态、报告与退出码

正常流转：`queued → running → ok / failed / incomplete / timeout`。
控制中可能出现 `interrupting`，取消完成为 `cancelled`。
worker 异常消失或停止无法确认时标记 `orphaned`，不会自动重放可能已经执行过的指令。
应先检查日志与遗留进程，再通过 `--session` 显式续跑。

JSON 包含 `run_id`、`worker_alive`、`summary`、`pending_commands`、`tasks`。
每个任务包含 `task_id`、`session_id`、`status`、`round`、`actions`、`tokens`、`final_result`、`error`、`pending` 和 `history`。
顶层工具统计、tokens 与文本是当前/最近一轮的数据；`history` 保留已结束各轮，包括被打断的轮。
只有无错误、正常 `stop` 且存在最终文本，才标记 `ok`；`length` 等情况标记 `incomplete`。

| 退出码 | 意义 |
|---|---|
| `0` | 请求成功；wait/result 时要求所选任务全部成功 |
| `1` | 任务失败、取消、执行超时、不完整，或控制指令失败 |
| `2` | 参数/环境错误、结果未就绪，或控制指令尚未确认 |
| `124` | wait 的等待时限到期，任务继续运行 |
| `130` | 用户中断等待，后台任务继续运行 |

## 实现与限制

仍然是一个 Python 文件，仅使用标准库。

- 每批任务一个 detached worker，维护独立 DevEco serve；整批结束后回收服务。
- 服务仅监听 127.0.0.1，每次启动使用独立随机密码，凭据只保存在 worker 内存与服务环境中。
- SQLite 持久化任务、命令确认和逐轮结果；并发控制指令按数据库入队顺序处理。
- 通过 `prompt_async` 提交模型任务，轮询会话消息与状态，不依赖已知有缺陷的 CLI attach。
- 文件锁阻止同一批出现两个 worker，也阻止两个 de-run 同时操作同一 session。
- 所有请求传工作目录，续跑时核对 session 原属目录。
- 同 session 的锁只协调 de-run，无法阻止另一个 DevEco TUI/外部客户端直接操作它。
- 不自动创建 worktree；多个任务写同目录仍需主 Agent 分配互不冲突的文件。
- 新 session 默认自动批准工具权限，与旧版 `--auto` 一致；已有 session 沿用原权限配置。只读约束需要额外的权限/环境隔离，提示词不能提供强制保证。
- 状态目录包含提示词、代码分析和报告，应按项目数据管理；可通过 `DE_RUN_HOME` 设置位置，默认 `~/.local/share/de-run`。
- worker 日志为 `<状态目录>/<run_id>/worker.log`，DevEco 日志为 `serve.log`。
- 不提供机器重启后的自动恢复；强杀 worker 可能遗留 DevEco 服务/工具进程，不能把历史状态当作停止证明。
- 多批并行仍会共享模型账号配额；没有自动限速、重试或计费承诺。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试通过本地模拟 DevEco HTTP 服务覆盖真实 CLI 子进程、后台启动、中断后续发、排队、取消、执行超时、失败状态、跨 worker 续跑和结果持久化，不消耗模型配额。

## License

MIT。底层模型、Agent 与鸿蒙开发能力来自 DevEco Code / DevEco Studio。
