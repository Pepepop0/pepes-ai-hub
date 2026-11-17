# Operating the Hub as a coding agent

The `hub run` subcommand runs the real Hub — the same `DesktopController` the
window uses — without a window, with JSON in and out. It goes through the
controller's allowlist and checks (path validation, delete confirmation, stop
consent, busy refusal). Session export/SSH transfer are explicit CLI-only tools.

Entry points (all equivalent):

```powershell
.\.venv\Scripts\python.exe -m local_ai_stack hub <subcommand>   # checkout
local-ai-stack hub <subcommand>                                 # installed script
.\PepitosAIHub-CLI.exe hub <subcommand>                         # built bundle
```

Linux Capella bench: `bash scripts/capella-linux.sh prepare`, then `check` and
`install` with a dedicated absolute root. It delegates installation to
`hub run install`; Windows UI and Linux CLI do not maintain different workflows.
See [Linux runbook](testing/runbook-linux-1.17.md) for prerequisites and SSH access.
`bash scripts/capella-linux.sh trace ROOT` follows the image chat filter's turn
trace (`ROOT/data/open-webui/pepito-trace/turns.jsonl`, across its rotation) live,
one line per record: local time, chat and turn ids, event and its key fields
(router source/timeouts, GPU waits, `ollama ps` placement, image job parameters,
tokens/s). It works over SSH; `TRACE_LINES=N` sets the history shown first.
Capella (`runtime.quiet_background_tasks`) also switches Open WebUI's follow-up
and tag generation off on every start (title generation is kept): two fewer 27B
calls after each answer.

Session evidence (not a whole installation backup):

```bash
local-ai-stack hub export-session --root /mnt/capella --output /mnt/session.zip
local-ai-stack hub fetch-session --host root@verified-host --port 2222 --bundle /mnt/bench --root /mnt/capella --output session.zip
```

`fetch-session` requires an existing verified OpenSSH host key and noninteractive
authentication; it never enrolls or disables host-key checking. Remote paths use
ASCII letters, digits, `/`, `.`, `_` and `-` only, protecting legacy SCP clients.
The transfer verifies whole-ZIP SHA256/size and each manifest member before
publishing a new local file; it never overwrites existing evidence. Exports omit
authentication tables, raw WebUI databases and secret configuration. The turn
trace (`turns.jsonl` and `turns.jsonl.1`) is included, redacted record by
record like the logs. User conversations/images remain private evidence. Capture
before deleting a pod; only an actually attached persistent volume survives
according to provider policy.

## Isolate state first

Never test against the real user state. Point the Hub at a throwaway root:

```powershell
$env:PEPITOS_AI_HUB_ROOT = "$env:TEMP\hub-agent-test"
```

`PEPITOS_AI_HUB_ROOT` moves the default installation, the Hub's state
(`<root>/state`: selection file, installation history) and the single-writer
lock. Installations in custom folders stay where their paths say; use paths
under the same temp folder. `PEPITOS_AI_HUB_RESOURCES` selects another
resource root (profiles, manifests, `desktop-ui/dist`). Delete the temp root
when done.

## Subcommands

| Subcommand | What it does |
| --- | --- |
| `hub state [--json]` | Prints `DesktopController.state()` as JSON (JSON is the default; `--json` is accepted for explicitness). |
| `hub commands` | Lists every allowlisted command with `required`/`optional` args, `read_only`, `busy_safe`, and `cli` (false plus `unavailable_reason` for commands the CLI cannot run). Derived from the controller's allowlist. |
| `hub run <command> [--args JSON] [--wait] [--timeout S]` | Runs one command. `--args` is a JSON object; omit it for commands without arguments. |
| `hub serve [--no-open]` | Browser dev mode: serves the UI on `127.0.0.1` and prints its URL. `--no-open` skips launching a browser. |

Not available from the CLI: `choose_folder` (native dialog, pass absolute paths instead), `install_git` (machine software requiring confirmation), `resolve_conflicts`, `cancel_pending`, `pause_installation`, `resume_installation`, `cancel_installation`, and `retry_installation`. These session controls use state held by the process that started the installation. Use the window or `hub serve`; a separate CLI process cannot control that worker. These commands exit 2 with code `interactive_only`.

`plan`, `estimate`, and `install` accept `create_subfolder`. In custom mode it defaults to `true`: `custom_root` is the parent and the final folder uses the profile name, such as `Capella`. Pass `false` when `custom_root` already names the exact installation folder. Default and portable modes are unchanged.

`download_auth {"profile_id":"capella"}` reports source-level token requirements and local credential presence. It is read-only and busy-safe; it does not validate an account online, hash models, expose credentials, or require an active installation. Current Hugging Face files are public; Civitai sources require authenticated access. Use the desktop credential modal to validate and save tokens.

## Output and exit codes

`hub run` prints JSON Lines (one compact JSON document per line, ASCII-escaped):

- success: `{"ok": true, "result": ...}`
- failure: `{"ok": false, "error": "...", "code": "...", "details": ...}` —
  the same body the window's HTTP API returns; `details` is present only when
  the controller attaches data (e.g. `conflict_required`, `stop_required`).

| Exit | Meaning |
| --- | --- |
| 0 | ok; with `--wait`, the operation succeeded |
| 1 | the Hub refused or failed (`desktop_running`, `busy`, `confirmation_required`, `stop_required`, `refused`, ...); with `--wait`, the operation ended `failed`, or `timeout` |
| 2 | usage: `unknown_command`, `invalid_args` (bad JSON, not an object, missing/unexpected keys), `interactive_only`; argparse errors also exit 2 |

Commands that start work (`install`, `start`, `stop`, `restart`, `repair`,
`delete`, and `switch`/`register`/`relocate` with `"stop": true` when a stop
is needed) return `{"operation_id": ...}`. The operation runs inside the CLI
process, so the process exits only after it ends, with or without `--wait`.

With `--wait`, after the result line each `OperationEvent` of that operation
is printed as one JSON line (`operation_id`, `sequence`, `kind`, `state`,
`phase`, `message`, `progress`, `transfer`, `activity`, `detail`,
`diagnostic`, `result`). The last line has `"kind": "result"` and `state`
`succeeded` or `failed`; failures carry `result.diagnostics`. `--timeout S`
(requires `--wait`) prints a `timeout` error line after S seconds and exits 1
once the operation has ended; the Hub never abandons an operation midway.

## Examples

PowerShell 7.3+ and bash pass the single-quoted JSON as is. In Windows
PowerShell 5.1, escape inner quotes: `--args '{\"root\":\"C:\\x\"}'`.

```bash
hub state
hub commands
hub run plan --args '{"profile_id":"pepe","mode":"custom","custom_root":"C:/Temp/hub-agent-test/a","create_subfolder":false}'
hub run estimate --args '{"profile_id":"pepe","mode":"default"}'
hub run install --args '{"profile_id":"pepe","mode":"custom","custom_root":"C:/Temp/hub-agent-test/a","create_subfolder":false}' --wait
hub run register --args '{"root":"C:/Temp/hub-agent-test/existing"}'
hub run relocate --args '{"old_root":"C:/Temp/hub-agent-test/a","new_root":"C:/Temp/hub-agent-test/b"}'
hub run start --wait --timeout 600
hub run stop --wait
hub run plan_delete --args '{"root":"C:/Temp/hub-agent-test/b"}'
hub run delete --args '{"root":"C:/Temp/hub-agent-test/b","confirmation":"C:/Temp/hub-agent-test/b"}' --wait
hub run logs --args '{"root":"C:/Temp/hub-agent-test/a","service":"ollama","lines":200}'
hub run measure --args '{"root":"C:/Temp/hub-agent-test/a"}'
hub run service_stop --args '{"service_id":"open-webui"}' --wait
hub run service_targets
hub run choose_chat_model --args '{"model_id":"ollama/qwen3_5_9b"}' --wait
hub run import_chat_model --args '{"reference":"hf.co/someone/Model-GGUF:Q4_K_M"}' --wait
hub run remove_imported_chat_model --args '{"model_id":"ollama/user_hf_co_someone_model_gguf_q4_k_m"}'
```

Search settings use the same allowlisted boundary:

```bash
hub run web_search_status
hub run web_search_configure --args '{"enabled":true,"searxng_url":"http://localhost:8888"}'
hub run brave_logout
hub run image_search_logout --args '{"provider":"unsplash"}'
```

Credential commands are `brave_login {token}`, `image_search_login
{provider, token}` (`provider`: `unsplash` or `pexels`), and the matching
logout commands. Prefer the desktop Settings screen for login: putting a
token in `--args` also puts it in the invoking shell's command history. The
result contains only configured booleans and a source label, never the key.
Unsplash/Pexels changes reach only the managed Open WebUI process and take
effect after the services restart.

`delete` requires `confirmation` to equal the exact `root` string; anything
else exits 1 with `confirmation_required` and deletes nothing. Run
`plan_delete` first to see what would be removed. `switch`, `register`,
`relocate` and `delete` accept `"stop": true` to authorise stopping the
running stack when the domain plan requires it; without it they exit 1 with
`stop_required` and the plan in `details`.

`logs` tails `<root>/logs/<service>.log` of a known installation (default: the
most recently written file; at most 1000 lines and the last 512 KiB). `measure`
walks the installation without following links and stops after 15 s with
`"partial": true`. `service_targets` lists each enabled service of the
installation in use with its loopback URL and health. These three read without
taking the controller's command slot, so they answer while another command runs.

## Single-writer lock

The window (native or `--browser`/`hub serve`) holds an exclusive OS lock on
`<default root>/state/desktop.lock` for its whole life. It is an OS lock, not
a pid file: it is released when the process exits or crashes.

- A second window brings the open one to the foreground and exits 0. When the
  lock is held by a command and no window exists, it refuses ("já está
  aberto": native dialog when windowed, stderr in browser mode) and exits 1.
- While a window is open, read-only commands still work: `hub state`,
  `hub commands`, and `hub run` with `plan`, `estimate`, `plan_delete`,
  `plan_switch`, `doctor`, `hf_status`, `civitai_status`, `web_search_status`,
  `logs`, `measure`, `service_targets` (see `read_only` in `hub commands`).
- Every other `hub run` command takes the lock for its whole duration,
  including the operation it starts. While the window (or another mutating
  command) holds it, the command exits 1 with code `desktop_running` and
  nothing runs. While it holds the lock, a window cannot open.

## Browser dev mode for UI testing

```powershell
.\.venv\Scripts\python.exe -m local_ai_stack.desktop --browser --no-open
.\PepitosAIHub-CLI.exe hub serve --no-open
```

Both print one line with the URL (`http://127.0.0.1:<port>/#token=<token>`)
and then the Ctrl+C hint, and serve until interrupted. Open the URL with a
browser-automation tool to test the real UI against the real API. The token in
the fragment authorises API calls; keep it out of logs and commits. Native
mode never prints it. The server holds the single-writer lock, so mutating CLI
commands are refused while it runs; drive changes through the UI, or stop the
server first. Build the renderer first (`npm run build` in `desktop-ui/`).

## Parallel work in git

Never put two agents in the same working tree. Use one `git worktree` per
agent and branch, small commits with one responsibility each, tests before
merging, and no reset or force-push on someone else's branch.
