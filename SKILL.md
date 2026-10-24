---
name: de-run-subagent
description: 将本机 DevEco Code 当作可调度的子 Agent：默认异步派活，通过 status/wait/result 查询收取报告，send 默认中止当前轮后在同一 session 续发，cancel 停止任务。适用于用户要求 de-run、deveco 子 Agent、鸿蒙任务或跨 Harness DevEco 调度。
metadata:
  version: 2.2.0
license: MIT
---

# de-run 子 Agent

前置要求：Python 3.9+、已安装并登录的 DevEco Code。实际接口验证版本为 0.2.4；启动时检查必要 HTTP 路由。当前支持 macOS/Linux。

通过 skill 所在目录的 `scripts/de-run.py` 调用，也可用 PATH 中的 `de-run`。

## 调度

```bash
# 默认非阻塞：保存返回的 run_id 与 task_id
de-run --dir /path/project --prompt '任务、允许修改的文件、验收标准、报告格式' --json

# 裸跑展示帮助、模型列表和默认模型；启动时可省略 --model
de-run

# 查询其他目录；也可显式覆盖自动选择
de-run models --dir /path/project --json
de-run --dir /path/project --prompt '任务' --model deveco/GLM-5.3 --json

# 找回任务；活跃任务与 orphaned 不受历史数量限制
de-run list --json
de-run list --active --json
de-run list --recent 30 --json
de-run list --run <run_id> --json

# 查询或等待；wait 超时不会取消任务
de-run status <task_id> --json
de-run wait <run_id> --timeout 60 --json
de-run result <task_id> --json

# 默认打断当前轮，确认停止后在同一 session 发送新要求
de-run send <task_id> '优先处理……，暂停此前方向' --json

# 不打断，当前轮成功后继续
de-run send <task_id> '接着检查……' --queue --json

de-run cancel <task_id> --json

# 需要旧版阻塞行为时
de-run --dir /path/project --prompt '任务' --wait --json
```

- `start`、`run` 与默认入口等价，立即返回回执；不是最终报告。
- `status/wait/result` 接受 task 或 run；`send/cancel` 仅接受 task。
- `list` 默认列出全部活跃任务、全部 orphaned 和最近结束的十个；`--active` 只看未结束任务。列表仅查询 de-run 任务，DevEco 全部对话用 `--sessions`。
- 普通输出隐藏 session ID；需要排查时用 `--verbose`，JSON 保留完整映射。
- `send` 返回 `queued` 只说明控制指令已接收，不表示模型已完成。完成后取 `result`。
- 指令默认替换待执行队列；`--queue` 保留已有队列。失败后不自动执行剩余排队指令。
- 不确定是否停止时，send 返回错误，不会并发派新指令。控制确认超时会返回 command_id；先查 pending_commands，勿盲目重发。
- 可以向已完成任务 send，按需重启 worker，保留原 session 记忆。
- 原生 session 也可通过 `--session ses_...` 续跑，但 `--dir` 必须等于其原属目录。
- 模型选择顺序：显式 `--model` → 可用的原任务/会话模型 → 项目默认 Agent 模型 → 项目 `model` → provider 默认模型（优先 deveco）→ 排序首个模型（优先 deveco）。worker 复用各目录的模型目录；选择显式写入请求，在任务和 history 中保存 `model/model_source`。
- 裸跑与 `--help` 按当前目录展示列表；`models --dir` 按指定目录查询，JSON 的 `default_model/default_source` 表示新任务默认选择。启动回执可能仍在 queued，自动选择结果通过后续 status 查询。

## 结果与判断

报告应要求包含结论、来源、未确认项、修改文件与验证结果。
当前轮指标在 `actions/tokens/final_result/error`；已结束的各轮保存在 `history`，中断轮标记为 `interrupted`。
`tokens` 累计本轮各 assistant 模型调用（含工具循环和缓存统计），按 parentID 区分轮次；不是跨轮累计、单次上下文长度或实际计费额。
只有无错误、正常结束并有最终文本才是 `ok`；不能把启动回执或动作次数当作验收通过。
自动选模并显式提交以绕开 DevEco 默认模型选择错误。提交前订阅错误事件，消息创建前的其他错误会保存到 `error/history` 并结束为 `failed`，`error_hint` 给出恢复建议。模型列出不保证账号权限/配额可用，`provider_default` 是 provider 自身的默认标记，de-run 的选择看 `default_model`。没有已连接模型时在提交提示词前明确失败。
连续 idle 且无完整回复 30 秒会确认中止并标记 `incomplete`；busy/retry 不受该空闲时限影响。事件流断开会主动中止并报错，中止无法确认则为 `orphaned`。
退出码：0 成功；1 失败/取消/不完整；2 参数错误或结果未就绪/控制未确认；124 等待超时。

`orphaned` 表示 worker 消失或无法确认执行状态。先看日志、核对遗留进程，再显式续跑，不自动重复可能已执行过的操作。
状态保存在 `DE_RUN_HOME`（默认 `~/.local/share/de-run`），日志位于对应 run 目录。

## 权限与并行

新 session 默认自动批准工具，已有 session 沿用原权限。提示词不构成只读沙箱；要求强隔离时使用受限环境。
中止不会回滚已写文件或外部操作。文件锁只协调 de-run，不能阻止 DevEco TUI 直接操作同一 session。
默认每批并行度 6，可用 `--max-parallel` 降低；多个批次仍共享账号配额。不要安排多个任务改同一文件。
模型与免费额度取决于 DevEco 当前配置；不传 `--model` 时由 de-run 按上述规则自动选择，不修改 DevEco 全局配置。
