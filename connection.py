"""Reusable OpenSSH sessions on Windows and POSIX; no installed remote daemon."""
from __future__ import annotations

import atexit
import base64
from collections import deque
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid
import zlib
import weakref


_connections = weakref.WeakSet()


def _close_connections():
    for connection in list(_connections):
        connection.close()


atexit.register(_close_connections)


class BridgeError(RuntimeError):
    pass


class Connection:
    def __init__(self, ssh_args, interpreter="python3", source=None):
        self.ssh_args = ssh_args
        self.interpreter = interpreter
        self.source = source or Path(__file__).with_name("remote_ops.py").read_text(encoding="utf-8")
        self.process = None
        self.inbox = queue.Queue(maxsize=2)
        self.errors = deque(maxlen=16)
        self.lock = threading.Lock()
        self.token = "SSHBRIDGE_" + uuid.uuid4().hex + ":"
        self.starts = 0
        self.calls = 0
        _connections.add(self)

    def _start(self):
        self.close()
        self.inbox = queue.Queue(maxsize=2)
        self.errors = deque(maxlen=16)
        loader = "import base64,zlib;exec(compile(zlib.decompress(base64.b64decode(%r)),'<ssh-bridge>','exec'))" % base64.b64encode(zlib.compress(self.source.encode())).decode()
        command = shlex.quote(self.interpreter) + " -u -c " + shlex.quote(loader) + " --worker " + shlex.quote(self.token)
        # The bootstrap must stay below Windows CreateProcess's 32767 character limit.
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(self.ssh_args + [command], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=flags)
        except FileNotFoundError as exc:
            raise BridgeError("Local OpenSSH executable was not found.") from exc
        self.process = process
        self.starts += 1
        inbox, errors, prefix = self.inbox, self.errors, self.token.encode()

        def output():
            try:
                while True:
                    line = process.stdout.readline(14_000_001)
                    if not line:
                        break
                    if len(line) > 14_000_000:
                        errors.append("Remote response exceeded the transport limit.")
                        break
                    if line.startswith(prefix):
                        inbox.put_nowait(line[len(prefix):])
                    elif len(line) < 4096:
                        errors.append(line.decode("utf-8", "replace").strip())
            except (OSError, queue.Full):
                pass
            finally:
                process.stdout.close()
                try:
                    inbox.put_nowait(None)
                except queue.Full:
                    pass

        def stderr():
            try:
                while chunk := process.stderr.read1(4096):
                    errors.append(chunk.decode("utf-8", "replace").strip())
            except OSError:
                pass
            finally:
                process.stderr.close()

        threading.Thread(target=output, daemon=True).start()
        threading.Thread(target=stderr, daemon=True).start()

    def call(self, request, timeout=120):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            identity = uuid.uuid4().hex
            request = dict(request, id=identity)
            payload = (json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
            if len(payload) > 12_000_000:
                raise ValueError("Request exceeds 12 MB wire limit.")
            deadline = time.monotonic() + timeout
            # A separate writer keeps a stopped peer from blocking the timeout.
            process = self.process
            failures = []

            def write():
                try:
                    process.stdin.write(payload)
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    failures.append(str(exc))

            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            self.calls += 1
            try:
                reply = self.inbox.get(timeout=max(0.001, deadline - time.monotonic()))
                if reply is None:
                    # Allow the stderr reader to consume the final diagnostic.
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.close()
                    detail = "\n".join(self.errors) or "SSH connection closed."
                    raise BridgeError(detail + " Remote Python 3.8+ is required for file tools and connection reuse.")
                data = json.loads(reply)
                if data.get("id") != identity:
                    raise BridgeError("Remote response ID mismatch; connection discarded.")
                if not data.get("ok"):
                    error = data.get("error", {})
                    raise ValueError(f"{error.get('code', 'remote_error')}: {error.get('message', '')}")
                return data["data"]
            except queue.Empty as exc:
                self.close()
                raise BridgeError(f"SSH operation timed out after {timeout:g}s; no operation was replayed. Verify remote state before retrying changes.") from exc
            except (BridgeError, json.JSONDecodeError, BrokenPipeError):
                self.close()
                raise
            finally:
                writer.join(timeout=0.1)

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass

\n