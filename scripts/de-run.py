#!/usr/bin/env python3
"""Durable, asynchronous DevEco task control. Python standard library only."""
import argparse
import base64
from collections import Counter
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_PARALLEL = 6
IDLE_GRACE = 30  # prompt_async may return before startup marks the session busy.
TERMINAL = {'ok', 'failed', 'incomplete', 'cancelled', 'timeout', 'orphaned'}
HELP = '''de-run — 异步 DevEco 子 Agent 调度（需要提供 /doc API 的 DevEco）

  de-run [start|run] --dir PATH --prompt TEXT [--json]
      默认后台启动，立即返回 run_id 和 task_id；可重复 --dir/--prompt。
  de-run wait ID [--timeout SEC] [--json]
      阻塞等待一个 task 或整批 run；超时只停止等待，不取消任务。
  de-run status ID [--json]
      查询持久化状态、session、工具动作和最新文本。
  de-run list [--active] [--recent N] [--run RUN_ID] [--json]
      列出所有活跃任务、待处理异常，以及最近结束的 10 个任务。
      默认只突出 task ID；--verbose 显示底层 session ID。
  de-run send TASK_ID TEXT [--queue] [--json]
      默认中止当前轮，确认停止后在同一 session 发新指令。
      --queue 保留当前轮，结束后追加执行。返回控制命令的接收确认。
  de-run cancel TASK_ID [--json]
      取消排队或运行中的任务；确认停止后返回。不会回滚已写文件。
  de-run result ID [--json] [--truncate N]
      获取报告、错误和各轮历史；任务未结束时退出码为 2。
  de-run --sessions [N] [--json]
      只读列出 DevEco 的历史 session。
  de-run models [--dir PATH] [--json]
      查询当前目录已连接 provider 的模型；不提交模型任务。

启动选项：
  --tasks FILE          JSON 数组或 {"tasks": [...]}，每项 dir/prompt/title/session
  --session SES_ID      续跑已有 session；工作目录必须与 session 一致
  --model PROVIDER/MODEL 显式选择模型；省略时自动选择并提交可用模型
  --max-parallel N      默认 6，上限 6（每一批次）
  --timeout SEC         每轮执行上限，默认 900 秒；超时主动 abort
  --wait                启动后阻塞，便于迁移原先的调用方式
  --json                机器可读输出

状态目录：DE_RUN_HOME，默认 ~/.local/share/de-run（私有目录）。
状态与报告在调用进程退出后仍保留；每批后台 worker 持有 DevEco serve。
默认自动批准工具权限，与旧版 --auto 一致。没有文件系统隔离或自动回滚。
支持 macOS/Linux；Windows 的进程与锁实现尚未支持。
退出码：0 成功；1 失败/取消/不完整；2 参数错误或结果未就绪；124 等待超时。
'''


def identifier(prefix):
    return prefix + uuid.uuid4().hex[:12]


def message_identifier():
    # DevEco sorts conversation IDs lexicographically. Match its ascending 6-byte
    # timestamp/counter prefix instead of supplying an unordered UUID as messageID.
    stamp = (int(time.time() * 1000) << 12) & ((1 << 48) - 1)
    return 'msg_%012x%s' % (stamp, uuid.uuid4().hex[:22])


def state_home():
    p = Path(os.environ.get('DE_RUN_HOME', '~/.local/share/de-run')).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def run_path(value):
    m = re.fullmatch(r'(run|task)_([a-f0-9]{12})(?:_([1-9][0-9]*))?', value)
    if not m or (m[1] == 'task') != bool(m[3]):
        raise ValueError('ID 应为 start 返回的 run_id 或 task_id')
    p = state_home() / ('run_' + m[2])
    if not (p / 'state.db').is_file():
        raise ValueError('找不到任务: ' + value)
    return p


def connect(p):
    db = sqlite3.connect(p / 'state.db', timeout=10)
    db.row_factory = sqlite3.Row
    return db


def task_rows(db):
    return [json.loads(r[0]) for r in db.execute('SELECT data FROM tasks ORDER BY ordinal')]


def save_task(db, task):
    task['updated_at'] = time.time()
    with db:
        db.execute('UPDATE tasks SET data=? WHERE id=?',
                   (json.dumps(task, ensure_ascii=False), task['task_id']))


def settings(db):
    return json.loads(db.execute('SELECT data FROM settings').fetchone()[0])


def worker_alive(p):
    with open(p / 'worker.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def spawn_worker(p):
    if worker_alive(p):
        return
    with open(p / 'worker.log', 'ab', buffering=0) as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker', p.name],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def ensure_deveco():
    found = shutil.which('deveco')
    if found:
        return found
    for directory in ('~/.npm-global/bin', '/opt/homebrew/bin', '/usr/local/bin',
                      '~/.local/bin', '~/.bun/bin'):
        candidate = Path(directory).expanduser() / 'deveco'
        if candidate.is_file() and os.access(candidate, os.X_OK):
            os.environ['PATH'] = str(candidate.parent) + os.pathsep + os.environ.get('PATH', '')
            return str(candidate)
    raise ValueError('未找到 deveco，请安装并完成 deveco auth login')


def build_tasks(args):
    if args.tasks:
        if args.dir or args.prompt or args.session:
            raise ValueError('--tasks 不可与 --dir/--prompt/--session 混用；session 可写在 JSON 每项内')
        data = json.loads(Path(args.tasks).read_text(encoding='utf-8'))
        raw = data.get('tasks') if isinstance(data, dict) else data
        if not isinstance(raw, list) or not raw:
            raise ValueError('任务文件必须是非空数组或 {"tasks": [...]}')
    else:
        if not args.dir or not args.prompt:
            raise ValueError('至少提供 --dir 和 --prompt，或 --tasks')
        if len(args.prompt) not in (1, len(args.dir)):
            raise ValueError('--prompt 应为一个广播提示词，或与 --dir 数量一致')
        prompts = args.prompt * len(args.dir) if len(args.prompt) == 1 else args.prompt
        raw = [dict(dir=d, prompt=p) for d, p in zip(args.dir, prompts)]
        if len(args.session) > len(raw):
            raise ValueError('--session 数量不可超过任务数量')
        for task, sid in zip(raw, args.session):
            task['session'] = sid
    tasks, seen = [], set()
    for t in raw:
        if not isinstance(t, dict) or not isinstance(t.get('dir'), str) or not isinstance(t.get('prompt'), str) or not t['prompt'].strip():
            raise ValueError('每项需要字符串 dir 和非空 prompt')
        directory = Path(t['dir']).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError('工作目录不存在: ' + str(directory))
        sid = t.get('session')
        if sid and (not isinstance(sid, str) or not re.fullmatch(r'ses_[a-zA-Z0-9]+', sid)):
            raise ValueError('session ID 格式不正确')
        if sid and sid in seen:
            raise ValueError('同一批次不可同时调度相同 session')
        seen.add(sid)
        tasks.append(dict(dir=str(directory), prompt=t['prompt'],
                          title=str(t.get('title') or directory.name), session_id=sid))
    return tasks


def start(args):
    ensure_deveco()
    tasks = build_tasks(args)
    if args.max_parallel < 1 or args.timeout < 1:
        raise ValueError('并行度与执行超时必须大于 0')
    if args.max_parallel > MAX_PARALLEL:
        print('警告: 并行度已限制为 6', file=sys.stderr)
    if args.model and ('/' not in args.model or not all(args.model.split('/', 1))):
        raise ValueError('--model 需要 provider/model')
    rid = identifier('run_')
    p = state_home() / rid
    p.mkdir(mode=0o700)
    with connect(p) as db:
        db.executescript('''
            CREATE TABLE settings(data TEXT NOT NULL);
            CREATE TABLE tasks(id TEXT PRIMARY KEY, ordinal INTEGER, data TEXT NOT NULL);
            CREATE TABLE commands(id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, prompt TEXT,
                                  state TEXT NOT NULL, response TEXT);
        ''')
        db.execute('INSERT INTO settings VALUES (?)', (json.dumps(dict(
            run_id=rid, model=args.model, max_parallel=min(args.max_parallel, MAX_PARALLEL),
            timeout=args.timeout, created_at=time.time())),))
        for i, t in enumerate(tasks, 1):
            t.update(task_id='task_' + rid[4:] + '_' + str(i), status='queued',
                     actions={}, total_actions=0, tokens=0, final_result='', error=None,
                     model=args.model, model_source='explicit' if args.model else None,
                     round=0, history=[], pending=[t['prompt']], updated_at=time.time())
            db.execute('INSERT INTO tasks VALUES (?, ?, ?)',
                       (t['task_id'], i, json.dumps(t, ensure_ascii=False)))
    spawn_worker(p)
    return snapshot(rid)


def snapshot(value):
    p = run_path(value)
    with connect(p) as db:
        opts, tasks = settings(db), task_rows(db)
        commands = [dict(r) for r in db.execute(
            "SELECT id, task_id, kind, state, response FROM commands WHERE state='pending'")]
    alive = worker_alive(p)
    # A missing worker is not equivalent to a completed task. Allow a short boot window.
    if not alive and time.time() - opts['created_at'] > 5:
        for t in tasks:
            if t['status'] not in TERMINAL:
                t['status'] = 'orphaned'
                t['error'] = '后台 worker 已退出；执行是否停止未经确认，日志: ' + str(p / 'worker.log')
    if value.startswith('task_'):
        tasks = [t for t in tasks if t['task_id'] == value]
        if not tasks:
            raise ValueError('找不到 task: ' + value)
        commands = [c for c in commands if c['task_id'] == value]
    return dict(run_id=p.name, worker_alive=alive, log=str(p / 'worker.log'),
                summary=dict(total=len(tasks), ok=sum(t['status'] == 'ok' for t in tasks),
                             done=sum(t['status'] in TERMINAL for t in tasks),
                             parallel=min(opts['max_parallel'], len(tasks))),
                pending_commands=commands, tasks=tasks)


def finished(result):
    terminal = all(t['status'] in TERMINAL for t in result['tasks'])
    lost_worker = (not result['worker_alive'] and
                   any(t['status'] == 'orphaned' for t in result['tasks']))
    return terminal and (not result['pending_commands'] or lost_worker)


def list_tasks(active_only=False, recent=10, run_id=None):
    if recent < 0:
        raise ValueError('--recent 不可为负数')
    if run_id:
        if not re.fullmatch(r'run_[a-f0-9]{12}', run_id):
            raise ValueError('--run 需要 run_id')
        paths = [run_path(run_id)]
    else:
        paths = sorted(p for p in state_home().glob('run_*')
                       if re.fullmatch(r'run_[a-f0-9]{12}', p.name) and (p / 'state.db').is_file())
    groups = dict(active=[], attention=[], recent=[])
    errors = []
    for p in paths:
        try:
            result = snapshot(p.name)
            for task in result['tasks']:
                row = dict(task, run_id=p.name, worker_alive=result['worker_alive'])
                group = ('attention' if task['status'] == 'orphaned' else
                         'recent' if task['status'] in TERMINAL else 'active')
                groups[group].append(row)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as e:
            errors.append(dict(run_id=p.name, error=str(e)))
    groups['active'].sort(key=lambda t: (t['status'] == 'queued', -t['updated_at'], t['task_id']))
    for group in ('attention', 'recent'):
        groups[group].sort(key=lambda t: (-t['updated_at'], t['task_id']))
    total_recent = len(groups['recent'])
    groups['recent'] = groups['recent'][:recent] if not active_only else []
    if active_only:
        groups['attention'] = []
    return dict(listing=True, **groups, errors=errors,
                summary=dict(active=len(groups['active']), attention=len(groups['attention']),
                             recent=len(groups['recent']), total_recent=total_recent))


def relative_time(timestamp):
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return '%s秒前' % seconds
    if seconds < 3600:
        return '%s分钟前' % (seconds // 60)
    if seconds < 86400:
        return '%s小时前' % (seconds // 3600)
    return '%s天前' % (seconds // 86400)


def wait(value, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        result = snapshot(value)
        if finished(result):
            return result, 0 if all(t['status'] == 'ok' for t in result['tasks']) else 1
        if deadline is not None and time.monotonic() >= deadline:
            return result, 124
        time.sleep(.2)


def control(value, kind, prompt=''):
    if not value.startswith('task_'):
        raise ValueError('send/cancel 需要 task_id；整批 run 请逐任务操作')
    p = run_path(value)
    current = snapshot(value)['tasks'][0]
    if current['status'] == 'orphaned':
        raise ValueError(current['error'] + '；请先检查遗留进程，再用 --session 显式续跑')
    cid = identifier('cmd_')
    with connect(p) as db:
        db.execute('INSERT INTO commands VALUES (?, ?, ?, ?, ?, NULL)',
                   (cid, value, kind, prompt, 'pending'))
    deadline = time.monotonic() + 35
    while True:
        spawn_worker(p)
        with connect(p) as db:
            row = db.execute('SELECT state, response FROM commands WHERE id=?', (cid,)).fetchone()
        if row['state'] != 'pending':
            result = json.loads(row['response'])
            result['command_id'] = cid
            return result, 0 if row['state'] == 'done' else 1
        if time.monotonic() >= deadline:
            return dict(command_id=cid, task_id=value, status='pending',
                        error='控制指令尚未确认；用 status 检查 pending_commands，请勿重复发送'), 2
        time.sleep(.2)


def http_request(base, method, route, directory=None, body=None):
    if directory:
        route += ('&' if '?' in route else '?') + urllib.parse.urlencode({'directory': directory})
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if isinstance(base, tuple):
        base, password = base
        headers['Authorization'] = 'Basic ' + base64.b64encode(
            ('deveco:' + password).encode()).decode()
    return urllib.request.Request(base + route, data=data, method=method, headers=headers)


def request_json(base, method, route, directory=None, body=None, timeout=5):
    req = http_request(base, method, route, directory, body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError('DevEco HTTP %s: %s' % (e.code, e.read().decode(errors='replace')[-1000:])) from e


def terminate(proc):
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def launch_server(log):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    env = dict(os.environ, DEVECO_SERVER_PASSWORD=password, DEVECO_SERVER_USERNAME='deveco')
    proc = subprocess.Popen([ensure_deveco(), 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                            env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True)
    base = ('http://127.0.0.1:' + str(port), password)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('deveco serve 提前退出: ' + str(proc.returncode))
            try:
                doc = request_json(base, 'GET', '/doc', timeout=1)
                paths = doc.get('paths', {})
                needed = ['/session', '/session/status', '/session/{sessionID}/prompt_async',
                          '/session/{sessionID}/abort', '/session/{sessionID}/message', '/event']
                if not all(k in paths for k in needed):
                    raise ValueError('DevEco 缺少异步调度 API；请升级 CLI')
                return proc, base
            except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError):
                time.sleep(.2)
        raise RuntimeError('deveco serve 30 秒内未就绪')
    except BaseException:
        terminate(proc)
        raise


class SessionEvents:
    """Subscribe before submission: early prompt failures have no message to poll.

    Each turn has its own subscription so late abort errors from a previous turn
    cannot contaminate its replacement. Only the reader owns the HTTP response;
    close asks it to exit on the next event/heartbeat (or its 30-second timeout).
    """
    def __init__(self, base, directory, session_id):
        self.ready, self.stopping = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.connected, self.error, self.failure = False, None, None
        self.thread = threading.Thread(target=self.listen, args=(base, directory, session_id), daemon=True)
        self.thread.start()
        self.ready.wait(5)
        if not self.connected:
            self.close()
            raise RuntimeError('DevEco 事件流未就绪: ' + (self.failure or '5 秒内未收到 server.connected'))

    def listen(self, base, directory, session_id):
        try:
            req = http_request(base, 'GET', '/event', directory)
            req.add_header('Accept', 'text/event-stream')
            with urllib.request.urlopen(req, timeout=30) as stream:
                data = []
                size = 0
                while not self.stopping.is_set():
                    line = stream.readline(1024 * 1024)
                    if not line:
                        raise RuntimeError('连接已断开')
                    size += len(line)
                    if size > 1024 * 1024:
                        raise RuntimeError('事件超过 1 MiB')
                    line = line.rstrip(b'\r\n')
                    if line.startswith(b'data:'):
                        data.append(line[5:].lstrip(b' '))
                    elif not line:
                        if data:
                            event = json.loads(b'\n'.join(data))
                            with self.lock:
                                if event.get('type') == 'server.connected':
                                    self.connected = True
                                    self.ready.set()
                                props = event.get('properties') or {}
                                if event.get('type') == 'session.error' and props.get('sessionID') == session_id:
                                    self.error = props.get('error') or {'name': 'UnknownError'}
                        data, size = [], 0
        except Exception as e:
            with self.lock:
                self.failure = str(e)
        finally:
            self.ready.set()

    def get_error(self):
        with self.lock:
            if self.error:
                return self.error
            if self.failure:
                raise RuntimeError('DevEco 事件流失效，无法可靠接收执行错误: ' + self.failure)
        return None

    def close(self):
        self.stopping.set()


def model_error_hint(error):
    if error and any(name in str(error) for name in (
            'ProviderNoProvidersError', 'ProviderModelNotFoundError', 'ProviderNotFoundError', 'ModelNotFoundError')):
        return ('使用 de-run models --dir <任务目录> 查询已连接模型，再以 --model provider/model 显式启动；'
                '也可检查 DevEco 的默认模型和登录配置。')
    return None


def round_result(messages, message_id):
    """Only count assistant messages belonging to this particular user turn."""
    replies = [m for m in messages if m.get('info', {}).get('role') == 'assistant'
               and m['info'].get('parentID') == message_id]
    actions, texts, tokens, error = Counter(), [], 0, None
    for m in replies:
        info = m['info']
        tk = info.get('tokens') or {}
        total = tk.get('total')
        if total is None:
            total = sum(tk.get(k, 0) or 0 for k in ('input', 'output', 'reasoning'))
            total += sum((tk.get('cache') or {}).values())
        tokens += total
        error = info.get('error') or error
        for part in m.get('parts', []):
            if part.get('type') == 'tool':
                actions[part.get('tool') or '?'] += 1
            elif part.get('type') == 'text' and part.get('text'):
                texts.append(part['text'])
    last = replies[-1]['info'] if replies else {}
    return dict(actions=dict(actions), total_actions=sum(actions.values()), tokens=tokens,
                final_result='\n\n'.join(texts).strip(), error=error,
                finish=last.get('finish'), completed=bool((last.get('time') or {}).get('completed')))


class Runner:
    def __init__(self, p, db, base, opts):
        self.p, self.db, self.base, self.opts = p, db, base, opts
        self.locks = {}
        self.events = {}
        self.catalogs = {}
        self.stopping = False

    def api(self, t, method, route, body=None, timeout=5):
        return request_json(self.base, method, route, t['dir'], body, timeout)

    def acquire_session(self, t):
        if t['task_id'] in self.locks:
            return
        directory = state_home() / 'sessions'
        directory.mkdir(exist_ok=True, mode=0o700)
        key = hashlib.sha256(t['session_id'].encode()).hexdigest()
        f = open(directory / (key + '.lock'), 'a')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise RuntimeError('该 session 正被其他 de-run 任务使用')
        self.locks[t['task_id']] = f

    def release_session(self, t):
        events = self.events.pop(t['task_id'], None)
        if events:
            events.close()
        f = self.locks.pop(t['task_id'], None)
        if f:
            f.close()

    def persist(self, t):
        t['error_hint'] = model_error_hint(t.get('error'))
        save_task(self.db, t)

    def record_round(self, t, status):
        t['error_hint'] = model_error_hint(t.get('error'))
        if t.get('message_id'):
            t['history'].append({k: t.get(k) for k in (
                'round', 'message_id', 'prompt', 'actions', 'total_actions', 'tokens',
                'final_result', 'error', 'error_hint', 'model', 'model_source', 'finish', 'started_at')})
            t['history'][-1].update(status=status, ended_at=time.time())
            t['message_id'] = None

    def refresh(self, t):
        messages = self.api(t, 'GET', '/session/' + t['session_id'] + '/message')
        t.update(round_result(messages, t['message_id']))

    def abort(self, t):
        """Do not send the replacement until DevEco acknowledges abort and becomes idle."""
        if not t.get('session_id'):
            return
        t['status'] = 'interrupting'
        self.persist(t)
        answer = self.api(t, 'POST', '/session/' + t['session_id'] + '/abort', {})
        if answer is not True:
            raise RuntimeError('DevEco 未确认 abort；没有发送新指令')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            statuses = self.api(t, 'GET', '/session/status', timeout=2)
            if statuses.get(t['session_id'], {}).get('type', 'idle') == 'idle':
                if t.get('message_id'):
                    self.refresh(t)
                return
            time.sleep(.1)
        raise RuntimeError('abort 后 session 仍未空闲；没有发送新指令')

    def prepare(self, t):
        if not t.get('session_id'):
            session = self.api(t, 'POST', '/session', {
                'title': t['title'],
                'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})
            t['session_id'] = session['id']
            self.persist(t)
        self.acquire_session(t)
        session = self.api(t, 'GET', '/session/' + t['session_id'])
        if Path(session['directory']).resolve() != Path(t['dir']).resolve():
            raise RuntimeError('--dir 与 session 原属目录不一致: ' + session['directory'])
        statuses = self.api(t, 'GET', '/session/status')
        if statuses.get(t['session_id'], {}).get('type', 'idle') != 'idle':
            raise RuntimeError('session 已在 DevEco 中运行，拒绝覆盖外部任务')
        return session

    def resolve_model(self, t, session):
        if self.opts.get('model'):
            return self.opts['model'], 'explicit'
        if t['dir'] not in self.catalogs:
            self.catalogs[t['dir']] = model_catalog(self.base, t['dir'])
        models, config, agents = self.catalogs[t['dir']]
        available = {row['model'] for row in models}
        previous = next((candidate for candidate in (t.get('model'), session.get('model'))
                         if model_name(candidate) in available), None)
        if not previous:
            messages = self.api(t, 'GET', '/session/' + t['session_id'] + '/message')
            for message in reversed(messages):
                info = message.get('info') or {}
                previous = info.get('model') if info.get('role') == 'user' else info
                if model_name(previous) in available:
                    break
        return choose_model(models, config, agents, previous)

    def dispatch(self, t):
        session = self.prepare(t)
        model, source = self.resolve_model(t, session)
        self.events[t['task_id']] = SessionEvents(self.base, t['dir'], t['session_id'])
        t['prompt'] = t['pending'].pop(0)
        t.update(status='running', round=t['round'] + 1,
                 message_id=message_identifier(), started_at=time.time(),
                 actions={}, total_actions=0, tokens=0, final_result='', error=None,
                 finish=None, completed=False, idle_since=None, model=model, model_source=source)
        self.persist(t)  # Persist identity before the HTTP side effect.
        body = dict(messageID=t['message_id'], parts=[{'type': 'text', 'text': t['prompt']}])
        provider, name = model.split('/', 1)
        body['model'] = dict(providerID=provider, modelID=name)
        self.api(t, 'POST', '/session/' + t['session_id'] + '/prompt_async', body)

    def poll(self, t):
        statuses = self.api(t, 'GET', '/session/status')
        state = statuses.get(t['session_id'], {}).get('type', 'idle')
        # Read messages after observing idle, so a stale tool step cannot hide the final reply.
        self.refresh(t)
        if not t['error']:
            t['error'] = self.events[t['task_id']].get_error()
        t['upstream_status'] = state
        if t['error']:
            error = t['error']
            if state != 'idle':
                self.abort(t)
            t.update(status='failed', error=error)
        elif time.time() - t['started_at'] >= self.opts['timeout']:
            self.abort(t)
            t.update(status='timeout', error='超过每轮执行时限，已确认中止')
        elif state == 'idle' and t['completed']:
            if t['finish'] == 'stop' and t['final_result']:
                t['status'] = 'ok'
            else:
                t.update(status='incomplete', error='执行结束但没有完整最终报告 (finish=%s)' % t['finish'])
        elif state == 'idle':
            t['idle_since'] = t.get('idle_since') or time.time()
            if time.time() - t['idle_since'] >= IDLE_GRACE:
                self.abort(t)
                t.update(status='incomplete', error='DevEco 持续空闲 %s 秒但没有完整回复，已确认中止；'
                         '请检查默认模型配置或 DevEco 日志' % IDLE_GRACE)
        else:
            t['idle_since'] = None
        if t['status'] in TERMINAL:
            self.record_round(t, t['status'])
            if t['pending'] and t['status'] == 'ok':
                t['status'] = 'queued'
            self.release_session(t)
        self.persist(t)

    def commands(self):
        rows = self.db.execute("SELECT * FROM commands WHERE state='pending' ORDER BY rowid").fetchall()
        for c in rows:
            t = json.loads(self.db.execute('SELECT data FROM tasks WHERE id=?', (c['task_id'],)).fetchone()[0])
            try:
                was_running = t['status'] == 'running'
                if t['status'] in ('orphaned', 'interrupting'):
                    raise RuntimeError('执行状态不确定，拒绝继续；请检查 worker.log')
                if c['kind'] == 'queue':
                    t['pending'].append(c['prompt'])
                    if t['status'] in TERMINAL:
                        t['status'] = 'queued'
                else:
                    if t['status'] == 'running':
                        self.abort(t)
                        self.record_round(t, 'interrupted' if c['kind'] == 'send' else 'cancelled')
                    t['pending'] = [c['prompt']] if c['kind'] == 'send' else []
                    t['status'] = 'queued' if c['kind'] == 'send' else 'cancelled'
                    t['error'] = None
                    self.release_session(t)
                self.persist(t)
                response = dict(task_id=t['task_id'], session_id=t['session_id'],
                                status=t['status'], interrupted=c['kind'] == 'send' and was_running,
                                queued=len(t['pending']))
                outcome = 'done'
            except Exception as e:
                # Never pretend interruption succeeded. Keep uncertainty visible.
                if t['status'] == 'interrupting':
                    t.update(status='orphaned', error=str(e))
                    self.persist(t)
                response, outcome = dict(task_id=t['task_id'], status=t['status'], error=str(e)), 'failed'
            with self.db:
                self.db.execute('UPDATE commands SET state=?, response=? WHERE id=?',
                                (outcome, json.dumps(response, ensure_ascii=False), c['id']))

    def loop(self, proc):
        while not self.stopping:
            if proc.poll() is not None:
                raise RuntimeError('deveco serve 意外退出: ' + str(proc.returncode))
            self.commands()
            tasks = task_rows(self.db)
            for t in tasks:
                if t['status'] == 'running':
                    try:
                        self.poll(t)
                    except Exception as e:
                        # Communication failures can leave remote work active; stop before reporting failure.
                        try:
                            self.abort(t)
                            t.update(status='failed', error=str(e))
                            self.record_round(t, 'failed')
                        except Exception as abort_error:
                            t.update(status='orphaned', error='%s; 中止未确认: %s' % (e, abort_error))
                        if t['status'] != 'orphaned':
                            self.release_session(t)
                        self.persist(t)
            tasks = task_rows(self.db)
            slots = self.opts['max_parallel'] - sum(t['status'] in ('running', 'interrupting') for t in tasks)
            for t in tasks:
                if self.stopping:
                    break
                if slots > 0 and t['status'] == 'queued' and t['pending']:
                    try:
                        self.dispatch(t)
                        slots -= 1
                    except Exception as e:
                        if t['status'] == 'running':
                            try:
                                self.abort(t)
                            except Exception as abort_error:
                                t.update(status='orphaned', error='%s; 中止未确认: %s' % (e, abort_error))
                        if t['status'] != 'orphaned':
                            t.update(status='failed', error=str(e))
                        if t['status'] != 'orphaned':
                            self.release_session(t)
                        self.persist(t)
            tasks = task_rows(self.db)
            pending = self.db.execute("SELECT count(*) FROM commands WHERE state='pending'").fetchone()[0]
            if all(t['status'] in TERMINAL for t in tasks) and not pending:
                return
            time.sleep(.2)


def worker(rid):
    p = run_path(rid)
    with open(p / 'worker.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        db, proc, runner = connect(p), None, None
        try:
            opts = settings(db)
            opts['worker_pid'] = os.getpid()
            with db:
                db.execute('UPDATE settings SET data=?', (json.dumps(opts),))
            # Restarting a worker must never replay an uncertain previous HTTP submission.
            for t in task_rows(db):
                if t['status'] in ('running', 'interrupting'):
                    t.update(status='orphaned', error='上次 worker 异常退出；请检查遗留执行后显式续跑')
                    save_task(db, t)
            with open(p / 'serve.log', 'ab', buffering=0) as log:
                proc, base = launch_server(log)
                runner = Runner(p, db, base, settings(db))
                def stop(signum, frame):
                    runner.stopping = True
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                runner.loop(proc)
        except Exception as e:
            print('worker error:', repr(e), flush=True)
            traceback.print_exc()
            for t in task_rows(db):
                if t['status'] not in TERMINAL:
                    t.update(status='orphaned' if t['status'] in ('running', 'interrupting') else 'failed', error=str(e))
                    save_task(db, t)
            with db:
                db.execute("UPDATE commands SET state='failed', response=? WHERE state='pending'",
                           (json.dumps(dict(error=str(e))),))
        finally:
            if runner:
                for t in task_rows(db):
                    if t['status'] in ('running', 'interrupting', 'orphaned') and t['task_id'] in runner.locks:
                        try:
                            runner.abort(t)
                            t.update(status='cancelled', error='worker 停止，已确认中止')
                        except Exception as e:
                            t.update(status='orphaned', error='worker 停止，中止未确认: ' + str(e))
                        runner.persist(t)
            terminate(proc)
            if runner:
                for t in task_rows(db):
                    runner.release_session(t)
            db.close()
    return 0


def model_name(value):
    if isinstance(value, dict):
        provider, name = value.get('providerID'), value.get('modelID') or value.get('id')
        return provider + '/' + name if provider and name else None
    return value if isinstance(value, str) and '/' in value else None


def model_catalog(base, directory):
    # Project/plugin initialization can exceed the normal five-second poll limit.
    def read(route):
        try:
            return request_json(base, 'GET', route, directory, timeout=30)
        except (TimeoutError, socket.timeout) as e:
            raise RuntimeError('DevEco 模型配置查询超时（30 秒）: ' + route) from e
    catalog = read('/provider')
    connected = set(catalog['connected'])
    models = [dict(model=p['id'] + '/' + mid, name=info.get('name') or mid,
                   provider_default=catalog.get('default', {}).get(p['id']) == mid)
              for p in catalog['all'] if p['id'] in connected
              for mid, info in p.get('models', {}).items()]
    return (sorted(models, key=lambda x: x['model']),
            read('/config'), read('/agent'))


def choose_model(models, config, agents, previous=None):
    """Use only connected catalog entries; explicit CLI overrides bypass this policy."""
    available = {row['model'] for row in models}
    if not available:
        raise RuntimeError('没有已连接的可用模型；请运行 deveco auth login 并检查 provider 配置')
    agent = next((a for a in agents if a['name'] == config.get('default_agent', 'build')), {})
    for candidate, source in ((previous, 'session'), (agent.get('model'), 'agent'),
                              (config.get('model'), 'config')):
        name = model_name(candidate)
        if name in available:
            return name, source
    defaults = [row['model'] for row in models if row['provider_default']]
    if defaults:
        return min(defaults, key=lambda name: (not name.startswith('deveco/'), name)), 'provider_default'
    return min(available, key=lambda name: (not name.startswith('deveco/'), name)), 'first_available'


def list_models(directory):
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError('工作目录不存在: ' + str(directory))
    with tempfile.TemporaryFile() as log:
        proc, base = launch_server(log)
        try:
            models, config, agents = model_catalog(base, str(directory))
            model, source = choose_model(models, config, agents) if models else (None, None)
            return dict(dir=str(directory), models=models, default_model=model, default_source=source)
        finally:
            terminate(proc)


def list_sessions(n):
    executable = ensure_deveco()
    path = Path('~/.local/share/deveco/deveco.db').expanduser()
    try:
        result = subprocess.run([executable, 'db', 'path'], capture_output=True, text=True, timeout=30)
        candidate = Path(result.stdout.strip())
        if result.returncode == 0 and candidate.is_file():
            path = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not path.is_file():
        raise ValueError('找不到 DevEco 数据库: ' + str(path))
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT id,title,directory,time_updated,model FROM session '
                          'WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT ?',
                          (max(0, min(n, 10000)),)).fetchall()
    return [dict(session_id=r[0], title=r[1], dir=r[2], updated_at=r[3], model=r[4]) for r in rows]


def emit(value, as_json=False, truncate=None, verbose=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, list):
        for row in value:
            print(json.dumps(row, ensure_ascii=False))
    elif 'models' in value:
        print('现有模型列表（%s）' % value['dir'])
        if not value['models']:
            print('  没有可用模型；请检查 deveco auth login 和 provider 配置')
        for row in sorted(value['models'], key=lambda x: (x['model'] != value['default_model'], x['model'])):
            print('  %s%s' % (row['model'], '（默认）' if row['model'] == value['default_model'] else ''))
    elif value.get('listing'):
        count = sum(len(value[k]) for k in ('active', 'attention', 'recent'))
        if not count:
            print('没有符合条件的 de-run 任务')
        for key, title in (('active', '活跃任务'), ('attention', '待处理异常'), ('recent', '最近结束')):
            if not value[key]:
                continue
            print('%s（%s）' % (title, len(value[key])))
            print('  task_id                 状态          标题  最近更新  run_id')
            for t in value[key]:
                title = ' '.join(t['title'].split())
                if len(title) > 40:
                    title = title[:39] + '…'
                print('  %s  %-12s  %s  %s  %s' % (
                    t['task_id'], t['status'], title, relative_time(t['updated_at']), t['run_id']))
                if verbose:
                    print('    session=%s  dir=%s' % (t.get('session_id') or '-', t['dir']))
        for error in value['errors']:
            print('警告: 无法读取 %s: %s' % (error['run_id'], error['error']), file=sys.stderr)
    elif 'tasks' in value:
        print('%s · 完成 %s/%s · 成功 %s' % (value['run_id'], value['summary']['done'],
                                          value['summary']['total'], value['summary']['ok']))
        for t in value['tasks']:
            print('  %s  %s  %s' % (t['task_id'], t['status'], t['title']))
            if t.get('model'):
                print('    model=%s (%s)' % (t['model'], t.get('model_source') or 'session'))
            if verbose:
                print('    session=%s' % (t['session_id'] or '-'))
            text = t.get('error') or t.get('final_result')
            if text:
                if isinstance(text, dict) and not verbose:
                    text = str((text.get('data') or {}).get('message') or text.get('name') or text).splitlines()[0]
                text = str(text)
                print('    ' + (text[:truncate] + '…' if truncate and len(text) > truncate else text))
            if t.get('error_hint'):
                print('    ' + t['error_hint'])
        if value.get('pending_commands'):
            print('  尚有 %s 条控制指令等待确认' % len(value['pending_commands']))
    else:
        display = value if verbose else {k: v for k, v in value.items() if k != 'session_id'}
        print(json.dumps(display, ensure_ascii=False))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '_worker':
        return worker(argv[1])
    if not argv or argv in (['--help'], ['-h']):
        print(HELP)
        try:
            emit(list_models(os.getcwd()))
        except (ValueError, OSError, RuntimeError) as e:
            print('模型列表获取失败: ' + str(e), file=sys.stderr)
            return 2
        return 0
    command = argv.pop(0) if argv[0] in ('start', 'run', 'list', 'models', 'status', 'wait', 'send', 'cancel', 'result') else 'start'
    parser = argparse.ArgumentParser(prog='de-run ' + command, description=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--truncate', type=int)
    parser.add_argument('--verbose', action='store_true', help='显示底层 session ID 等详细信息')
    if command == 'start' or command == 'run':
        parser.add_argument('--dir', action='append', default=[])
        parser.add_argument('--prompt', action='append', default=[])
        parser.add_argument('--session', action='append', default=[])
        parser.add_argument('--sessions', nargs='?', const=15, type=int)
        parser.add_argument('--tasks')
        parser.add_argument('--model')
        parser.add_argument('--max-parallel', type=int, default=6)
        parser.add_argument('--timeout', type=int, default=900)
        parser.add_argument('--wait', action='store_true')
    elif command == 'models':
        parser.add_argument('--dir', default=os.getcwd())
    elif command == 'list':
        parser.add_argument('--active', action='store_true')
        parser.add_argument('--recent', type=int, default=10)
        parser.add_argument('--run')
    else:
        parser.add_argument('id')
        if command == 'send':
            parser.add_argument('text', nargs='?')
            parser.add_argument('--prompt')
            parser.add_argument('--queue', action='store_true')
        if command == 'wait':
            parser.add_argument('--timeout', type=float)
    args = parser.parse_args(argv)
    try:
        code = 0
        if command in ('start', 'run'):
            if args.sessions is not None:
                result = list_sessions(args.sessions)
            else:
                result = start(args)
                if args.wait:
                    print('已启动 ' + result['run_id'], file=sys.stderr)
                    result, code = wait(result['run_id'])
        elif command == 'models':
            result = list_models(args.dir)
        elif command == 'list':
            result = list_tasks(args.active, args.recent, args.run)
            code = 1 if result['errors'] else 0
        elif command in ('status', 'result'):
            result = snapshot(args.id)
            if command == 'result':
                code = (0 if all(t['status'] == 'ok' for t in result['tasks']) else 1) if finished(result) else 2
        elif command == 'wait':
            if args.timeout is not None and args.timeout < 0:
                raise ValueError('等待超时不可为负数')
            result, code = wait(args.id, args.timeout)
        else:
            text = (args.text or args.prompt) if command == 'send' else ''
            if command == 'send' and (not text or not text.strip()):
                raise ValueError('send 需要非空指令文本')
            result, code = control(args.id, 'queue' if command == 'send' and args.queue else command, text)
        emit(result, args.json, args.truncate, args.verbose)
        return code
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as e:
        if args.json:
            emit(dict(error=str(e)), True)
        else:
            print('错误: ' + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('停止等待；后台任务继续运行。使用 cancel TASK_ID 停止任务。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
