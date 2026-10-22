> **English** | [简体中文](./README.md)

# de-run — interruptible DevEco subagent control

A single Python 3.9+ file, with no third-party dependencies, connecting any command-capable parent agent to DevEco Code.

## Quick start

Install DevEco Code, complete `deveco auth login`, and make `deveco` available on PATH. The implementation was checked against the actual OpenAPI of DevEco **0.2.4**; required routes are checked at startup. Process management currently supports macOS/Linux, not Windows. Model availability and quotas depend on DevEco and your account.

```bash
python3 scripts/de-run.py --help
ln -s "$(pwd)/scripts/de-run.py" ~/.local/bin/de-run

# start/run/no subcommand are equivalent: return immediately
de-run --dir /path/to/project --prompt 'Analyze the architecture; cite files' --json

# Discover managed tasks
de-run list
de-run list --active
de-run list --recent 30
de-run list --run <run_id> --json

# Replace these placeholders with IDs from the start receipt
de-run status <task_id> --json
de-run wait <run_id> --timeout 60 --json
de-run result <task_id> --json

# Interrupt the current turn and continue in the same session
de-run send <task_id> 'Prioritize database connection leaks' --json

# Explicitly append without interruption
de-run send <task_id> 'Then check test coverage' --queue --json

de-run cancel <task_id> --json

# Blocking compatibility with older integrations
de-run --dir /path/to/project --prompt 'Analyze' --wait --json
```

**Breaking change:** execution is asynchronous by default. Existing callers expecting a final report must add `--wait` or use `wait` / `result` with the returned IDs.

## Control semantics

| Command | Behavior |
|---|---|
| `start` / `run` / default | Persist a batch, spawn a detached worker, return run/task IDs |
| `list` | All active tasks, all orphaned tasks, and the latest 10 finished tasks; filter with `--active`, `--recent N`, or `--run RUN_ID` |
| `status ID` | Read one task or an entire batch, including progress and latest text |
| `wait ID` | Block for completion; its timeout does not cancel execution |
| `send TASK TEXT` | Abort the active turn, confirm idle, enqueue replacement in the same session |
| `send TASK TEXT --queue` | Preserve the active turn and append another |
| `cancel TASK` | Clear pending prompts and confirm that active work has stopped |
| `result ID` | Read durable reports and turn history; exit 2 if not ready |
| `--sessions [N]` | Read historical sessions from DevEco SQLite |

Human output emphasizes task IDs and hides session IDs by default. Use `--verbose` for session details; JSON retains the complete mapping. `--sessions` still shows session IDs because it explicitly queries DevEco history.

`list` reads only de-run storage without starting DevEco. Active and orphaned tasks are never capped by the recent-history limit. Running/interrupting tasks precede queued tasks; each group otherwise uses most recently updated first. Lost workers are classified using the same rules as `status`. Human lists omit full prompts/reports; JSON retains task data. Unreadable batches appear in `errors`, with remaining results preserved and exit code 1.

`send` acknowledges the control request, not model completion. A `queued` response means the prompt is persisted and subject to batch concurrency limits. Sending to a completed task starts another worker as needed and resumes its session.

Interruption calls `POST /session/:id/abort`, requires a positive acknowledgment, checks that the session is idle, archives the interrupted turn, then uses `prompt_async`. If stopping cannot be confirmed, the replacement is not submitted. **Interruption does not undo file edits or external effects.** Independently detached tool processes may outlive DevEco's cancellation.

Queued follow-ups automatically run only after a successful turn. After failure, pending text remains visible for inspection; explicitly `send` your recovery decision.

## Batch and model options

```bash
de-run --dir /path/A --prompt 'Analyze A' --dir /path/B --prompt 'Analyze B'
de-run --dir /path/A --dir /path/B --prompt 'Read-only review with references'
de-run --tasks examples/tasks.example.json --max-parallel 3 --json
de-run --dir /path/A --session ses_example --prompt 'Continue'
de-run --dir /path/A --prompt 'Analyze' --model provider/model
```

Task JSON is an array or `{"tasks": [...]}`; each item supports `dir`, `prompt`, optional `title` and `session`. Resumed session directories must match. Without `--model`, DevEco's default/session configuration is used.

- `--max-parallel N`: per-batch limit, default and maximum 6; not an account-wide rate limiter.
- Startup `--timeout SEC`: execution deadline per turn, default 900; actively aborts on expiry.
- Wait `--timeout SEC`: only limits waiting; omitted means wait indefinitely.
- `--truncate N`: shortens human output only; stored data and JSON remain complete.

## Results and exit codes

Tasks progress through `queued`, `running`, and terminal `ok`, `failed`, `incomplete`, `timeout`, or `cancelled`. `interrupting` denotes cancellation in progress. `orphaned` denotes worker loss or unconfirmed execution state; inspect logs and leftover processes before explicitly resuming with `--session`. Uncertain submissions are never automatically replayed.

JSON includes `run_id`, `worker_alive`, `summary`, `pending_commands`, and `tasks`. Task records include IDs, status, round, actions, tokens, latest text, errors, pending prompts, and per-turn history. Current metrics belong to the current/latest turn; completed and interrupted turns remain in `history`. Only error-free `stop` with final text counts as `ok`; a length cutoff is `incomplete`.

| Code | Meaning |
|---|---|
| 0 | Request succeeded; wait/result require all selected tasks to succeed |
| 1 | Failed/cancelled/incomplete/timed-out task, or failed control request |
| 2 | Invalid input/environment, result not ready, or control not yet acknowledged |
| 124 | Wait deadline expired; task continues |
| 130 | Waiting interrupted by user; task continues |

## Architecture and limitations

Each batch has a detached worker, a private DevEco serve process, SQLite task/command/history storage, and ordered control commands. The worker polls session state/messages; it does not depend on CLI attach. Services are reclaimed when all batch tasks finish. Each server binds to 127.0.0.1 with a per-process random password held only in worker memory and the server environment.

Worker and session file locks coordinate de-run processes. Other DevEco clients can bypass these locks. No automatic worktree creation or file-write isolation is provided; callers must coordinate concurrent edits. New sessions automatically allow tools, matching the former `--auto` behavior; existing sessions retain their permissions. Prompts cannot enforce read-only execution.

`DE_RUN_HOME` selects storage, default `~/.local/share/de-run`. It contains prompts and reports; manage it as project data. Logs are `<home>/<run_id>/worker.log` and `serve.log`. State survives the caller exiting, but automatic machine-reboot recovery is not provided. Killing a worker forcibly may leave DevEco/tool processes running. Multiple batches share account quotas; automatic rate limiting/retries are not implemented.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Local HTTP fixtures exercise real CLI processes, asynchronous startup, interruption, queued follow-ups, cancellation, timeouts, failure classification, session continuation across workers, and persistence without using model quotas.

MIT. Underlying models and HarmonyOS tooling are provided by DevEco Code / DevEco Studio.
