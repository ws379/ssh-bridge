"""Project sessions, reusable tasks, lazy outputs and loopback port forwarding."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import socket
import tempfile
import threading
import time
import uuid

from connection import Connection


class Workspace:
    def __init__(self, runtime):
        self.runtime = runtime
        self.path = ""
        self.detected = {}
        self.snapshots = {}
        self.outputs = OrderedDict()
        self.forwards = {}
        self.lock = threading.Lock()

    def state_path(self):
        default = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "ssh-bridge" / "projects.json"
        return Path(os.environ.get("SSH_BRIDGE_STATE_PATH") or default).expanduser()

    def state(self):
        path = self.state_path()
        if not path.exists():
            return {"recent": [], "tasks": {}}
        if path.stat().st_size > 2_000_000:
            raise ValueError("Project state exceeds 2 MB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("recent"), list)
                or not isinstance(data.get("tasks"), dict) or not isinstance(data.get('jobs', {}), dict)):
            raise ValueError("Invalid project state file; existing file was not overwritten")
        return data

    def save(self, data):
        target = self.state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if len(raw) > 2_000_000:
            raise ValueError("Project state exceeds 2 MB")
        fd, name = tempfile.mkstemp(prefix=".projects-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def key(self):
        return self.runtime.ns["_host"]() + ":" + self.path

    def remember(self):
        with self.lock:
            state = self.state()
            item = {"host": self.runtime.ns["_host"](), "path": self.path}
            state["recent"] = [item] + [v for v in state["recent"] if v != item][:19]
            self.save(state)

    def retain(self, data, mode):
        """Keep large command output out of context; fetch pages by output_id."""
        if mode not in {"auto", "full", "summary"}:
            raise ValueError("output_mode must be auto, full or summary")
        if "stdout" in data:
            size = len(data["stdout"].encode()) + len(data["stderr"].encode())
            if mode == "full" or mode == "auto" and size <= 4096:
                return data
            identity = uuid.uuid4().hex[:16]
            self.outputs[identity] = (time.monotonic(), dict(data), size)
            while len(self.outputs) > 32 or sum(item[2] for item in self.outputs.values()) > 8_000_000:
                self.outputs.popitem(last=False)
            result = {k: v for k, v in data.items() if k not in {"stdout", "stderr"}}
            result.update(output_id=identity, captured_bytes=size)
            if data.get("exit_code"):
                result["stderr_tail"] = data["stderr"][-1500:]
                result["stdout_tail"] = data["stdout"][-1500:]
            return result
        if "results" in data:
            result = dict(data)
            result["results"] = [self.retain(item, mode) if item.get("op") == "exec" else item for item in data["results"]]
            return result
        return data

    def result(self, options):
        identity = options.get("output_id", "")
        if identity not in self.outputs or time.monotonic() - self.outputs[identity][0] > 3600:
            raise ValueError("Output is expired or belongs to another Bridge session")
        entry = self.outputs[identity][1]
        stream = options.get("stream", "stdout")
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        limit = self.runtime.ns["_integer"](options.get("max_bytes", 4096), 1, 65536, "max_bytes")
        offset = self.runtime.ns["_integer"](options.get("offset", 0), 0, 2_000_000, "offset")
        raw = entry[stream].encode()
        if options.get("tail", False):
            offset = max(0, len(raw) - limit)
        content = raw[offset:offset + limit].decode("utf-8", "replace")
        return {"output_id": identity, "stream": stream, "content": content, "offset": offset,
                "next_offset": offset + min(limit, len(raw[offset:])), "has_more": offset + limit < len(raw),
                "truncated_at_capture": entry.get(stream + "_truncated", False)}

    def open(self, path, options):
        data = self.runtime.call("project", {"path": path or "."})
        self.path = data["path"]
        self.detected[self.key()] = data.pop("tasks")
        saved = self.state()["tasks"].get(self.key(), {})
        data["tasks"] = sorted(set(saved) | set(self.detected[self.key()]))
        self.remember()
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        previous = self.snapshots.get(self.key())
        self.snapshots[self.key()] = fingerprint
        if previous == fingerprint and not options.get("refresh", False):
            return {"path": self.path, "unchanged": True, "context_id": fingerprint[:16]}
        data["context_id"] = fingerprint[:16]
        return data

    def task(self, action, name, options):
        if options.get("path") and options["path"] != self.path:
            self.open(options["path"], {})
        elif not self.path:
            self.open(".", {})
        if action == "tasks":
            saved = self.state()["tasks"].get(self.key(), {})
            return {"path": self.path, "tasks": {**self.detected.get(self.key(), {}), **saved}}
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", name):
            raise ValueError("Task name must contain 1..80 letters, digits, dots, colons, underscores or hyphens")
        if action == "save_task":
            steps = options.get("commands")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 32 or not all(isinstance(s, str) and s.strip() for s in steps):
                raise ValueError("commands must contain 1..32 non-empty shell commands")
            for command in steps:
                self.runtime.ns["_guard_command"](command)
            with self.lock:
                state = self.state()
                state["tasks"].setdefault(self.key(), {})[name] = {"commands": steps}
                self.save(state)
            return {"name": name, "saved": True, "steps": len(steps), "path": self.path}
        tasks = {**self.detected.get(self.key(), {}), **self.state()["tasks"].get(self.key(), {})}
        if name not in tasks:
            raise ValueError("Unknown task; open the project or call action=tasks first")
        task = tasks[name]
        commands = task.get("commands") or [task["command"]]
        # Guard every step before starting the first command, including saved tasks.
        for command in commands:
            self.runtime.ns["_guard_command"](command)
        timeout = self.runtime.ns["_integer"](options.get("timeout_sec", 120), 1, 900, "timeout_sec")
        result = self.runtime.handler("ssh_bridge_batch", {
            "operations": [{"op": "exec", "command": c, "cwd": self.path} for c in commands],
            "timeout_sec": timeout, "stop_on_error": True, "output_mode": options.get("output_mode", "auto")})
        data = result["structuredContent"]
        data.update(task=name, path=self.path)
        return data

    def forward(self, options):
        remote = self.runtime.ns["_integer"](options.get("remote_port"), 1, 65535, "remote_port")
        local = self.runtime.ns["_integer"](options.get("local_port", 0), 0, 65535, "local_port")
        if local == 0:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                local = probe.getsockname()[1]
        if local in self.forwards or len(self.forwards) >= 8:
            raise ValueError("Local port already managed, or eight-forward limit reached")
        base, _ = self.runtime.connection(False)
        args = base.ssh_args[:-1] + ["-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}", base.ssh_args[-1]]
        connection = Connection(args, base.interpreter)
        try:
            connection.call({"op": "status", "args": {}}, timeout=15)
        except Exception:
            connection.close()
            raise
        self.forwards[local] = (connection, remote)
        return {"local_port": local, "remote_port": remote, "bind": "127.0.0.1", "url": f"http://127.0.0.1:{local}"}

    def jobs(self, action, name, options):
        if not self.path:
            raise ValueError('Open a project before tracking or inspecting jobs')
        include_gpu = options.get('include_gpu', True)
        if not isinstance(include_gpu, bool):
            raise ValueError('include_gpu must be a boolean')
        if action == 'jobs':
            stored = self.state().get('jobs', {}).get(self.key(), {})
            limit = self.runtime.ns['_integer'](options.get('limit', 20), 1, 20, 'limit')
            offset = self.runtime.ns['_integer'](options.get('offset', 0), 0, 100000, 'offset')
            tail_bytes = self.runtime.ns['_integer'](options.get('tail_bytes', 1024), 1, 4096, 'tail_bytes')
            entries = [dict(value, name=key, tail_bytes=tail_bytes)
                       for key, value in sorted(stored.items())[offset:offset + limit]]
            result = self.runtime.call('jobs_snapshot', {'jobs': entries, 'include_gpu': include_gpu})
            result['total'] = len(stored)
            result['has_more'] = offset + len(entries) < len(stored)
            result['next_offset'] = offset + len(entries) if result['has_more'] else None
            return result
        if action == 'job_status' and options.get('pid') is not None and not name:
            record = {'pid': self.runtime.ns['_integer'](options['pid'], 1, 2147483647, 'pid')}
            if options.get('log_path'):
                record['log_path'] = self.job_log_path(options['log_path'])
        else:
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,80}', name):
                raise ValueError('Job name must contain 1..80 letters, digits, dots, colons, underscores or hyphens')
            stored = self.state().get('jobs', {}).get(self.key(), {})
            if action == 'job_untrack':
                with self.lock:
                    state = self.state()
                    jobs = state.get('jobs', {}).get(self.key(), {})
                    removed = jobs.pop(name, None) is not None
                    if removed:
                        self.save(state)
                return {'name': name, 'untracked': removed, 'process_affected': False}
            if action == 'job_track':
                if name in stored and options.get('replace') is not True:
                    raise ValueError('Job name already exists; pass replace=true to update it')
                pid = self.runtime.ns['_integer'](options.get('pid'), 1, 2147483647, 'pid')
                log_path = options.get('log_path')
                if log_path is not None and (not isinstance(log_path, str) or not log_path.strip()):
                    raise ValueError('log_path must be a non-empty string')
                record = {'pid': pid}
                if log_path:
                    record['log_path'] = self.job_log_path(log_path)
                card = self.runtime.call('job_inspect', dict(record, include_gpu=False))
                if card.get('status') != 'running' or not card.get('identity'):
                    raise ValueError('Process must be visible and running when tracking starts')
                record['identity'] = card['identity']
                with self.lock:
                    state = self.state()
                    jobs = state.setdefault('jobs', {}).setdefault(self.key(), {})
                    if name in jobs and options.get('replace') is not True:
                        raise ValueError('Job name already exists; pass replace=true to update it')
                    jobs[name] = record
                    self.save(state)
                card.update(name=name, tracked=True)
                return card
            if name not in stored:
                raise ValueError('Unknown tracked job')
            record = dict(stored[name])
        if action == 'job_status':
            record['include_gpu'] = include_gpu
            record['tail_bytes'] = self.runtime.ns['_integer'](options.get('tail_bytes', 1024), 1, 4096, 'tail_bytes')
            result = self.runtime.call('job_inspect', record)
            if name:
                result['name'] = name
            return result
        if action == 'job_logs':
            if not record.get('log_path'):
                raise ValueError('This job has no log_path')
            args = {'log_path': record['log_path'],
                    'offset': self.runtime.ns['_integer'](options.get('offset', 0), 0, 2**53, 'offset'),
                    'max_bytes': self.runtime.ns['_integer'](options.get('max_bytes', 4096), 1, 65536, 'max_bytes')}
            log_id = options.get('log_id')
            if log_id is not None:
                if not isinstance(log_id, str) or not re.fullmatch(r'[a-f0-9]{20}', log_id):
                    raise ValueError('log_id must be the value returned by a previous log read')
                args['log_id'] = log_id
            return self.runtime.call('job_logs', args)
        raise ValueError('Unknown job action')

    def job_log_path(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError('log_path must be a non-empty string')
        if posixpath.isabs(path) or path.startswith('~'):
            return path
        return posixpath.join(self.path, path)

    def handle(self, args):
        action, options = args.get("action", "open"), dict(args.get("options", {}))
        if args.get("path"):
            options["path"] = args["path"]
        if action == "open":
            return self.open(options.get("path", "."), options)
        if action == "recent":
            return {"projects": self.state()["recent"]}
        if action in {"tasks", "save_task", "run"}:
            return self.task(action, args.get("name"), options)
        if action in {'job_track', 'job_status', 'job_logs', 'job_untrack', 'jobs'}:
            return self.jobs(action, args.get('name'), options)
        if action == 'gpu':
            return self.runtime.call('gpu_snapshot', {})
        if action == "result":
            return self.result(options)
        if action == "forward":
            return self.forward(options)
        if action == "ports":
            return {"ports": [{"local_port": port, "remote_port": item[1], "active": item[0].process is not None and item[0].process.poll() is None}
                              for port, item in self.forwards.items()]}
        if action == "close_forward":
            port = self.runtime.ns["_integer"](options.get("local_port"), 1, 65535, "local_port")
            item = self.forwards.pop(port, None)
            if item:
                item[0].close()
            return {"local_port": port, "closed": bool(item)}
        if action == "reconnect":
            for connection in self.runtime.sessions.values():
                connection.close()
            return self.runtime.call("status", {})
        if action == "tail":
            options.setdefault("max_bytes", 8192)
            options.setdefault("lines", 100)
            self.runtime.ns["_integer"](options.get("max_bytes", 8192), 1, 65536, "max_bytes")
            self.runtime.ns["_integer"](options.get("lines", 100), 1, 1000, "lines")
            return self.runtime.call("tail", options)
        if action == "status":
            data = self.runtime.call("status", {})
            data["workspace"] = self.path or None
            return data
        if action in {"list", "search", "write", "mkdir", "rename", "delete"}:
            # Compatibility operations share the same validated legacy contract.
            result = self.runtime.handler("ssh_bridge_" + action, options)
            return result["structuredContent"]
        if action == "help":
            return {"options": {
                "open": "path; refresh=true repeats unchanged context",
                "save_task": "name + options.commands=[shell commands]; saved per host/project",
                "job_track": "name + options.pid and optional log_path; records PID identity locally",
                "job_status": "name, or options.pid; optional include_gpu/tail_bytes",
                "jobs": "tracked job cards; optional include_gpu/offset/limit/tail_bytes",
                "job_logs": "name + options.offset/max_bytes/log_id; use returned next_offset and log_id",
                "job_untrack": "name; forget local record only, never signal the remote process",
                "gpu": "read-only GPU utilization, memory and compute process snapshot",
                "run": "name + options.timeout_sec/output_mode",
                "result": "options.output_id, stream=stdout|stderr, offset, max_bytes, tail",
                "forward": "options.remote_port, local_port=0 chooses a free loopback port",
                "close_forward": "options.local_port", "tail": "path + options.lines/max_bytes",
                "search": "options.query/glob/max_results/max_bytes", "list": "path + options.offset/limit/query",
                "write": "path + options.content/expected_sha256/overwrite", "rename": "options.old_path/new_path",
                "delete": "path + options.confirm=true/recursive; human approval required"}}
        raise ValueError("Unknown workspace action")
