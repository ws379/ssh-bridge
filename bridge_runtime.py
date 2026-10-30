"""Compact tool handlers and schemas for SSH Bridge 0.4."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import threading
import time

from connection import Connection
from workspace import Workspace


DEFAULT_BYTES = 16_384
TOTAL_BYTES = 65_536
MAX_BYTES = 1_000_000


def integer(value, minimum, maximum, name):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def validate(value, schema, name="arguments"):
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool)}
    if kind and not valid.get(kind, True):
        raise ValueError(f"{name} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{name} must be one of {schema['enum']}")
    if kind == "integer":
        integer(value, schema.get("minimum", -2**63), schema.get("maximum", 2**63), name)
    if kind == "string":
        if "\x00" in value or len(value) < schema.get("minLength", 0):
            raise ValueError(f"{name} is empty or contains NUL")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{name} has invalid format")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{name}.{key} is required")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key not in props and schema.get("additionalProperties") is False:
                raise ValueError(f"unknown argument: {name}.{key}")
            if key in props:
                validate(item, props[key], f"{name}.{key}")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100000):
            raise ValueError(f"{name} has invalid item count")
        for item in value:
            validate(item, schema.get("items", {}), name + "[]")


class Runtime:
    def __init__(self, namespace):
        self.ns = namespace
        self.sessions = {}
        self.schema = {}
        self.lock = threading.Lock()
        self.workspace = Workspace(self)

    def connection(self, proxy):
        host = self.ns["_host"]()
        if not host:
            raise ValueError("Set SSH_BRIDGE_HOST to a host alias from your OpenSSH configuration.")
        if host.startswith("-") or any(c.isspace() or ord(c) < 32 for c in host):
            raise ValueError("SSH_BRIDGE_HOST must be a host alias")
        settings = self.ns["_proxy_settings"]() if proxy else None
        port = os.environ.get("SSH_BRIDGE_PORT", "").strip()
        if port:
            integer(int(port), 1, 65535, "SSH_BRIDGE_PORT")
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectionAttempts=1",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
        if port:
            args += ["-p", port]
        if settings:
            local, local_port, remote_port = settings
            args += ["-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{remote_port}:{local}:{local_port}"]
        args += [host]
        interpreter = os.environ.get("SSH_BRIDGE_REMOTE_PYTHON", "python3").strip() or "python3"
        key = (tuple(args), interpreter)
        with self.lock:
            if key not in self.sessions:
                self.sessions[key] = Connection(args, interpreter)
            return self.sessions[key], settings

    def call(self, op, args, proxy=False):
        connection, settings = self.connection(proxy)
        options = copy.deepcopy(args)
        if settings:
            url = f"http://127.0.0.1:{settings[2]}"
            if op == "batch":
                for item in options["operations"]:
                    if item["op"] == "exec":
                        item["proxy_url"] = url
            else:
                options["proxy_url"] = url
        timeout = args.get("timeout_sec", 120)
        if op == "batch":
            # Each command receives the remaining overall budget remotely.
            options["deadline_sec"] = timeout
        started = time.monotonic()
        try:
            root = self.ns["_root"]() if op == "project" else self.ns["_root"]() or self.workspace.path
            data = connection.call({"op": op, "root": root, "args": options}, timeout=timeout + 12)
        finally:
            # Reverse forwarding retains the earlier per-command lifetime. File
            # connections stay warm and never carry an HTTP-proxy tunnel.
            if settings or os.environ.get("SSH_BRIDGE_REUSE", "1") == "0":
                connection.close()
        if op == "status":
            data.update(configured_host=self.ns["_host"](), configured_root=self.ns["_root"](),
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        session_starts=connection.starts, session_calls=connection.calls)
        return data

    def handler(self, name, args):
        validate(args, self.schema[name])
        suffix = name.removeprefix("ssh_bridge_")
        options = copy.deepcopy(args)
        if suffix == "workspace":
            data = self.workspace.handle(options)
            failed = (data.get("exit_code") not in (None, 0) or any(not item.get("ok", True) for item in data.get("results", []))
                      or any("error" in item for item in data.get("files", [])))
            return self.ns["_structured_result"](data, f"workspace {options.get('action', 'open')}: {'failed' if failed else 'ok'}; see structuredContent", is_error=failed)
        if suffix == "status" and self.workspace.path:
            options["workspace"] = self.workspace.path
        if suffix in {"read", "read_many", "exec", "batch", "search"}:
            options["max_bytes"] = options.get("max_bytes", DEFAULT_BYTES)
        if suffix == "read":
            if options.get("end_line", 2**63) < options.get("start_line", 1):
                raise ValueError("end_line must be at least start_line")
        if suffix == "exec":
            self.ns["_guard_command"](options["command"])
            if not options["command"].strip():
                raise ValueError("command is required")
        if suffix == "read_many":
            options["max_bytes"] = min(options["max_bytes"], TOTAL_BYTES // len(options["paths"]))
            data = self.call("batch_read", options)
            data["per_file_max_bytes"] = options["max_bytes"]
        elif suffix == "batch":
            operations = options["operations"]
            limit = min(options["max_bytes"], TOTAL_BYTES // max(1, len(operations) * 2))
            total_write = 0
            for item in operations:
                op = item["op"]
                required = {"read": ["path"], "write": ["path", "content"], "edit": ["path", "old_text", "new_text"], "exec": ["command"]}[op]
                for key in required:
                    if key not in item:
                        raise ValueError(f"{op} operation requires {key}")
                if op == "exec":
                    self.ns["_guard_command"](item["command"])
                    if not item["command"].strip():
                        raise ValueError("batch command is empty")
                if op == "write":
                    total_write += len(item["content"].encode("utf-8"))
                if op == "edit" and not item["old_text"]:
                    raise ValueError("old_text cannot be empty")
                item["max_bytes"] = limit
                item["timeout_sec"] = options.get("timeout_sec", 120)
            if total_write > MAX_BYTES * 8:
                raise ValueError("batch writes exceed 8 MB")
            data = self.call("batch", options, proxy=any(i["op"] == "exec" for i in operations))
            data["per_operation_max_bytes"] = limit
        else:
            op = {"open_browser": "list", "read_many": "batch_read"}.get(suffix, suffix)
            data = self.call(op, options, proxy=suffix == "exec")
        if suffix in {"open_browser", "list"}:
            data["host"] = self.ns["_host"]()
        if suffix in {"exec", "batch"}:
            data = self.workspace.retain(data, args.get("output_mode", "auto"))
        failed = (data.get("exit_code", 0) != 0 or any(not i.get("ok", True) for i in data.get("results", []))
                  or any("error" in i for i in data.get("files", [])))
        summary = f"{suffix}: "
        if "content" in data:
            summary += f"{data.get('path', '')}, {len(data['content'].encode('utf-8'))} bytes"
            if data.get("truncated") or data.get("partial"):
                summary += " (partial; use ranges to continue)"
            if data.get("binary"):
                summary += " (binary; text editor disabled)"
        elif "entries" in data:
            summary += f"{data['path']}, {len(data['entries'])}/{data['total']} entries"
            if data.get("has_more"):
                summary += f", next_offset={data['next_offset']}"
        elif suffix == "exec":
            summary += f"exit={data['exit_code']}, {data['elapsed_ms']}ms"
            if data.get("stdout_truncated") or data.get("stderr_truncated"):
                summary += " (output truncated; use a narrower command)"
        else:
            summary += "failed; see structuredContent" if failed else "ok; see structuredContent"
        return self.ns["_structured_result"](data, summary, is_error=failed)


def install(ns):
    runtime = Runtime(ns)
    ns["_integer"] = integer
    tools = ns["TOOLS"]
    byte_schema = {"type": "integer", "minimum": 1, "maximum": MAX_BYTES, "default": DEFAULT_BYTES}
    line_schema = {"type": "integer", "minimum": 1, "maximum": 10000000}
    paging = {"offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
              "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
              "query": {"type": "string"}, "show_hidden": {"type": "boolean", "default": True}}
    for tool in tools:
        suffix = tool["name"].removeprefix("ssh_bridge_")
        properties = tool["inputSchema"]["properties"]
        if suffix in {"read", "read_many", "exec", "batch"}:
            properties["max_bytes"] = dict(byte_schema)
        if suffix == "read":
            properties.update(start_line=dict(line_schema, default=1), end_line=dict(line_schema))
            properties["if_sha256"] = {"type": "string", "pattern": "[a-fA-F0-9]{64}"}
            tool["description"] = "Read UTF-8 text, default 16KB. Use start_line/end_line to inspect a range. Full reads return sha256."
        if suffix in {"list", "open_browser"}:
            properties.update(paging)
        if suffix == "write":
            properties["expected_sha256"] = {"type": "string", "pattern": "[a-fA-F0-9]{64}"}
            properties["overwrite"] = {"type": "boolean", "default": True}
            tool["description"] = "Atomically write UTF-8 text. Pass read sha256 as expected_sha256 to reject conflicting updates."
        if suffix == "batch":
            properties["stop_on_error"] = {"type": "boolean", "default": True}
            properties["operations"]["items"]["properties"]["expected_sha256"] = {"type": "string", "pattern": "[a-fA-F0-9]{64}"}
            operation_props = properties["operations"]["items"]["properties"]
            operation_props["op"]["enum"].append("edit")
            operation_props["old_text"] = {"type": "string", "minLength": 1}
            operation_props["new_text"] = {"type": "string"}
            operation_props["start_line"] = dict(line_schema)
            operation_props["end_line"] = dict(line_schema)
            tool["description"] = "Ordered read/edit/write/exec in ONE request, e.g. edit then test. Stops on failure; <=64KB captured output."
        if suffix in {"batch", "exec"}:
            properties["output_mode"] = {"type": "string", "enum": ["auto", "full", "summary"], "default": "auto"}
        if suffix == "rename":
            properties["overwrite"] = {"type": "boolean", "default": False}
        # Preserve detailed structured fields and optional UI. Text contains a
        # short receipt, avoiding a second copy of file and command contents.
    def tool(suffix, description, props, required, readonly):
        return {"name": "ssh_bridge_" + suffix, "description": description,
                "inputSchema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
                "outputSchema": {"type": "object"}, "annotations": {"readOnlyHint": readonly, "idempotentHint": readonly}}
    path = {"type": "string", "minLength": 1}
    tools.append(tool("search", "Search literal text in remote files; bounded results, excludes dependency and Git directories.",
                      {"path": path, "query": {"type": "string", "minLength": 1}, "glob": {"type": "string"},
                       "case_sensitive": {"type": "boolean"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                       "max_files": {"type": "integer", "minimum": 1, "maximum": 10000}, "max_bytes": byte_schema}, ["query"], True))
    tools.append(tool("edit", "Replace exactly one old_text occurrence atomically; avoids resending entire files. Read before editing.",
                      {"path": path, "old_text": {"type": "string", "minLength": 1}, "new_text": {"type": "string"},
                       "expected_sha256": {"type": "string", "pattern": "[a-fA-F0-9]{64}"}}, ["path", "old_text", "new_text"], False))
    tools.append(tool("workspace", "Open a project once; reuse paths/tasks. Actions include job_track/job_status/jobs/job_logs/gpu for read-only process tracking, plus open/search/tail/tasks/run/result/forward. Call help for options.",
                      {"action": {"type": "string", "enum": ["open", "list", "search", "tail", "status", "recent", "tasks", "save_task", "run", "result", "forward", "ports", "close_forward", "reconnect", "write", "mkdir", "rename", "delete", "help", "job_track", "job_status", "job_logs", "job_untrack", "jobs", "gpu"]},
                       "path": path, "name": {"type": "string"}, "options": {"type": "object"}}, ["action"], False))
    def compact(schema):
        # Property names and tool descriptions carry the contract. Avoid loading
        # repeated prose and output field lists for every discovered tool.
        if isinstance(schema, dict):
            schema.pop("description", None)
            for value in schema.values():
                compact(value)
        elif isinstance(schema, list):
            for value in schema:
                compact(value)
    for item in tools:
        compact(item["inputSchema"])
        item.pop("outputSchema", None)
        name = item["name"]
        runtime.schema[name] = item["inputSchema"]
        def handler(args, name=name):
            try:
                return runtime.handler(name, args)
            except Exception as exc:
                return ns["_result_text"](str(exc)[:4096], is_error=True)
        ns["_" + name.removeprefix("ssh_bridge_")] = handler
    ns["RUNTIME"] = runtime
    core = {"workspace", "read", "edit", "batch", "exec", "open_browser"}
    profile = os.environ.get("SSH_BRIDGE_TOOLSET", "compact")
    if profile not in {"compact", "full"}:
        raise ValueError("SSH_BRIDGE_TOOLSET must be compact or full")
    ns["VISIBLE_TOOLS"] = [t for t in tools if profile == "full" or t["name"].removeprefix("ssh_bridge_") in core]
