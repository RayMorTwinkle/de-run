#!/usr/bin/env python3
"""
de-run — deveco (DevEco Code) 中转站（批量并行调度 + 结果汇总）

把若干"工作区目录 + 提示词"任务并行交给 deveco CLI（华为 DevEco Code，
基于 OpenCode 扩展）执行，全部结束后自动解析 deveco 输出的 JSONL 事件流，
汇总每个 Agent 的 session ID、动作次数、最终结果与 tokens 消耗。

由 oc-run 移植而来（opencode → deveco），事件流 schema 与 opencode 完全一致。
前置条件: 已安装 @deveco/deveco-code 并完成 `deveco auth login`（华为账号，
登录后免费用 deveco/GLM-5.1，单账号 50 次/分钟——并行时注意限速）。

无参数运行或 --help 输出详细使用说明（面向大模型）。
纯 Python 标准库，零第三方依赖。
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

PROG = "de-run"
DEFAULT_MAX_PARALLEL = 6
MAX_PARALLEL_LIMIT = 6
DEFAULT_TIMEOUT = 900

HELP = f"""de-run — 主 Agent 与 deveco 子 Agent 之间的调度接口
=====================================================

你给出若干"工作区目录 + 提示词"，de-run 并行派发给独立子 Agent（deveco），
完成后返回结构化汇总（每个子 Agent 的 session、动作次数、最终报告）。
子 Agent 的搜索、读码、思考都在隔离环境完成，不占你的上下文；
也支持 --session 续跑同一子 Agent，保持它的记忆做多轮迭代。

用法示例
--------
1) 单个任务（阻塞执行，跑完输出汇总）:
   {PROG} --dir /path/to/harmonyos-project --prompt "分析这个工程的结构"

2) 多个任务：工作目录与提示词一一对应（主用法，并行度默认 {DEFAULT_MAX_PARALLEL}、上限 {MAX_PARALLEL_LIMIT}）:
   {PROG} --dir /path/A --prompt "分析项目A" --dir /path/B --prompt "分析项目B"
   只给 1 个提示词则广播到所有目录: {PROG} --dir A --dir B --prompt "..."

3) 批量任务文件（每个任务自定义 dir / prompt / title）:
   {PROG} --tasks tasks.json   # 文件为 [{{"dir": "...", "prompt": "...", "title": "..."}}, ...]

4) 接着某个 session 的上下文继续跑（续跑）:
   {PROG} --sessions                                            # 查看历史 session
   {PROG} --dir /path/A --session ses_xxx --prompt "继续上次的分析"
   说明: 续跑经 deveco serve + HTTP API 实现。deveco 0.1.10 的
   `run --attach` 事件回传有 bug（只吐 step_start）、原生 `--session`
   会挂起，两者都不可用；HTTP POST /session/:id/message 实测正常。
   续跑时 --dir 仅用于展示/校验，实际工作目录为 session 原属目录。

推荐用法
--------
1) token 外包（单轮）——大量读、简洁报:
   派临时 agent 去读海量资料（鸿蒙文档/代码/日志），回报只要简洁结论+来源。
   子 agent 独立上下文，读再多也不占你的上下文。

2) 主从循环（多轮）——强模型指挥弱模型:
   用 --session 续跑同一 agent（保持它的记忆），按每次回报决定下一轮，
   循环直到结果达标。两条铁律:
   - 任务描述要详细：agent 没有你的全局视野，prompt 就是它的世界
   - 回报格式要明确：你只能看到报告/最后发言——回报至少包含
     结论 + 来源 + 不确定性 + 未完成项 + 关键函数或举措

两种用法均可并行（一次派多个，≤6）、可异步（借宿主环境如
ZCode / Claude Code 的后台任务机制，完成自动通知）

参数说明
--------
  --dir <path>        工作区目录，可重复指定
  --prompt <text>     提示词，可重复指定，与 --dir 按出现顺序一一对应
                      只给 1 个时广播到所有目录
  --session <id>      续跑指定 session（可重复；按顺序与前几个任务一一配对，
                      数量不能超过任务数；不可与 --tasks 同用）
  --sessions [N]      列出最近 N 个 session（默认 15；传大数如 9999 查全部）
                      后退出，--json 时输出 JSON
  --tasks <file>      任务文件（JSON），与 --dir/--prompt 二选一
  --max-parallel N    最大并行数，默认 {DEFAULT_MAX_PARALLEL}，上限 {MAX_PARALLEL_LIMIT}
                      ⚠️ deveco/GLM-5.1 免费通道限 50 次/分钟，并行别拉满
  --model <m>         指定模型，格式 provider/model（默认 deveco/GLM-5.1，
                      登录华为账号后免费）
  --timeout <sec>     单个任务超时秒数，默认 {DEFAULT_TIMEOUT}
  --json              汇总结果输出为 JSON（否则输出人类可读表格）
  --truncate <n>      人类可读输出中每条结果的最大字符数（默认不截断，
                      输出全文；给 Agent 消费时建议保持全文）

图片 / 文件输入
------------
  读图或附带文件无需额外参数：把文件路径写进提示词即可，子 Agent 会自行读取；
  也可用 deveco run 原生 -f/--file 传附件。

行为说明
--------
- 权限自动批准用 deveco 的 --auto（等价 opencode 的
  --dangerously-skip-permissions）；只读分析类任务请勿让 agent 修改文件
- 需要 `deveco auth login` 已完成（凭据存 ~/.local/share/deveco/auth.json）；
  未登录时 deveco run 会直接失败
- （可选）编译/运行鸿蒙工程：安装 HarmonyOS Command Line Tools 并设置
  DEVECO_CLI_CLT_PATH 环境变量，子 Agent 会自行调用 devecocli 完成构建
- 单个任务失败（目录不存在 / 超时 / deveco 报错）不影响其他任务
- 汇总字段: session ID / 动作统计（按工具名）/ 最终文本 / tokens
"""


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False, prog=PROG)
    p.add_argument("--help", "-h", action="store_true", help="显示帮助")
    p.add_argument("--dir", action="append", default=[], metavar="<path>")
    p.add_argument("--prompt", action="append", default=[], metavar="<text>")
    p.add_argument("--session", action="append", default=[], metavar="<session_id>")
    p.add_argument("--sessions", nargs="?", type=int, const=15, metavar="N",
                   help="列出最近 N 个 session（默认 15）后退出")
    p.add_argument("--tasks", metavar="<file.json>")
    p.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    p.add_argument("--model", metavar="<provider/model>")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--json", action="store_true")
    p.add_argument("--truncate", type=int, default=None, metavar="<n>",
                   help="人类可读输出中每条结果的最大字符数（默认不截断）")
    return p.parse_args(argv)


def build_tasks(args):
    """把参数展开为任务列表: [{"dir", "prompt", "title"}]"""
    tasks = []
    if args.tasks:
        if args.dir or args.prompt:
            sys.exit("错误: --tasks 与 --dir/--prompt 互斥，请二选一。\n")
        try:
            with open(args.tasks, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            sys.exit(f"错误: 找不到任务文件: {args.tasks}\n")
        except json.JSONDecodeError as e:
            sys.exit(f"错误: 任务文件不是合法 JSON（{e}）\n")
        raw = data.get("tasks", data) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            sys.exit("错误: 任务文件需是数组，或 {\"tasks\": [...]} 结构。\n")
        for i, t in enumerate(raw):
            if not isinstance(t, dict) or "dir" not in t or "prompt" not in t:
                sys.exit(f"错误: 任务文件第 {i + 1} 项缺少 dir 或 prompt 字段。\n")
            tasks.append({
                "dir": t["dir"],
                "prompt": t["prompt"],
                "title": t.get("title") or f"任务{i + 1}",
            })
    else:
        if not args.dir:
            sys.exit(HELP + "\n\n错误: 至少需要 --dir 或 --tasks 之一。\n")
        if not args.prompt:
            sys.exit("错误: 需要至少一个 --prompt 提示词。\n")
        n_dir, n_prompt = len(args.dir), len(args.prompt)
        if n_prompt == 1:
            prompts = args.prompt * n_dir  # 单提示词广播到所有目录
        elif n_prompt == n_dir:
            prompts = args.prompt          # 一一对应（主用法）
        else:
            sys.exit(
                f"错误: --dir 有 {n_dir} 个、--prompt 有 {n_prompt} 个，数量不匹配。\n"
                "需一一对应（两者数量相等），或只给 1 个提示词广播到所有目录。\n")
        for d, p in zip(args.dir, prompts):
            tasks.append({
                "dir": d,
                "prompt": p,
                "title": os.path.basename(d.rstrip("/")) or d,
            })
    if args.session:
        if args.tasks:
            sys.exit("错误: --session 与 --tasks 不能同时使用。\n")
        if len(args.session) > len(tasks):
            sys.exit(
                f"错误: --session 有 {len(args.session)} 个、任务只有 {len(tasks)} 个。\n"
                "--session 按顺序与前几个任务配对，数量不能超过任务数。\n")
        for t, sid in zip(tasks, args.session):
            if not sid.startswith("ses_"):
                sys.exit(f"错误: session ID 格式不正确（应以 ses_ 开头）: {sid}\n")
            t["session"] = sid
    return tasks


def parse_events(stdout):
    """解析 deveco run --format json 输出的 JSONL 事件流，忽略非 JSON 行"""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize(task, stdout, stderr, returncode):
    """从一个任务的输出中提取汇总字段（事件 schema 与 opencode 一致）"""
    events = parse_events(stdout or "")
    session_id = None
    actions = Counter()
    text_parts = []
    tokens_total = 0

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("sessionID"):
            session_id = session_id or ev["sessionID"]
        etype = ev.get("type")
        part = ev.get("part")
        if not isinstance(part, dict):
            part = {}
        if etype == "tool_use":
            tool = part.get("tool") or "?"
            actions[tool] += 1
        elif etype == "text":
            if part.get("text"):
                text_parts.append(part["text"])
        elif etype == "step_finish":
            tk = part.get("tokens") or {}
            tokens_total += tk.get("total", 0) or 0

    final_text = (text_parts[-1] if text_parts else "").strip()
    if returncode == 0 and session_id:
        status = "ok"
    else:
        status = "failed"

    return {
        "title": task["title"],
        "dir": task["dir"],
        "status": status,
        "session_id": session_id,
        "actions": dict(actions),
        "total_actions": sum(actions.values()),
        "final_result": final_text,
        "tokens": tokens_total,
        "exit_code": returncode,
        "stderr_tail": (stderr or "").strip()[-500:],
    }


def _task_error(task, status, message):
    """构造失败任务的汇总条目（统一字段结构）"""
    return {
        "title": task["title"], "dir": task["dir"], "status": status,
        "session_id": None, "actions": {}, "total_actions": 0,
        "final_result": message, "tokens": 0,
        "exit_code": None, "stderr_tail": "",
    }


def kill_tree(proc):
    """向进程组发 SIGTERM，连带 deveco 派生的孙进程"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def run_task(task, model, timeout, serve_url=None):
    if task.get("session"):
        # 续跑模式: 直连 serve 的 HTTP API（run --attach 事件回传有 bug，
        # 原生 --session 挂起，故走 POST /session/:id/message）
        return resume_via_http(task, model, serve_url, timeout)
    cmd = [
        "deveco", "run",
        "--dir", task["dir"],
        "--format", "json",
        "--title", task["title"],
        "--auto",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(task["prompt"])

    try:
        # start_new_session: 独立进程组，超时后可按组清理孙进程
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                errors="replace", start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return summarize(task, stdout, stderr, proc.returncode)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return _task_error(task, "timeout", f"超过 {timeout}s 未完成，已终止")
    except FileNotFoundError:
        return _task_error(task, "error", "找不到 deveco 命令，请先安装 (npm i -g @deveco/deveco-code)")
    except Exception as e:
        # 兜底: 任何异常都不应中断整批任务
        return _task_error(task, "error", f"执行异常: {e}")


# ── deveco serve 生命周期管理（续跑用）────────────────────────────

_serve_proc = None
_serve_url = None
_serve_lock = threading.Lock()


def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_serve():
    """懒启动一个 headless deveco server，所有续跑任务共用"""
    global _serve_proc, _serve_url
    with _serve_lock:
        # serve 中途崩溃则重置，下次重新启动
        if _serve_proc is not None and _serve_proc.poll() is not None:
            _serve_proc = None
            _serve_url = None
        if _serve_proc is None:
            port = find_free_port()
            _serve_proc = subprocess.Popen(
                ["deveco", "serve", "--port", str(port), "--print-logs"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            url = f"http://localhost:{port}"
            ok = False
            for _ in range(30):
                if _serve_proc.poll() is not None:
                    break  # serve 进程提前退出 = 启动失败
                try:
                    # /health 返回 200（HTML 页）即视为就绪
                    urllib.request.urlopen(url + "/health", timeout=2)
                    ok = True
                    break
                except Exception:
                    time.sleep(1)
            if not ok:
                code = _serve_proc.poll()
                kill_tree(_serve_proc)
                _serve_proc = None
                raise RuntimeError(
                    f"deveco serve 启动失败（进程退出 exit={code}，或 30s 未就绪）")
            _serve_url = url
    return _serve_url


def stop_serve():
    global _serve_proc, _serve_url
    if _serve_proc is not None:
        kill_tree(_serve_proc)
        try:
            _serve_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_serve_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _serve_proc = None
        _serve_url = None


# ── 续跑：直连 serve 的 HTTP API（run --attach 事件回传有 bug）────

DEFAULT_RESUME_MODEL = "deveco/GLM-5.1"


def resume_via_http(task, model, serve_url, timeout):
    """POST /session/:id/message 完成续跑，返回与 summarize() 同构的汇总。

    响应为单个 JSON: {info: {sessionID, tokens, finish, ...}, parts: [...]}，
    parts 内 text / tool 部件分别对应文本与动作统计。
    """
    if model:
        provider_id, _, model_id = model.partition("/")
        if not model_id:
            provider_id, model_id = "deveco", model
    else:
        provider_id, model_id = DEFAULT_RESUME_MODEL.split("/", 1)
    body = json.dumps({
        "providerID": provider_id,
        "modelID": model_id,
        "parts": [{"type": "text", "text": task["prompt"]}],
    }).encode("utf-8")

    result = _task_error(task, "failed", "")
    actions = Counter()
    text_parts = []
    try:
        req = urllib.request.Request(
            f"{serve_url}/session/{task['session']}/message",
            data=body, headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[-300:]
        except Exception:
            pass
        result["final_result"] = f"HTTP {e.code}: {detail or e.reason}"
        return result
    except Exception as e:
        result["final_result"] = f"执行异常: {e}"
        return result

    info = data.get("info") or {}
    for part in data.get("parts") or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and part.get("text"):
            text_parts.append(part["text"])
        elif ptype == "tool":
            actions[part.get("tool") or part.get("name") or "?"] += 1

    result["status"] = "ok" if (info.get("sessionID")
                                and info.get("finish") in ("stop", "length",
                                                           "tool-calls")) else "failed"
    result["session_id"] = info.get("sessionID") or task["session"]
    result["actions"] = dict(actions)
    result["total_actions"] = sum(actions.values())
    result["final_result"] = (text_parts[-1] if text_parts else "").strip()
    result["tokens"] = (info.get("tokens") or {}).get("total", 0) or 0
    return result


# ── session 历史（--sessions）──────────────────────────────────────

def format_ts(ms):
    """epoch 毫秒 → 本地时间 MM-DD HH:MM"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))
    except (TypeError, ValueError, OSError):
        return str(ms)


def deveco_db_path():
    """优先问 deveco 本身，失败则用默认路径"""
    try:
        p = subprocess.run(["deveco", "db", "path"], capture_output=True,
                           text=True, timeout=30)
        path = p.stdout.strip()
        if p.returncode == 0 and path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return os.path.expanduser("~/.local/share/deveco/deveco.db")


def list_sessions(n):
    """列出最近 n 个 session（只读方式直查 deveco.db SQLite）。

    deveco db 子命令无 --format json（表格输出难解析），故直接用
    python sqlite3 只读打开。deveco.db 与 opencode 的 opencode.db
    schema 不同：无 project/worktree 关联表，session.directory 即工作目录。
    """
    n = max(0, min(n, 10000))
    db_path = deveco_db_path()
    if not os.path.isfile(db_path):
        print(f"警告: 找到不到 deveco 数据库: {db_path}", file=sys.stderr)
        return []
    query = (
        "SELECT id, title, directory, time_updated, model "
        "FROM session WHERE time_archived IS NULL "
        f"ORDER BY time_updated DESC LIMIT {n}"
    )
    items = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute(query).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"警告: deveco.db 查询失败（{e}）", file=sys.stderr)
        return []
    for r in rows:
        model = None
        if r[4]:
            try:
                model = json.loads(r[4]).get("id")
            except Exception:
                model = r[4]
        items.append({
            "session_id": r[0],
            "title": r[1] or "(无标题)",
            "worktree": r[2] or "/",
            "updated": format_ts(r[3]),
            "model": model,
        })
    return items


def print_sessions(items, as_json=False):
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    print(f"de-run 历史 session（最近 {len(items)} 条，deveco 全部项目）")
    for i, it in enumerate(items, 1):
        model = f" · {it['model']}" if it.get("model") else ""
        print(f"  [{i}] {it['session_id']}  {it['title']}{model}")
        print(f"       {it['updated']}  {it['worktree']}")
    print()
    print('续跑方法: de-run --dir <目录> --session <session_id> --prompt "继续..."')


def truncate(text, n=200):
    text = " ".join(text.split())
    if n <= 1:  # 非法截断长度：直接返回原文
        return text
    return text if len(text) <= n else text[: n - 1] + "…"


def print_human(results, workers, truncate_n=None):
    n_ok = sum(1 for r in results if r["status"] == "ok")
    print(f"de-run 汇总 · {len(results)} 个任务 · 并行度 {workers} · 成功 {n_ok}/{len(results)}")
    print("─" * 72)
    icons = {"ok": "✅", "failed": "❌", "timeout": "⏱", "error": "⚠️"}
    for i, r in enumerate(results, 1):
        icon = icons.get(r["status"], "·")
        print(f"{icon} [{i}] {r['title']}")
        print(f"    session: {r['session_id'] or '(无)'}")
        acts = " · ".join(f"{k}×{v}" for k, v in sorted(r["actions"].items())) or "(无工具调用)"
        print(f"    动作:   {r['total_actions']} 次 ({acts})")
        print(f"    tokens: {r['tokens']:,}")
        if r["status"] == "ok":
            res = r["final_result"] if not truncate_n else truncate(r["final_result"], truncate_n)
            print(f"    结果:   {res if res else '(无文本输出)'}")
        else:
            detail = r["final_result"] or r["stderr_tail"] or "未知错误"
            print(f"    错误:   {truncate(detail, 160)}")
        print()


def ensure_deveco():
    """确保 subprocess 能找到 deveco。

    环境 PATH 精简时（非交互 shell / 脚本 / cron），`which deveco`
    可能落空；按常见安装路径探测，命中后把所在目录注入 PATH。
    os.path.isfile 会自动过滤悬空软链（目标不存在返回 False）。
    """
    if shutil.which("deveco"):
        return True
    for pat in (
        "~/.npm-global/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "~/.local/bin",
        "~/.bun/bin",
    ):
        for d in glob.glob(os.path.expanduser(pat)):
            cand = os.path.join(d, "deveco")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                return True
    return False


def main():
    args = parse_args(sys.argv[1:])
    if args.help:
        print(HELP)
        return 0

    if not ensure_deveco():
        sys.exit("错误: 未找到 deveco 命令，请先安装 (npm i -g @deveco/deveco-code)。")

    if args.sessions is not None:
        print_sessions(list_sessions(args.sessions), as_json=args.json)
        return 0

    tasks = build_tasks(args)
    if not tasks:
        sys.exit("错误: 没有可执行的任务。\n")
    if args.max_parallel < 1:
        sys.exit("错误: --max-parallel 至少为 1。\n")
    if args.max_parallel > MAX_PARALLEL_LIMIT:
        print(f"警告: --max-parallel 超过上限 {MAX_PARALLEL_LIMIT}，已按上限执行", file=sys.stderr)
    if args.timeout < 1:
        sys.exit("错误: --timeout 至少为 1 秒。\n")
    workers = min(args.max_parallel, MAX_PARALLEL_LIMIT, len(tasks))

    serve_url = None
    if any(t.get("session") for t in tasks):
        try:
            serve_url = get_serve()
        except RuntimeError as e:
            sys.exit(f"错误: {e}")

    t0 = time.time()
    results = [None] * len(tasks)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_task, t, args.model, args.timeout, serve_url): i
                       for i, t in enumerate(tasks)}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
    finally:
        stop_serve()
    elapsed = time.time() - t0

    if args.json:
        print(json.dumps({
            "summary": {
                "total": len(results),
                "parallel": workers,
                "ok": sum(1 for r in results if r["status"] == "ok"),
                "elapsed_sec": round(elapsed, 1),
            },
            "tasks": results,
        }, ensure_ascii=False, indent=2))
    else:
        print_human(results, workers, args.truncate)
        print(f"总耗时: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
