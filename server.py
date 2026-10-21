"""SSH Bridge 0.4: compact MCP tools and reusable OpenSSH transport."""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = "2024-11-05"

DEFAULT_HOST = ""

DEFAULT_PROXY_REMOTE_PORT = 18080

DEFAULT_TIMEOUT = 120

MAX_OUTPUT_BYTES = 1_000_000

FILE_BROWSER_URI = "ui://ssh-bridge/file-browser/v3.html"

RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"

FILE_BROWSER_PATH = Path(__file__).with_name("ui") / "file-browser.html"

BLOCKED_COMMANDS = (
    "rm -rf",
    "rm -fr",
    "git reset --hard",
    "git clean -fd",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    ":(){",
)

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def _host() -> str:
    return _env("SSH_BRIDGE_HOST", DEFAULT_HOST)

def _root() -> str:
    return _env("SSH_BRIDGE_ROOT")

def _proxy_settings() -> tuple[str, int, int] | None:
    """Return a validated loopback HTTP proxy and its remote forwarded port."""
    proxy_url = _env("SSH_BRIDGE_PROXY_URL")
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    try:
        proxy_port = parsed.port
    except ValueError as exc:
        raise ValueError("SSH_BRIDGE_PROXY_URL must use a valid port from 1 to 65535.") from exc
    if parsed.scheme != "http" or not parsed.hostname or proxy_port is None or not 1 <= proxy_port <= 65535:
        raise ValueError("SSH_BRIDGE_PROXY_URL must be an HTTP proxy URL with an explicit port.")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("SSH_BRIDGE_PROXY_URL must not contain credentials, a path, a query, or a fragment.")
    if parsed.hostname.lower() not in {"localhost", "127.0.0.1"}:
        raise ValueError("SSH Bridge only forwards a proxy listening on this computer's loopback address.")
    raw_remote_port = _env("SSH_BRIDGE_PROXY_REMOTE_PORT", str(DEFAULT_PROXY_REMOTE_PORT))
    try:
        remote_port = int(raw_remote_port)
    except ValueError as exc:
        raise ValueError("SSH_BRIDGE_PROXY_REMOTE_PORT must be an integer from 1 to 65535.") from exc
    if not 1 <= remote_port <= 65535:
        raise ValueError("SSH_BRIDGE_PROXY_REMOTE_PORT must be an integer from 1 to 65535.")
    return "127.0.0.1", proxy_port, remote_port

def _result_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }

def _structured_result(
    data: dict[str, Any],
    text: str,
    *,
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "structuredContent": data,
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if meta:
        result["_meta"] = meta
    return result

def _json_response(request_id: Any, result: Any = None, error: Any = None) -> None:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    # Windows may expose a legacy console encoding (for example cp936). MCP
    # clients decode JSON strings back to Unicode, so ASCII escaping keeps the
    # stdio transport reliable without changing the UI or tool data.
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    sys.stdout.flush()

def _guard_command(command: str) -> None:
    if _env("SSH_BRIDGE_ALLOW_DANGEROUS").lower() in {"1", "true", "yes"}:
        return
    normalized = " ".join(command.lower().split())
    for blocked in BLOCKED_COMMANDS:
        if blocked in normalized:
            raise ValueError(
                f"命令包含默认拦截的危险片段：{blocked!r}。"
                " 如确实需要，请由用户在本机显式设置 SSH_BRIDGE_ALLOW_DANGEROUS=1。"
            )

TOOLS = [
    {
        "name": "ssh_bridge_status",
        "description": "Check the configured SSH host connection. The remote host does not need Codex CLI.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "ssh_bridge_list",
        "description": "List a directory on the configured SSH host. Use before reading or editing remote files.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Remote path, relative to SSH_BRIDGE_ROOT when configured."}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "root": {"type": "string"},
                "path": {"type": "string"},
                "parent": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["host", "root", "path", "entries"],
        },
    },
    {
        "name": "ssh_bridge_read",
        "description": "Read a text file from the configured SSH host.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Remote file path."},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES, "default": 200000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
            "required": ["path", "content", "truncated"],
        },
    },
    {
        "name": "ssh_bridge_read_many",
        "description": "Read several text files through one SSH process to avoid serialized per-file handshakes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 32,
                    "description": "Remote file paths.",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_OUTPUT_BYTES,
                    "default": 200000,
                    "description": "Maximum bytes per file, capped so the batch stays within the server output budget.",
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "object"}},
                "per_file_max_bytes": {"type": "integer"},
            },
            "required": ["files", "per_file_max_bytes"],
        },
    },
    {
        "name": "ssh_bridge_batch",
        "description": "Run ordered read, write, and exec operations through one SSH process.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "description": "Ordered operations. Each item uses op=read, write, or exec.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["read", "write", "exec"]},
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "create_dirs": {"type": "boolean", "default": True},
                            "command": {"type": "string"},
                            "cwd": {"type": "string"},
                        },
                        "required": ["op"],
                        "additionalProperties": False,
                    },
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_OUTPUT_BYTES,
                    "default": 200000,
                    "description": "Maximum output bytes per read or exec operation.",
                },
                "timeout_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 900,
                    "default": DEFAULT_TIMEOUT,
                    "description": "Overall timeout for the batch SSH process.",
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
                "operation_count": {"type": "integer"},
            },
            "required": ["results", "operation_count"],
        },
    },
    {
        "name": "ssh_bridge_write",
        "description": "Write a text file on the configured SSH host. Use only when the user requested a change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Remote file path."},
                "content": {"type": "string", "description": "Complete UTF-8 file content."},
                "create_dirs": {"type": "boolean", "default": True},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "written": {"type": "boolean"}},
            "required": ["path", "written"],
        },
    },
    {
        "name": "ssh_bridge_open_browser",
        "description": "Open the remote file browser UI at a directory on the configured SSH host.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory to open."}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "root": {"type": "string"},
                "path": {"type": "string"},
                "parent": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["host", "root", "path", "parent", "entries"],
        },
        "_meta": {
            "ui": {"resourceUri": FILE_BROWSER_URI, "prefersBorder": True},
            "openai/outputTemplate": FILE_BROWSER_URI,
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "Opening remote files…",
            "openai/toolInvocation/invoked": "Remote file browser ready",
        },
    },
    {
        "name": "ssh_bridge_mkdir",
        "description": "Create a directory on the configured SSH host.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path to create."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "created": {"type": "boolean"}},
            "required": ["path", "created"],
        },
    },
    {
        "name": "ssh_bridge_rename",
        "description": "Rename or move a remote file or directory on the configured SSH host.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_path": {"type": "string"},
                "new_path": {"type": "string"},
            },
            "required": ["old_path", "new_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "old_path": {"type": "string"},
                "new_path": {"type": "string"},
                "renamed": {"type": "boolean"},
            },
            "required": ["old_path", "new_path", "renamed"],
        },
    },
    {
        "name": "ssh_bridge_delete",
        "description": "Delete a remote file or directory. Requires confirm=true; recursive deletion is explicit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "description": "Must be true to perform deletion."},
            },
            "required": ["path", "confirm"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "deleted": {"type": "boolean"},
                "recursive": {"type": "boolean"},
            },
            "required": ["path", "deleted", "recursive"],
        },
    },
    {
        "name": "ssh_bridge_exec",
        "description": "Run a user-requested shell command on the configured SSH host. Destructive commands are blocked by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run remotely."},
                "cwd": {"type": "string", "description": "Remote working directory."},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 900, "default": DEFAULT_TIMEOUT},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
]

def _dispatch(method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": "ssh-bridge", "version": "0.4.0"},
            "instructions": "Prefer workspace open, relative paths and named tasks. Read before editing; human approval before deletion. Data lives in structuredContent; text is a receipt.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": VISIBLE_TOOLS}
    if method == "resources/list":
        return {
            "resources": [
                {
                    "uri": FILE_BROWSER_URI,
                    "name": "ssh-bridge-file-browser",
                    "description": "Remote SSH file browser and editor.",
                    "mimeType": RESOURCE_MIME_TYPE,
                }
            ]
        }
    if method == "resources/read":
        uri = str(params.get("uri", ""))
        if uri != FILE_BROWSER_URI:
            raise ValueError(f"unknown resource: {uri}")
        if not FILE_BROWSER_PATH.is_file():
            raise RuntimeError(f"UI resource is missing: {FILE_BROWSER_PATH}")
        return {
            "contents": [
                {
                    "uri": FILE_BROWSER_URI,
                    "mimeType": RESOURCE_MIME_TYPE,
                    "text": FILE_BROWSER_PATH.read_text(encoding="utf-8"),
                    "_meta": {
                        "ui": {
                            "prefersBorder": True,
                            "csp": {"connectDomains": [], "resourceDomains": []},
                        },
                        "openai/widgetDescription": "Browse, edit, and send files from the SSH host.",
                        "openai/widgetPrefersBorder": True,
                    },
                }
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handlers = {
            "ssh_bridge_status": _status,
            "ssh_bridge_list": _list,
            "ssh_bridge_read": _read,
            "ssh_bridge_read_many": _read_many,
            "ssh_bridge_batch": _batch,
            "ssh_bridge_write": _write,
            "ssh_bridge_open_browser": _open_browser,
            "ssh_bridge_mkdir": _mkdir,
            "ssh_bridge_rename": _rename,
            "ssh_bridge_delete": _delete,
            "ssh_bridge_exec": _exec,
            "ssh_bridge_search": _search,
            "ssh_bridge_edit": _edit,
            "ssh_bridge_workspace": _workspace,
        }
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        return handlers[name](args)
    raise ValueError(f"unsupported method: {method}")

def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                _json_response(None, error={"code": -32600, "message": "request must be an object"})
                continue
            if "id" not in request:
                continue
            try:
                result = _dispatch(request.get("method", ""), request.get("params") or {})
                _json_response(request["id"], result=result)
            except Exception as exc:  # MCP tool errors should stay inside the protocol.
                _json_response(request["id"], error={"code": -32000, "message": str(exc)})
        except json.JSONDecodeError as exc:
            _json_response(None, error={"code": -32700, "message": f"invalid JSON: {exc}"})

from bridge_runtime import install
install(globals())

if __name__ == "__main__":
    main()
