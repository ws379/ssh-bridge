"""Sent to remote Python on stdin/command invocation; never installed remotely."""
from __future__ import annotations

import contextlib
import codecs
import csv
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import subprocess
import threading
import signal
import platform
import socket
import shlex

MAX_BYTES = 1_000_000


def _process_stat(pid):
    text = (Path('/proc') / str(pid) / 'stat').read_text(encoding='utf-8')
    fields = text[text.rfind(')') + 2:].split()
    return fields[0], int(fields[1]), int(fields[19])


def process_info(pid):
    """Inspect a Linux process without treating a missing PID as success."""
    if sys.platform != 'linux' or not Path('/proc').is_dir():
        return {'pid': pid, 'status': 'unsupported', 'exit_code': None}
    directory = Path('/proc') / str(pid)
    try:
        owner = directory.stat().st_uid
        if os.geteuid() != 0 and owner != os.geteuid():
            return {'pid': pid, 'status': 'inaccessible', 'exit_code': None}
        state, parent, ticks = _process_stat(pid)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        identity = hashlib.sha256(('%s:%s:%s' % (boot, pid, ticks)).encode()).hexdigest()[:24]
        info = {'pid': pid, 'status': 'zombie' if state in ('Z', 'X') else 'running',
                'process_state': state, 'parent_pid': parent, 'identity': identity, 'exit_code': None}
        try:
            boot_time = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                             if line.startswith('btime '))
            info['started_at'] = boot_time + ticks / os.sysconf('SC_CLK_TCK')
        except (OSError, StopIteration, ValueError):
            info['started_at'] = None
        try:
            with (directory / 'cmdline').open('rb') as stream:
                raw = stream.read(1025)
            info['command_truncated'] = len(raw) > 1024
            info['command'] = raw[:1024].replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            info['cwd'] = os.readlink(str(directory / 'cwd'))
        except OSError:
            info['command'], info['cwd'] = None, None
        return info
    except FileNotFoundError:
        return {'pid': pid, 'status': 'not_visible', 'exit_code': None}
    except (PermissionError, ProcessLookupError):
        return {'pid': pid, 'status': 'inaccessible', 'exit_code': None}


def gpu_snapshot():
    executable = shutil.which('nvidia-smi')
    if not executable:
        return {'available': False, 'reason': 'nvidia-smi not found', 'gpus': [], 'processes': []}
    def query(fields):
        result = subprocess.run([executable, '--query-' + fields[0] + '=' + fields[1],
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:300] or 'nvidia-smi query failed')
        return list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))
    try:
        rows = query(('gpu', 'index,uuid,name,memory.total,memory.used,utilization.gpu'))
        gpus = [dict(zip(('index', 'uuid', 'name', 'memory_total_mib', 'memory_used_mib', 'utilization_percent'), row))
                for row in rows[:32] if len(row) == 6]
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {'available': False, 'reason': str(exc)[:300], 'gpus': [], 'processes': []}
    result = {'available': True, 'gpus': gpus, 'gpus_truncated': len(rows) > 32,
              'gpus_complete': len(rows) <= 32 and len(gpus) == len(rows),
              'processes': [], 'processes_complete': True}
    try:
        rows = query(('compute-apps', 'pid,gpu_uuid,used_gpu_memory'))
        result['processes'] = [{'pid': int(row[0]), 'gpu_uuid': row[1], 'memory_used_mib': row[2]}
                               for row in rows[:128] if len(row) == 3 and row[0].strip().isdigit()]
        result['processes_complete'] = len(rows) <= 128 and len(result['processes']) == len(rows)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        result.update(processes_complete=False, process_error=str(exc)[:300])
    return result


def _descends_from(pid, ancestor):
    seen = set()
    for _ in range(64):
        if pid == ancestor:
            return True
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            _, pid, _ = _process_stat(pid)
        except (OSError, ValueError, IndexError):
            break
    return False


def job_log(args, root):
    path, _ = resolve_path(args['log_path'], root)
    limit = args.get('max_bytes', 4096)
    offset = args.get('offset', 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 65536:
        raise OperationError('invalid_argument', 'max_bytes must be 1..65536')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise OperationError('invalid_argument', 'offset must be a non-negative integer')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        log_id = hashlib.sha256(('%s:%s' % (info.st_dev, info.st_ino)).encode()).hexdigest()[:20]
        if offset > info.st_size or args.get('log_id') and args['log_id'] != log_id:
            return {'path': str(path), 'content': '', 'offset': offset, 'next_offset': 0,
                    'log_id': log_id, 'size': info.st_size, 'reset_required': True, 'has_more': info.st_size > 0}
        stream.seek(offset)
        raw = stream.read(limit)
    if b'\0' in raw:
        raise OperationError('binary_file', 'Log must be UTF-8 text')
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    content = decoder.decode(raw, final=False)
    pending = len(decoder.getstate()[0])
    next_offset = offset + len(raw) - pending
    return {'path': str(path), 'content': content, 'offset': offset, 'next_offset': next_offset,
            'log_id': log_id, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'reset_required': False,
            'pending_utf8_bytes': pending,
            'has_more': next_offset < info.st_size}


def inspect_job(args, root, gpu=None):
    pid = args.get('pid')
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2147483647:
        raise OperationError('invalid_argument', 'pid must be a positive integer')
    info = process_info(pid)
    expected = args.get('identity')
    if expected and info.get('identity') and info['identity'] != expected:
        info = {'pid': pid, 'status': 'pid_reused', 'exit_code': None, 'identity_match': False}
    elif expected:
        info['identity_match'] = True if info.get('identity') == expected else None
    log_path = args.get('log_path')
    if log_path:
        tail_bytes = args.get('tail_bytes', 1024)
        if isinstance(tail_bytes, bool) or not isinstance(tail_bytes, int) or not 1 <= tail_bytes <= 4096:
            raise OperationError('invalid_argument', 'tail_bytes must be 1..4096')
        path, _ = resolve_path(log_path, root)
        try:
            size = path.stat().st_size
            log = job_log({'log_path': log_path, 'offset': max(0, size - tail_bytes),
                           'max_bytes': tail_bytes}, root)
            info['log'] = log
        except FileNotFoundError:
            info['log'] = {'path': str(path), 'missing': True}
    if args.get('include_gpu', True):
        gpu = gpu if gpu is not None else gpu_snapshot()
        info['gpu_available'] = gpu['available']
        if not gpu['available']:
            info['gpu_reason'] = gpu.get('reason')
        info['gpu_processes_complete'] = gpu.get('processes_complete', False)
        matches = [item for item in gpu.get('processes', [])
                   if info['status'] == 'running' and _descends_from(item['pid'], pid)]
        if info.get('identity') and process_info(pid).get('identity') != info['identity']:
            matches, info['gpu_association_stale'] = [], True
        indices = {item['uuid']: item['index'] for item in gpu.get('gpus', [])}
        info['gpu_processes'] = [dict(item, gpu_index=indices.get(item['gpu_uuid'])) for item in matches]
    return info


class OperationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def resolve_path(raw, root="", *, entry=False):
    if not isinstance(raw, str) or "\x00" in raw:
        raise OperationError("invalid_path", "路径必须是不含 NUL 的字符串。")
    bound = Path(root).expanduser().resolve() if root else None
    path = Path(raw or ".").expanduser()
    if not path.is_absolute():
        path = (bound or Path.cwd()) / path
    path = Path(os.path.abspath(path))
    path = path.parent.resolve() / path.name if entry else path.resolve()
    if bound and path != bound and bound not in path.parents:
        raise OperationError("root_escape", "路径或符号链接超出了 SSH_BRIDGE_ROOT。")
    return path, bound


def read_file(path, limit, start_line=1, end_line=None):
    if not path.is_file():
        if not path.exists():
            raise FileNotFoundError(str(path))
        raise OperationError("not_file", "目标不是普通文件。")
    with path.open("rb") as stream:
        # readline(size) also bounds memory for a multi-gigabyte single line.
        if start_line > 1:
            number = 1
            while number < start_line:
                part = stream.readline(65536)
                if not part:
                    break
                if part.endswith(b"\n"):
                    number += 1
        if end_line is None:
            data = stream.read(limit + 1)
        else:
            chunks, count, number = [], 0, start_line
            while count <= limit and number <= end_line:
                part = stream.readline(limit + 1 - count)
                if not part:
                    break
                chunks.append(part)
                count += len(part)
                if part.endswith(b"\n"):
                    number += 1
            data = b"".join(chunks)
        info = os.fstat(stream.fileno())
    truncated = len(data) > limit
    data = data[:limit]
    binary = b"\0" in data
    content = ""
    if not binary:
        try:
            # A byte-limited preview may stop inside a UTF-8 character.
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if truncated and exc.reason == "unexpected end of data" and exc.end == len(data):
                content = data[:exc.start].decode("utf-8")
            else:
                binary = True
    partial = start_line != 1 or end_line is not None
    return {"path": str(path), "content": content, "truncated": truncated, "partial": partial,
            "binary": binary, "read_only": truncated or binary or partial, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "sha256": hashlib.sha256(data).hexdigest() if not truncated and not binary and not partial else None}


@contextlib.contextmanager
def write_lock(path):
    identity = str(getattr(os, "getuid", lambda: os.environ.get("USERNAME", "user"))())
    directory = Path(tempfile.gettempdir()) / ("sshbridge-locks-" + hashlib.sha256(identity.encode()).hexdigest()[:12])
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
        raise OperationError("unsafe_lock_directory", "文件锁目录权限异常。")
    name = directory / hashlib.sha256(os.fsencode(path)).hexdigest()
    fd = os.open(name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise OperationError("file_busy", "另一个保存操作仍在进行，请稍后重试。")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_file(path, args):
    data = args["content"].encode("utf-8")
    if len(data) > MAX_BYTES:
        raise OperationError("too_large", "文件超过 1,000,000 bytes 写入限制。")
    if args.get("create_dirs", True):
        path.parent.mkdir(parents=True, exist_ok=True)
    with write_lock(path):
        exists = path.exists()
        if exists and not path.is_file():
            raise OperationError("not_file", "目标不是普通文件。")
        if exists and not args.get("overwrite", True):
            raise OperationError("already_exists", "同名文件已存在，未覆盖。")
        expected = args.get("expected_sha256")
        if isinstance(expected, str):
            expected = expected.lower()
        if expected is not None:
            if not exists or path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise OperationError("conflict", "远程文件已变化，请重新读取后合并修改。")
        mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
        fd, temporary = tempfile.mkstemp(prefix=".sshbridge-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"path": str(path), "written": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def list_directory(path, bound, args):
    query = args.get("query", "").casefold()
    items = []
    with os.scandir(path) as iterator:
        for item in iterator:
            if not args.get("show_hidden", True) and item.name.startswith("."):
                continue
            if query and query not in item.name.casefold():
                continue
            kind = "link" if item.is_symlink() else "directory" if item.is_dir(follow_symlinks=False) else "file"
            items.append((kind, item.name))
            if len(items) > 100_000:
                raise OperationError("directory_too_large", "目录超过 100,000 项，请指定 query 缩小范围。")
    items.sort(key=lambda item: (item[0] != "directory", item[1].casefold(), item[1]))
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    entries = []
    for kind, name in items[offset:offset + limit]:
        child = path / name
        try:
            info = child.lstat()
            size, mtime = info.st_size, info.st_mtime_ns
        except OSError:
            size, mtime = 0, None
        entries.append({"name": name, "path": str(child), "kind": kind, "size": size, "mtime_ns": mtime})
    more = offset + len(entries) < len(items)
    parent = path.parent if path != bound else path
    return {"root": str(bound or Path(path.anchor)), "path": str(path), "parent": str(parent), "entries": entries,
            "total": len(items), "has_more": more, "next_offset": offset + len(entries) if more else None}


def search_files(path, bound, args):
    query = args["query"]
    sensitive = args.get("case_sensitive", False)
    needle = query if sensitive else query.casefold()
    matches, scanned, consumed, skipped, rendered = [], 0, 0, 0, 0
    truncated = False
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"} and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            if not fnmatch.fnmatchcase(name, args.get("glob", "*")):
                continue
            file = Path(directory) / name
            if file.is_symlink():
                continue
            if scanned >= args.get("max_files", 2000) or consumed >= 32_000_000:
                truncated = True
                break
            scanned += 1
            try:
                if file.stat().st_size > MAX_BYTES:
                    skipped += 1
                    continue
                with file.open("rb") as stream:
                    data = stream.read(min(MAX_BYTES + 1, 32_000_000 - consumed))
                consumed += len(data)
                if b"\0" in data or len(data) > MAX_BYTES:
                    skipped += 1
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                candidate = line if sensitive else line.casefold()
                if needle in candidate:
                    snippet = line[:500]
                    size = len(snippet.encode("utf-8")) + len(os.fsencode(file)) + 80
                    if rendered + size > args.get("max_bytes", 16_384):
                        truncated = True
                        break
                    rendered += size
                    matches.append({"path": str(file), "line": number, "text": snippet})
                    if len(matches) >= args.get("max_results", 100):
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break
    return {"path": str(path), "query": query, "matches": matches, "truncated": truncated,
            "scanned_files": scanned, "scanned_bytes": consumed, "skipped_files": skipped}


def dispatch(request):
    op, args, root = request["op"], request.get("args", {}), request.get("root", "")
    if op == 'gpu_snapshot':
        return gpu_snapshot()
    if op == 'job_inspect':
        return inspect_job(args, root)
    if op == 'job_logs':
        return job_log(args, root)
    if op == 'jobs_snapshot':
        jobs = args.get('jobs', [])
        if not isinstance(jobs, list) or len(jobs) > 20:
            raise OperationError('invalid_argument', 'jobs must contain at most 20 entries')
        include_gpu = args.get('include_gpu', True)
        gpu = gpu_snapshot() if include_gpu else None
        cards = []
        for job in jobs:
            try:
                card = inspect_job(dict(job, include_gpu=include_gpu), root, gpu)
                card['name'] = job['name']
                cards.append(card)
            except Exception as exc:
                cards.append({'name': job.get('name'), 'pid': job.get('pid'), 'error': error_data(exc)})
        result = {'jobs': cards}
        if gpu is not None:
            result['gpu'] = gpu
        return result
    if op == "status":
        return {"host": socket.gethostname(), "user": os.environ.get("USER", os.environ.get("USERNAME", "")),
                "home": str(Path.home()), "cwd": str(Path.cwd()), "os": platform.system(),
                "python": platform.python_version()}
    if op == "project":
        path, bound = resolve_path(args.get("path", "."), root)
        directory = list_directory(path, bound, {"limit": 30, "show_hidden": False})
        tasks = {}
        package = path / "package.json"
        if package.is_file() and package.stat().st_size <= 65536:
            try:
                package, _ = resolve_path(str(package), str(path))
                value = json.loads(package.read_text(encoding="utf-8"))
                for name in list(value.get("scripts", {}))[:50]:
                    tasks["npm:" + name] = {"command": "npm run " + shlex.quote(name), "cwd": str(path)}
            except (ValueError, OSError, TypeError, OperationError):
                pass
        if (path / "tests").is_dir() or (path / "pytest.ini").is_file():
            python = str(path / ".venv/bin/python") if (path / ".venv/bin/python").is_file() else "python3"
            tasks["python:test"] = {"command": shlex.quote(python) + " -m pytest -q", "cwd": str(path)}
        git = execute({"command": "git --no-optional-locks -c core.fsmonitor=false status --short --branch --untracked-files=no",
                       "cwd": str(path), "max_bytes": 2048, "timeout_sec": 5}, root) if shutil.which("git") else {"exit_code": 127}
        if git.get("exit_code") == 0:
            tasks.update({"git:status": {"command": "git status --short --branch", "cwd": str(path)},
                          "git:diff": {"command": "git diff --stat", "cwd": str(path)}})
        total, used, free = shutil.disk_usage(path)
        return {"path": str(path), "entries": [{"name": e["name"], "kind": e["kind"]} for e in directory["entries"]],
                "total_entries": directory["total"], "has_more": directory["has_more"],
                "git": git.get("stdout", "").strip() if git.get("exit_code") == 0 else None,
                "toolchains": {name: shutil.which(name) for name in ("python3", "node", "npm", "git", "rg", "uv")},
                "disk_free_gb": round(free / 1e9, 1), "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
                "tasks": tasks}
    if op == "exec":
        return execute(args, root)
    if op == "batch":
        results = []
        deadline = time.monotonic() + args.get("deadline_sec", 120)
        for index, item in enumerate(args["operations"]):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OperationError("timeout", "Batch overall timeout expired.")
                if item["op"] == "exec":
                    item = dict(item, timeout_sec=min(item.get("timeout_sec", 120), remaining))
                data = dispatch({"op": item["op"], "args": item, "root": root})
                ok = data.get("exit_code", 0) == 0
                result = dict(data, index=index, op=item["op"], ok=ok)
            except Exception as exc:
                result = {"index": index, "op": item["op"], "ok": False, "error": error_data(exc)}
            results.append(result)
            if not result["ok"] and args.get("stop_on_error", True):
                break
        return {"results": results, "operation_count": len(results),
                "requested_count": len(args["operations"]), "stopped": len(results) < len(args["operations"])}
    if op == "batch_read":
        paths, limit = args["paths"], args.get("max_bytes", 16_384)
        if not paths or len(paths) > 32 or len(paths) * limit > MAX_BYTES:
            raise OperationError("too_large", "批量读取最多 32 个文件，合计读取预算最多 1,000,000 bytes。")
        files = []
        for raw in paths:
            try:
                path, _ = resolve_path(raw, root)
                files.append(read_file(path, limit))
            except (OSError, OperationError) as exc:
                files.append({"path": raw, "error": error_data(exc)})
        return {"files": files}
    path, bound = resolve_path(args.get("path", "."), root, entry=op in {"delete", "rename"})
    if op == "list":
        return list_directory(path, bound, args)
    if op == "read":
        data = read_file(path, args.get("max_bytes", 16_384), args.get("start_line", 1), args.get("end_line"))
        if args.get("if_sha256") and data["sha256"] == args["if_sha256"].lower():
            data.pop("content")
            data["unchanged"] = True
        return data
    if op == "write":
        return write_file(path, args)
    if op == "edit":
        current = read_file(path, MAX_BYTES)
        if current["binary"] or current["truncated"]:
            raise OperationError("not_text", "精确替换仅支持完整的 UTF-8 文本文件，最大 1MB。")
        expected = args.get("expected_sha256")
        if expected and expected.lower() != current["sha256"]:
            raise OperationError("conflict", "文件已变化，请重新读取后修改。")
        old, new = args["old_text"], args["new_text"]
        count = current["content"].count(old)
        if count != 1:
            raise OperationError("ambiguous_edit", "old_text 必须在原文件中恰好出现一次，实际 %d 次。" % count)
        return write_file(path, {"content": current["content"].replace(old, new, 1),
                                 "create_dirs": False, "expected_sha256": current["sha256"]})
    if op == "mkdir":
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "created": True}
    if op in {"delete", "rename"}:
        if path == bound or path == path.parent or path == Path.cwd().resolve():
            raise OperationError("protected_root", "不能删除或移动根目录或远程当前目录。")
    if op == "delete":
        if args.get("confirm") is not True:
            raise OperationError("confirmation_required", "删除操作需要 confirm=true。")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            if args.get("recursive", False):
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()
        return {"path": str(path), "deleted": True, "recursive": args.get("recursive", False)}
    if op == "rename":
        target, _ = resolve_path(args["new_path"], root, entry=True)
        if target == bound or target == target.parent or target == Path.cwd().resolve():
            raise OperationError("protected_root", "不能覆盖根目录。")
        if (target.exists() or target.is_symlink()) and not args.get("overwrite", False):
            raise OperationError("already_exists", "目标已存在，未覆盖。")
        path.rename(target)
        return {"old_path": str(path), "new_path": str(target), "renamed": True}
    if op == "tail":
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            limit = args.get("max_bytes", 64_000)
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
        if b"\0" in data:
            raise OperationError("binary_file", "日志尾读仅支持文本文件。")
        text = data.decode("utf-8", errors="replace")
        # Drop a partial first line when seeking into the middle of a file.
        if size > limit and "\n" in text:
            text = text.split("\n", 1)[1]
        lines = text.splitlines(keepends=True)
        count = args.get("lines", 200)
        return {"path": str(path), "content": "".join(lines[-count:]), "size": size,
                "truncated": size > limit or len(lines) > count}
    if op == "search":
        return search_files(path, bound, args)
    raise OperationError("unknown_operation", f"未知文件操作: {op}")


def error_data(exc):
    if isinstance(exc, OperationError):
        code = exc.code
    elif isinstance(exc, FileNotFoundError):
        code = "not_found"
    elif isinstance(exc, PermissionError):
        code = "permission_denied"
    elif isinstance(exc, IsADirectoryError):
        code = "not_file"
    elif isinstance(exc, NotADirectoryError):
        code = "not_directory"
    else:
        code = "file_error"
    return {"code": code, "message": str(exc)}


def execute(args, root):
    cwd, _ = resolve_path(args.get("cwd") or ".", root)
    environment = dict(os.environ)
    proxy = args.get("proxy_url")
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment[key] = proxy
    limit, timeout = args.get("max_bytes", 16_384), args.get("timeout_sec", 120)
    started = time.monotonic()
    process = subprocess.Popen(args["command"], shell=True, cwd=str(cwd), env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != "nt")
    captures, totals = [bytearray(), bytearray()], [0, 0]

    def drain(pipe, index):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                totals[index] += len(chunk)
                combined = captures[index] + chunk
                if len(combined) > limit:
                    head = limit // 2
                    captures[index] = combined[:head] + combined[-(limit - head):]
                else:
                    captures[index] = combined
        finally:
            pipe.close()

    threads = [threading.Thread(target=drain, args=(pipe, i), daemon=True)
               for i, pipe in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Detached jobs must use their own supervisor. Clean descendants even when
        # the shell exited, so inherited pipes cannot hold this RPC indefinitely.
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=2)
    return {"exit_code": 124 if timed_out else process.returncode, "cwd": str(cwd),
            "stdout": bytes(captures[0]).decode("utf-8", "replace"),
            "stderr": bytes(captures[1]).decode("utf-8", "replace"),
            "stdout_truncated": totals[0] > limit, "stderr_truncated": totals[1] > limit,
            "capture_format": "head_and_tail" if any(n > limit for n in totals) else "complete",
            "timed_out": timed_out, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def main():
    try:
        raw = sys.stdin.buffer.read(8_000_001)
        if len(raw) > 8_000_000:
            raise OperationError("too_large", "请求超过大小限制。")
        data = dispatch(json.loads(raw))
        result = {"ok": True, "data": data}
    except Exception as exc:
        result = {"ok": False, "error": error_data(exc)}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    if "--worker" in sys.argv:
        token = sys.argv[sys.argv.index("--worker") + 1]
        while True:
            raw = sys.stdin.buffer.readline(12_000_001)
            if not raw:
                break
            request = {}
            try:
                if len(raw) > 12_000_000:
                    break
                request = json.loads(raw)
                result = {"id": request["id"], "ok": True, "data": dispatch(request)}
            except Exception as exc:
                result = {"id": request.get("id"), "ok": False, "error": error_data(exc)}
            sys.stdout.write(token + json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    else:
        main()
