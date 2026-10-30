---
name: ssh-bridge-usage
description: Work on a configured SSH host using compact, reusable project workflows. Remote Codex CLI is not required.
---

# SSH Bridge

Set `SSH_BRIDGE_HOST` to an alias from the local OpenSSH configuration, then start with `ssh_bridge_workspace` action=`open`, path=the requested project. It bundles connectivity, directory overview, Git state, toolchains and task names. Later paths are relative to that project; open another path to switch. Use action=`status` only when connection details are needed.

Payloads are in `structuredContent`; text is a short receipt. Read 16KB or line ranges, not whole trees. `read.if_sha256` suppresses unchanged full reads. Reopening an unchanged project suppresses its overview; options.refresh=true expands it again.

Prefer one `ssh_bridge_batch` for ordered read/edit/write/exec, e.g. edit then test. It stops on failure by default. Read before editing. Use exact old_text/new_text or write with expected_sha256. Never replay an interrupted write/command before checking remote state.

Workspace actions accept action-specific `options`:

- `search`: query/glob/max_results; `list`: offset/limit/query; `tail`: path/lines/max_bytes.
- `tasks` shows commands. `save_task`: name + options.commands=[commands]. `run`: name + optional timeout_sec/output_mode. Saved tasks persist per host/project. Opening projects never executes tasks.
- `result`: output_id, stream=stdout|stderr, offset/max_bytes or tail=true. Large command output stays in this MCP process for at most one hour. Use output_mode=full only when needed.
- `forward`: remote_port, optional local_port (0 chooses a free port). `ports` lists tunnels; `close_forward` takes local_port. Both ends bind to loopback.
- `recent`, `reconnect`, `help` provide project reuse, a fresh connection and action options.
- `job_track`: name + options.pid and optional log_path tracks an existing Linux process locally. `job_status` accepts that name or options.pid; `jobs` pages tracked cards; `gpu` lists NVIDIA devices and compute PIDs. GPU association uses a sampled parent chain and may be incomplete. `include_gpu=false` skips the GPU queries.
- `job_logs`: name + options.offset/max_bytes/log_id; reuse returned next_offset and log_id. On reset_required start at zero. `job_untrack` forgets only the local record. Tracking never launches, stops or resumes a process; an invisible PID has unknown exit status, and PID reuse is reported separately.

SSH_BRIDGE_PROXY_URL is a credential-free loopback HTTP proxy, reverse-forwarded only during exec/batch exec. File operations never use that tunnel. Commands with side effects are never replayed automatically.

Default discovery exposes six tools; SSH_BRIDGE_TOOLSET=full restores legacy tools. Only claim the browser panel is visible if the client renders it. Deletion requires explicit human approval and confirm=true. Command filtering is an accident guard, not a sandbox.
