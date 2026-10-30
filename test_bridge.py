import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import remote_ops as remote
import server
from bridge_runtime import Runtime
from workspace import Workspace
from connection import Connection


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, op, **args):
        return remote.dispatch({"op": op, "root": str(self.root), "args": args})

    def test_unicode_ranges_and_exact_truncation(self):
        (self.root / "中 文.txt").write_bytes("一\n二\n三\n".encode())
        data = self.call("read", path="中 文.txt", start_line=2, end_line=2)
        self.assertEqual(data["content"], "二\n")
        self.assertTrue(data["read_only"])
        self.assertIsNone(data["sha256"])
        exact = self.call("read", path="中 文.txt", max_bytes=12)
        self.assertFalse(exact["truncated"])
        short = self.call("read", path="中 文.txt", max_bytes=2)
        self.assertTrue(short["truncated"])
        self.assertFalse(short["binary"])
        self.assertNotIn("�", short["content"])

    def test_huge_line_bounded(self):
        (self.root / "large").write_bytes(b"x" * 2_000_000 + b"\nlast\n")
        data = self.call("read", path="large", max_bytes=40, start_line=2)
        self.assertEqual(data["content"], "last\n")
        data = self.call("read", path="large", max_bytes=40, end_line=1)
        self.assertEqual(len(data["content"]), 40)
        self.assertTrue(data["truncated"])

    def test_binary_and_missing(self):
        (self.root / "binary").write_bytes(b"a\0b")
        self.assertTrue(self.call("read", path="binary")["binary"])
        data = self.call("batch_read", paths=["missing", "binary"], max_bytes=10)
        self.assertEqual(data["files"][0]["error"]["code"], "not_found")
        self.assertTrue(data["files"][1]["binary"])

    def test_atomic_write_conflict_and_edit(self):
        initial = self.call("write", path="new/a.txt", content="hello world")
        self.call("edit", path="new/a.txt", old_text="world", new_text="世界", expected_sha256=initial["sha256"])
        self.assertEqual((self.root / "new/a.txt").read_text(encoding="utf-8"), "hello 世界")
        with self.assertRaises(remote.OperationError):
            self.call("write", path="new/a.txt", content="lost", expected_sha256=initial["sha256"])
        self.assertEqual((self.root / "new/a.txt").read_text(encoding="utf-8"), "hello 世界")
        self.assertEqual(list((self.root / "new").glob(".sshbridge-*")), [])
        with self.assertRaises(remote.OperationError):
            self.call("edit", path="new/a.txt", old_text="absent", new_text="x")

    def test_root_escape_and_rename_collision(self):
        with self.assertRaises(remote.OperationError):
            self.call("read", path="../outside")
        self.call("write", path="a", content="a")
        self.call("write", path="b", content="b")
        with self.assertRaises(remote.OperationError):
            self.call("rename", path="a", new_path="b")
        self.assertEqual((self.root / "a").read_text(), "a")

    def test_listing_pagination_and_search_budget(self):
        for i in range(12):
            (self.root / f"{i:02}.txt").write_text("needle " * 500, encoding="utf-8")
        first = self.call("list", limit=5)
        second = self.call("list", offset=first["next_offset"], limit=5)
        self.assertEqual(first["total"], 12)
        self.assertTrue(first["has_more"])
        self.assertFalse(set(i["name"] for i in first["entries"]) & set(i["name"] for i in second["entries"]))
        matches = self.call("search", query="needle", max_bytes=800)
        self.assertTrue(matches["truncated"])
        self.assertLessEqual(len(matches["matches"]), 1)

    def test_batch_stop_and_continue(self):
        operations = [{"op": "read", "path": "missing"}, {"op": "write", "path": "later", "content": "bad"}]
        result = self.call("batch", operations=operations)
        self.assertTrue(result["stopped"])
        self.assertFalse((self.root / "later").exists())
        result = self.call("batch", operations=operations, stop_on_error=False)
        self.assertEqual(result["operation_count"], 2)
        self.assertTrue((self.root / "later").exists())

    def test_twelve_files_no_shell_positional_bug(self):
        paths = []
        for i in range(12):
            path = f"file {i}.txt"
            paths.append(path)
            (self.root / path).write_text(str(i))
        result = self.call("batch_read", paths=paths, max_bytes=20)
        self.assertEqual([i["content"] for i in result["files"]], list(map(str, range(12))))

    def test_conditional_read_does_not_repeat_content(self):
        self.call("write", path="a", content="unchanged text")
        first = self.call("read", path="a")
        second = self.call("read", path="a", if_sha256=first["sha256"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("content", second)

    def test_edit_then_read_in_one_batch(self):
        self.call("write", path="a", content="old text")
        result = self.call("batch", operations=[{"op": "edit", "path": "a", "old_text": "old", "new_text": "new"}, {"op": "read", "path": "a"}])
        self.assertTrue(all(item["ok"] for item in result["results"]))
        self.assertEqual(result["results"][1]["content"], "new text")
        result = self.call("batch", operations=[{"op": "edit", "path": "a", "old_text": "missing", "new_text": "bad"}, {"op": "write", "path": "later", "content": "bad"}])
        self.assertTrue(result["stopped"])
        self.assertFalse((self.root / "later").exists())

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, "secret").write_text("outside")
            try:
                (self.root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks requires platform privileges")
            with self.assertRaises(remote.OperationError):
                self.call("read", path="link/secret")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environ = patch.dict(os.environ, {"SSH_BRIDGE_HOST": "test-host", "SSH_BRIDGE_STATE_PATH": str(Path(self.temp.name) / "projects.json"), "SSH_BRIDGE_ROOT": ""})
        self.environ.start()
        self.runtime = Runtime(vars(server))
        self.runtime.schema = server.RUNTIME.schema
        self.workspace = self.runtime.workspace

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def project(self):
        return {"path": "/demo", "entries": [], "tasks": {"check": {"command": "echo checked"}}}

    def test_project_context_dedup_and_recent(self):
        with patch.object(self.runtime, "call", side_effect=lambda *a, **k: self.project()) as call:
            first = self.workspace.open("/demo", {})
            second = self.workspace.open("/demo", {})
            third = self.workspace.open("/demo", {"refresh": True})
        self.assertIn("entries", first)
        self.assertTrue(second["unchanged"])
        self.assertNotIn("entries", second)
        self.assertIn("entries", third)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(len(self.workspace.state()["recent"]), 1)

    def test_saved_task_reused_after_new_session(self):
        self.workspace.path = "/demo"
        saved = self.workspace.task("save_task", "check-all", {"commands": ["echo first", "echo second"]})
        self.assertEqual(saved["steps"], 2)
        other = Runtime(vars(server))
        other.schema = self.runtime.schema
        other.workspace.path = "/demo"
        seen = []
        def call(op, args, proxy=False):
            seen.append(args)
            return {"results": [{"op": "exec", "ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}], "operation_count": 1}
        with patch.object(other, "call", side_effect=call):
            result = other.workspace.task("run", "check-all", {})
        self.assertEqual(result["task"], "check-all")
        self.assertEqual([i["command"] for i in seen[0]["operations"]], ["echo first", "echo second"])

    def test_dangerous_task_rejected_before_persistence(self):
        self.workspace.path = "/demo"
        with self.assertRaises(ValueError):
            self.workspace.task("save_task", "bad", {"commands": ["echo first", "rm -rf /important"]})
        self.assertFalse(self.workspace.state_path().exists())

    def test_lazy_output_and_paging(self):
        data = {"exit_code": 0, "stdout": "x" * 16000, "stderr": "", "stdout_truncated": False}
        summary = self.workspace.retain(data, "auto")
        self.assertNotIn("stdout", summary)
        page = self.workspace.result({"output_id": summary["output_id"], "offset": 10, "max_bytes": 100})
        self.assertEqual(len(page["content"]), 100)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 110)
        self.assertIn("stdout", self.workspace.retain(data, "full"))

    def test_failure_output_retains_diagnostics(self):
        result = self.workspace.retain({"exit_code": 1, "stdout": "a" * 8000, "stderr": "traceback\nerror"}, "summary")
        self.assertEqual(result["stderr_tail"], "traceback\nerror")
        self.assertIn("output_id", result)
        self.assertNotIn("stdout", result)

    def test_forward_validations_and_cleanup(self):
        with self.assertRaises(ValueError):
            self.workspace.forward({"remote_port": -1})
        class Fake:
            process = None
            def __init__(self, args, interpreter): self.args = args
            def call(self, *a, **k): return {}
            def close(self): self.closed = True
        with patch("workspace.Connection", Fake):
            data = self.workspace.forward({"remote_port": 22, "local_port": 0})
        connection = self.workspace.forwards[data["local_port"]][0]
        self.assertIn(f"127.0.0.1:{data['local_port']}:127.0.0.1:22", connection.args)
        self.workspace.handle({"action": "close_forward", "options": {"local_port": data["local_port"]}})
        self.assertTrue(connection.closed)

    def test_compact_catalog_and_app_compatibility(self):
        catalog = server._dispatch("tools/list", {})["tools"]
        self.assertEqual(len(catalog), 6)
        self.assertLess(len(json.dumps(catalog)), 5000)
        self.assertIn("ssh_bridge_workspace", [i["name"] for i in catalog])
        with patch.object(server.RUNTIME, "call", return_value={"path": "/demo/new", "written": True}):
            result = server._workspace({"action": "write", "options": {"path": "/demo/new", "content": "x"}})
        self.assertFalse(result["isError"])


class ToolTests(unittest.TestCase):
    def tool(self, suffix, args):
        return server._dispatch("tools/call", {"name": "ssh_bridge_" + suffix, "arguments": args})

    def test_validations_before_any_network(self):
        for suffix, args in [("read", {}), ("read", {"path": "a", "max_bytes": True}),
                             ("read", {"path": "a", "start_line": 3, "end_line": 2}),
                             ("batch", {"operations": [{"op": "write", "path": "x"}]}),
                             ("exec", {"command": "rm -rf /"}), ("delete", {"path": "x", "confirm": "yes"})]:
            with self.subTest(suffix=suffix, args=args):
                with patch.object(server.RUNTIME, "call", side_effect=AssertionError("network reached")):
                    result = self.tool(suffix, args)
                self.assertTrue(result["isError"])

    def test_compact_single_copy(self):
        data = {"path": "a", "content": "unique-payload-" * 1000, "truncated": False}
        with patch.object(server.RUNTIME, "call", return_value=data):
            result = self.tool("read", {"path": "a"})
        wire = json.dumps(result)
        self.assertEqual(wire.count("unique-payload-"), 1000)
        self.assertLess(len(result["content"][0]["text"]), 150)

    def test_total_batch_budget(self):
        seen = []
        def call(op, args, proxy=False):
            seen.extend(args["operations"])
            return {"results": [], "operation_count": 0}
        with patch.object(server.RUNTIME, "call", side_effect=call):
            result = self.tool("batch", {"operations": [{"op": "read", "path": str(i)} for i in range(32)], "max_bytes": 1000000})
        self.assertFalse(result["isError"])
        self.assertLessEqual(sum(i["max_bytes"] * 2 for i in seen), 65536)

    def test_proxy_only_for_exec(self):
        with patch.dict(os.environ, {"SSH_BRIDGE_HOST": "test-host", "SSH_BRIDGE_PROXY_URL": "http://127.0.0.1:8080"}):
            files, none = server.RUNTIME.connection(False)
            commands, settings = server.RUNTIME.connection(True)
        self.assertIsNone(none)
        self.assertNotIn("-R", files.ssh_args)
        self.assertIn("-R", commands.ssh_args)
        self.assertEqual(settings, ("127.0.0.1", 8080, 18080))
        self.assertFalse(any("ProxyCommand" in a for a in commands.ssh_args))

    def test_missing_host_is_reported_before_connecting(self):
        with patch.dict(os.environ, {"SSH_BRIDGE_HOST": ""}):
            with self.assertRaisesRegex(ValueError, "Set SSH_BRIDGE_HOST"):
                server.RUNTIME.connection(False)

    def test_mcp_stdio(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ssh_bridge_read", "arguments": {}}}]
        completed = subprocess.run([sys.executable, str(Path(server.__file__))],
                                   input="\n".join(map(json.dumps, requests)) + "\n", text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = list(map(json.loads, completed.stdout.splitlines()))
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.4.0")
        self.assertTrue(responses[2]["result"]["isError"])

    def test_mcp_invalid_request_does_not_stop_server(self):
        completed = subprocess.run([sys.executable, str(Path(server.__file__))],
                                   input='null\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                                   text=True, capture_output=True, timeout=10)
        responses = list(map(json.loads, completed.stdout.splitlines()))
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"], {})


class ConnectionTests(unittest.TestCase):
    def test_persistent_worker_and_reconnect(self):
        # Execute the exact compressed SSH bootstrap locally, with no remote writes.
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake_ssh.py"
            fake.write_text("import shlex,subprocess,sys\nsys.exit(subprocess.call(shlex.split(sys.argv[-1])))\n")
            connection = Connection([sys.executable, str(fake)], sys.executable)
            try:
                for _ in range(3):
                    data = connection.call({"op": "status", "args": {}}, timeout=10)
                    self.assertIn("python", data)
                self.assertEqual(connection.starts, 1)
                with self.assertRaises(ValueError):
                    connection.call({"op": "read", "args": {"path": str(Path(directory) / "missing")}}, timeout=5)
                connection.call({"op": "status", "args": {}}, timeout=5)
                self.assertEqual(connection.starts, 1)
                connection.close()
                connection.call({"op": "status", "args": {}}, timeout=5)
                self.assertEqual(connection.starts, 2)
            finally:
                connection.close()


class JobInspectionTests(unittest.TestCase):
    def test_incremental_log_cursor_handles_utf8_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'train.log'
            log.write_bytes('A中B'.encode('utf-8'))
            first = remote.job_log({'log_path': 'train.log', 'offset': 0, 'max_bytes': 3}, directory)
            self.assertEqual((first['content'], first['next_offset'], first['pending_utf8_bytes']), ('A', 1, 2))
            second = remote.job_log({'log_path': 'train.log', 'offset': first['next_offset'],
                                     'max_bytes': 4, 'log_id': first['log_id']}, directory)
            self.assertEqual(second['content'], '中B')
            self.assertFalse(second['has_more'])
            log.write_bytes(b'x')
            reset = remote.job_log({'log_path': 'train.log', 'offset': second['next_offset'],
                                    'log_id': second['log_id']}, directory)
            self.assertTrue(reset['reset_required'])
            self.assertEqual(reset['next_offset'], 0)
            with self.assertRaises(remote.OperationError):
                remote.job_log({'log_path': '../outside'}, directory)

    def test_gpu_association_and_pid_reuse(self):
        process = {'pid': 7, 'status': 'running', 'identity': 'identity-a', 'exit_code': None}
        gpu = {'available': True, 'processes_complete': True, 'gpus': [{'uuid': 'gpu-a', 'index': '0'}],
               'processes': [{'pid': 8, 'gpu_uuid': 'gpu-a', 'memory_used_mib': '100'}]}
        with patch.object(remote, 'process_info', return_value=process), patch.object(remote, '_descends_from', return_value=True):
            card = remote.inspect_job({'pid': 7, 'identity': 'identity-a'}, '', gpu)
            self.assertEqual(card['gpu_processes'][0]['gpu_index'], '0')
            self.assertTrue(card['identity_match'])
            reused = remote.inspect_job({'pid': 7, 'identity': 'identity-old'}, '', gpu)
            self.assertEqual(reused['status'], 'pid_reused')
            self.assertEqual(reused['gpu_processes'], [])
            self.assertIsNone(reused['exit_code'])
        with patch.object(remote, 'process_info', side_effect=[process, dict(process, identity='identity-new')]), \
                patch.object(remote, '_descends_from', return_value=True):
            stale = remote.inspect_job({'pid': 7}, '', gpu)
        self.assertEqual(stale['gpu_processes'], [])
        self.assertTrue(stale['gpu_association_stale'])

    def test_gpu_snapshot_parses_and_reports_missing_support(self):
        from types import SimpleNamespace
        responses = [SimpleNamespace(returncode=0, stdout='0, GPU-a, Test GPU, 8192, 1024, 20\n', stderr=''),
                     SimpleNamespace(returncode=0, stdout='123, GPU-a, 512\n', stderr='')]
        with patch.object(remote.shutil, 'which', return_value='nvidia-smi'), \
                patch.object(remote.subprocess, 'run', side_effect=responses):
            snapshot = remote.gpu_snapshot()
        self.assertTrue(snapshot['available'])
        self.assertEqual(snapshot['gpus'][0]['uuid'], 'GPU-a')
        self.assertEqual(snapshot['processes'][0]['pid'], 123)
        with patch.object(remote.shutil, 'which', return_value=None):
            self.assertFalse(remote.gpu_snapshot()['available'])

    def test_tracked_job_survives_new_workspace_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'SSH_BRIDGE_HOST': 'test-host', 'SSH_BRIDGE_STATE_PATH': str(Path(directory) / 'projects.json')}):
            runtime = Runtime(vars(server))
            runtime.workspace.path = '/demo'
            card = {'pid': 42, 'status': 'running', 'identity': 'stable-identity', 'exit_code': None}
            with patch.object(runtime, 'call', return_value=card):
                tracked = runtime.workspace.handle({'action': 'job_track', 'name': 'train',
                                                    'options': {'pid': 42, 'log_path': 'train.log'}})
            self.assertTrue(tracked['tracked'])
            other = Runtime(vars(server))
            other.workspace.path = '/demo'
            with patch.object(other, 'call', return_value=dict(card)) as call:
                other.workspace.handle({'action': 'job_status', 'name': 'train', 'options': {'include_gpu': False}})
            self.assertEqual(call.call_args.args[1]['identity'], 'stable-identity')
            self.assertEqual(call.call_args.args[1]['log_path'], '/demo/train.log')
            with patch.object(other, 'call', side_effect=AssertionError('duplicate name must not contact remote')):
                with self.assertRaises(ValueError):
                    other.workspace.handle({'action': 'job_track', 'name': 'train', 'options': {'pid': 42}})
            with patch.object(other, 'call', side_effect=AssertionError('must not touch remote process')):
                removed = other.workspace.handle({'action': 'job_untrack', 'name': 'train'})
            self.assertTrue(removed['untracked'])
            self.assertFalse(removed['process_affected'])

    def test_unknown_exit_code_is_not_a_tool_error(self):
        with patch.object(server.RUNTIME.workspace, 'handle', return_value={'pid': 42, 'status': 'running', 'exit_code': None}):
            result = server._workspace({'action': 'job_status', 'options': {'pid': 42}})
        self.assertFalse(result['isError'])

    @unittest.skipUnless(sys.platform == 'linux', 'Linux /proc is required')
    def test_real_process_identity(self):
        info = remote.process_info(os.getpid())
        self.assertEqual(info['status'], 'running')
        self.assertEqual(len(info['identity']), 24)


if __name__ == "__main__":
    unittest.main(verbosity=2)
