import os
import base64
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from repomin.execution import (
    CommandRunner,
    DockerRunner,
    RunnerError,
    _run_process,
    _tree_size,
    _working_directory_identity,
)
from repomin.model import FailureSpec, RunResult
from repomin.oracle import FailureOracle


def _python_command(script: str) -> str:
    if os.name != "nt":
        return subprocess.list2cmdline([sys.executable, "-c", script])
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    code = "import base64; exec(base64.b64decode('%s'))" % encoded
    return '"%s" -c "%s"' % (sys.executable, code)


class ExecutionTest(unittest.TestCase):
    def test_runners_attach_host_and_container_frame_roots_without_changing_output(self) -> None:
        from dataclasses import replace
        result = RunResult(1, "stdout", "stderr", 0.01)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("repomin.execution._run_process", return_value=result):
                host = CommandRunner("false", timeout_seconds=5).run(root)
                container = DockerRunner("false", timeout_seconds=5,
                    image="fixture:local", executable="/fake/docker")
                container.resolved_image_id = "sha256:" + "a" * 64
                docker = container.run(root)
            self.assertEqual(str(root.resolve()), host.python_execution_root)
            self.assertEqual("/workspace", docker.python_execution_root)
            self.assertEqual(result, replace(host, python_execution_root=None))
            self.assertEqual(result, replace(docker, python_execution_root=None))

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_interrupt_terminates_the_active_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "escaped.txt"
            script = (
                "import time; from pathlib import Path; time.sleep(0.5); "
                "Path(%r).write_text('child survived\\n', encoding='utf-8')"
                % str(marker)
            )

            def interrupt():
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                _run_process(
                    [sys.executable, "-c", script],
                    root,
                    os.environ.copy(),
                    timeout_seconds=5,
                    resource_check=interrupt,
                )
            time.sleep(0.8)

            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_interrupt_during_process_registration_terminates_the_process(self) -> None:
        class InterruptingRegistry:
            def __init__(self) -> None:
                self.process = None
                self.unregistered = False

            def register(self, process, cleanup, activate) -> None:
                self.process = process
                raise KeyboardInterrupt

            def was_cancelled(self, process) -> bool:
                return False

            def unregister(self, process) -> None:
                self.unregistered = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "escaped.txt"
            registry = InterruptingRegistry()
            script = (
                "import time; from pathlib import Path; time.sleep(0.5); "
                "Path(%r).write_text('child survived\\n', encoding='utf-8')"
                % str(marker)
            )

            with self.assertRaises(KeyboardInterrupt):
                _run_process(
                    [sys.executable, "-c", script],
                    root,
                    os.environ.copy(),
                    timeout_seconds=5,
                    process_registry=registry,
                )
            time.sleep(0.8)

            self.assertIsNotNone(registry.process)
            self.assertTrue(registry.unregistered)
            self.assertFalse(marker.exists())

    def test_cancelled_runner_rejects_commands_started_after_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "escaped.txt"
            command = _python_command(
                "from pathlib import Path; "
                "Path(%r).write_text('ran\\n', encoding='utf-8')"
                % str(marker)
            )
            runner = CommandRunner(command, timeout_seconds=5)
            runner.cancel()

            with self.assertRaisesRegex(RunnerError, "cancelled"):
                runner.run(root)

            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "symlink replacement test requires POSIX")
    def test_replaced_working_directory_cannot_supply_external_diagnostics(
        self,
    ) -> None:
        report = """\
<testsuite name="external">
  <testcase name="fails" classname="demo.External">
    <error type="java.lang.AssertionError" message="EXTERNAL_MATCH" />
  </testcase>
</testsuite>
"""
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            renamed = parent / "workspace-renamed"
            external = parent / "external"
            root.mkdir()
            reports = external / "target" / "surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-demo.External.xml").write_text(
                report,
                encoding="utf-8",
            )
            script = (
                "import os; from pathlib import Path; "
                "root=Path(%r); renamed=Path(%r); external=Path(%r); "
                "root.rename(renamed); os.symlink(external, root); "
                "raise SystemExit(1)"
                % (str(root), str(renamed), str(external))
            )
            command = _python_command(script)
            runner = CommandRunner(
                command,
                timeout_seconds=5,
                collect_java_diagnostics=True,
            )
            oracle = FailureOracle(
                runner,
                FailureSpec("EXTERNAL_MATCH", java_exception=True),
            )

            with mock.patch(
                "repomin.execution.collect_surefire_diagnostics"
            ) as collect:
                with self.assertRaisesRegex(
                    RunnerError,
                    "working directory changed",
                ):
                    oracle.verify_baseline(root, repeat=1)

            collect.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX shell command fixture")
    def test_collects_surefire_diagnostics_generated_by_command(self) -> None:
        report = """\
<testsuite name="generated">
  <testcase name="fails" classname="demo.Generated">
    <error type="java.lang.AssertionError" message="GENERATED_MATCH" />
  </testcase>
</testsuite>
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "from pathlib import Path; "
                "reports=Path('target/surefire-reports'); "
                "reports.mkdir(parents=True); "
                "reports.joinpath('TEST-demo.Generated.xml').write_text("
                "%r, encoding='utf-8'); raise SystemExit(1)" % report
            )
            command = _python_command(script)

            result = CommandRunner(
                command,
                timeout_seconds=5,
                collect_java_diagnostics=True,
            ).run(root)

        self.assertEqual(1, result.returncode)
        self.assertIn("GENERATED_MATCH", result.diagnostics)

    def test_windows_working_directory_identity_rejects_reparse_points(
        self,
    ) -> None:
        cwd = mock.Mock()
        cwd.lstat.return_value = mock.Mock(
            st_mode=stat.S_IFDIR,
            st_dev=11,
            st_ino=22,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        with mock.patch("repomin.execution.os.name", "nt"):
            with self.assertRaisesRegex(RunnerError, "reparse point"):
                _working_directory_identity(cwd)

    @unittest.skipUnless(os.name == "posix", "POSIX shell command fixture")
    def test_cancellation_falls_back_when_a_cleanup_thread_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started.txt"
            command = _python_command(
                "import time; from pathlib import Path; "
                "Path(%r).write_text('started\\n', encoding='utf-8'); "
                "time.sleep(10)"
                % str(started)
            )
            runner = CommandRunner(command, timeout_seconds=5)
            errors = []

            def run_command() -> None:
                try:
                    runner.run(root)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run_command)
            worker.start()
            deadline = time.monotonic() + 2
            while not started.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.is_file())

            with mock.patch.object(
                threading.Thread,
                "start",
                side_effect=RuntimeError("thread unavailable"),
            ):
                runner.cancel()
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], RunnerError)

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_host_timeout_terminates_child_processes(self) -> None:
        child_script = """\
import time
from pathlib import Path

time.sleep(0.6)
Path("escaped.txt").write_text("child survived\\n", encoding="utf-8")
"""
        parent_script = """\
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "child.py"])
print("parent started", flush=True)
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child.py").write_text(child_script, encoding="utf-8")
            (root / "parent.py").write_text(parent_script, encoding="utf-8")
            command = "%s parent.py" % shlex.quote(sys.executable)

            # macOS CI can spend more than 100ms starting a fresh Python
            # interpreter; leave enough time for the parent to flush its
            # startup marker while still timing out before the child writes.
            result = CommandRunner(command, timeout_seconds=0.5).run(root)
            time.sleep(0.8)

            self.assertTrue(result.timed_out)
            self.assertEqual(124, result.returncode)
            self.assertIn("parent started", result.stdout)
            self.assertFalse((root / "escaped.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object test")
    def test_windows_timeout_terminates_child_processes(self) -> None:
        child_script = """\
import time
from pathlib import Path

time.sleep(0.6)
Path("escaped.txt").write_text("child survived\\n", encoding="utf-8")
"""
        parent_script = """\
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "child.py"])
print("parent started", flush=True)
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child.py").write_text(child_script, encoding="utf-8")
            (root / "parent.py").write_text(parent_script, encoding="utf-8")
            command = subprocess.list2cmdline([sys.executable, "parent.py"])

            result = CommandRunner(command, timeout_seconds=0.1).run(root)
            time.sleep(0.8)

            self.assertTrue(result.timed_out)
            self.assertEqual(124, result.returncode)
            self.assertIn("parent started", result.stdout)
            self.assertFalse((root / "escaped.txt").exists())

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_timeout_kills_descendants_after_the_group_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready.txt"
            marker = root / "escaped.txt"
            child_script = (
                "import signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(%r).write_text('ready\\n', encoding='utf-8'); "
                "time.sleep(1.5); "
                "Path(%r).write_text('child survived\\n', encoding='utf-8')"
                % (str(ready), str(marker))
            )
            parent_script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "subprocess.Popen([sys.executable, '-c', %r]); "
                "ready=Path(%r); "
                "\nwhile not ready.exists(): time.sleep(0.01)"
                % (child_script, str(ready))
            )

            result = _run_process(
                [sys.executable, "-c", parent_script],
                root,
                os.environ.copy(),
                timeout_seconds=0.15,
            )
            time.sleep(0.6)

            self.assertTrue(result.timed_out)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_completed_command_terminates_background_processes_in_its_group(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "escaped.txt"
            child_script = (
                "import time; from pathlib import Path; time.sleep(0.5); "
                "Path(%r).write_text('child survived\\n', encoding='utf-8')"
                % str(marker)
            )
            parent_script = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', %r]); "
                "raise SystemExit(1)"
                % child_script
            )

            result = _run_process(
                [sys.executable, "-c", parent_script],
                root,
                os.environ.copy(),
                timeout_seconds=5,
            )
            time.sleep(0.7)

            self.assertEqual(1, result.returncode)
            self.assertFalse(result.timed_out)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "setsid test requires POSIX")
    def test_timeout_is_not_blocked_by_a_descendant_that_leaves_the_group(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "escaped.pid"
            child_script = (
                "import os,time; from pathlib import Path; os.setsid(); "
                "Path(%r).write_text(str(os.getpid()), encoding='ascii'); "
                "time.sleep(10)"
                % str(pidfile)
            )
            parent_script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "subprocess.Popen([sys.executable, '-c', %r]); "
                "pidfile=Path(%r); "
                "\nwhile not pidfile.exists(): time.sleep(0.01)\n"
                "time.sleep(10)"
                % (child_script, str(pidfile))
            )
            escaped_pid = None
            started = time.monotonic()
            try:
                result = _run_process(
                    [sys.executable, "-c", parent_script],
                    root,
                    os.environ.copy(),
                    timeout_seconds=0.15,
                )
                elapsed = time.monotonic() - started
                if pidfile.is_file():
                    escaped_pid = int(pidfile.read_text(encoding="ascii"))
            finally:
                if escaped_pid is None and pidfile.is_file():
                    escaped_pid = int(pidfile.read_text(encoding="ascii"))
                if escaped_pid is not None:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            self.assertTrue(result.timed_out)
            self.assertLess(elapsed, 2.5)

    def test_docker_command_uses_isolation_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = DockerRunner(
                "python3 reproduce.py",
                timeout_seconds=10,
                image="repomin-fixture:local",
                network="none",
                environment={"DEMO": "value"},
                cpus=1.5,
                memory_bytes=128 * 1024 * 1024,
                pids_limit=64,
                tmpfs_bytes=256 * 1024 * 1024,
                executable="/fake/docker",
            )

            argv = list(runner.build_argv(root, root / "container.cid"))

        self.assertEqual("repomin-fixture:local", runner.image_reference)
        self.assertIsNone(runner.resolved_image_id)
        self.assertEqual(["/fake/docker", "run"], argv[:2])
        self.assertIn("never", argv)
        self.assertIn("--read-only", argv)
        container_name = argv[argv.index("--name") + 1]
        self.assertRegex(container_name, r"^repomin-[0-9a-f]{32}$")
        self.assertIn("no-new-privileges", argv)
        self.assertIn("ALL", argv)
        self.assertIn("none", argv)
        self.assertIn("REPOMIN=1", argv)
        self.assertIn("DEMO=value", argv)
        self.assertEqual("1.5", argv[argv.index("--cpus") + 1])
        self.assertEqual("134217728", argv[argv.index("--memory") + 1])
        self.assertEqual("134217728", argv[argv.index("--memory-swap") + 1])
        self.assertEqual("64", argv[argv.index("--pids-limit") + 1])
        self.assertIn("size=268435456", argv[argv.index("--tmpfs") + 1])
        self.assertEqual(
            ["repomin-fixture:local", "/bin/sh", "-c", "python3 reproduce.py"],
            argv[-4:],
        )

    def test_docker_runner_keeps_runner_owned_home_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = DockerRunner(
                "true",
                timeout_seconds=1,
                image="repomin-fixture:local",
                environment={"HOME": "/host-controlled"},
                executable="/fake/docker",
            )
            argv = list(runner.build_argv(root, root / "container.cid"))

        env_values = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--env"
        ]
        self.assertIn("HOME=/host-controlled", env_values)
        self.assertEqual("HOME=/tmp", env_values[-1])

    def test_docker_validation_pins_execution_to_the_resolved_image_id(self) -> None:
        image_id = "sha256:" + "a" * 64
        runner = DockerRunner(
            "false",
            timeout_seconds=1,
            image="fixture:mutable",
            executable="/fake/docker",
        )
        with mock.patch(
            "repomin.execution._run_check",
            side_effect=["27.1.1\n", image_id + "\n"],
        ) as run_check:
            runner.validate()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = list(runner.build_argv(root, root / "container.cid"))

        self.assertEqual("fixture:mutable", runner.image)
        self.assertEqual("fixture:mutable", runner.image_reference)
        self.assertEqual(image_id, runner.resolved_image_id)
        self.assertEqual(image_id, argv[-4])
        self.assertNotIn("fixture:mutable", argv)
        self.assertEqual(
            mock.call(
                [
                    "/fake/docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    "fixture:mutable",
                ],
                "Docker image is unavailable locally: fixture:mutable",
            ),
            run_check.call_args_list[1],
        )

    def test_repeated_docker_validation_checks_the_pinned_id_not_the_tag(self) -> None:
        image_id = "sha256:" + "b" * 64
        runner = DockerRunner(
            "false",
            timeout_seconds=1,
            image="fixture:mutable",
            executable="/fake/docker",
        )
        with mock.patch(
            "repomin.execution._run_check",
            side_effect=[
                "27.1.1\n",
                image_id + "\n",
                "27.1.1\n",
                image_id + "\n",
            ],
        ) as run_check:
            runner.validate()
            runner.validate()

        inspect_commands = [
            call.args[0]
            for call in run_check.call_args_list
            if "inspect" in call.args[0]
        ]
        self.assertEqual("fixture:mutable", inspect_commands[0][-1])
        self.assertEqual(image_id, inspect_commands[1][-1])
        self.assertEqual(image_id, runner.resolved_image_id)

    def test_docker_validation_rejects_a_malformed_image_id(self) -> None:
        malformed_ids = (
            "",
            "sha256:" + "a" * 63,
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 64 + "\nsha256:" + "b" * 64,
        )
        for image_id in malformed_ids:
            with self.subTest(image_id=image_id):
                runner = DockerRunner(
                    "false",
                    timeout_seconds=1,
                    image="fixture:local",
                    executable="/fake/docker",
                )
                with mock.patch(
                    "repomin.execution._run_check",
                    side_effect=["27.1.1\n", image_id],
                ):
                    with self.assertRaisesRegex(
                        RunnerError, "valid sha256 image ID"
                    ):
                        runner.validate()
                self.assertIsNone(runner.resolved_image_id)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX executable fixture")
    def test_docker_timeout_requests_container_cleanup(self) -> None:
        script_template = """\
#!/bin/sh
if [ "$1" = "run" ]; then
  sleep 10
fi
printf '%s\\n' "$*" >> "{actions}"
"""

        class CidDockerRunner(DockerRunner):
            def build_argv(self, cwd: Path, cidfile: Path):
                cidfile.write_text("a" * 64, encoding="ascii")
                return [self.executable, "run"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake-docker"
            actions = root / "actions.txt"
            executable.write_text(
                script_template.format(actions=str(actions)),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            runner = CidDockerRunner(
                "false",
                timeout_seconds=0.1,
                image="fixture:local",
                executable=str(executable),
            )

            result = runner.run(root)
            recorded = actions.read_text(encoding="utf-8")

        self.assertTrue(result.timed_out)
        self.assertIn("rm -f aaaaaaaaaaaa", recorded)

    def test_docker_interrupt_requests_container_cleanup(self) -> None:
        class InterruptDockerRunner(DockerRunner):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cleanup_calls = 0

            def build_argv(self, cwd: Path, cidfile: Path):
                self.cidfile = cidfile
                return [sys.executable, "-c", "import time; time.sleep(10)"]

            def _remove_container(self, cidfile: Path) -> bool:
                self.cleanup_calls += 1
                return True

        with tempfile.TemporaryDirectory() as directory:
            runner = InterruptDockerRunner(
                "unused",
                timeout_seconds=5,
                image="fixture:local",
                workspace_limit_bytes=1024,
                executable="/fake/docker",
            )
            with mock.patch(
                "repomin.execution._workspace_limit_reason",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run(Path(directory))

        self.assertGreaterEqual(runner.cleanup_calls, 1)
        self.assertFalse(runner.cidfile.exists())

    def test_docker_cleanup_retries_after_the_client_stops(self) -> None:
        class DelayedContainerRunner(DockerRunner):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cleanup_calls = 0

            def build_argv(self, cwd: Path, cidfile: Path):
                return [sys.executable, "-c", "import time; time.sleep(10)"]

            def _remove_container(self, cidfile: Path) -> bool:
                self.cleanup_calls += 1
                return self.cleanup_calls >= 3

        with tempfile.TemporaryDirectory() as directory:
            runner = DelayedContainerRunner(
                "unused",
                timeout_seconds=0.1,
                image="fixture:local",
                executable="/fake/docker",
            )

            result = runner.run(Path(directory))

        self.assertTrue(result.timed_out)
        self.assertEqual(3, runner.cleanup_calls)

    def test_docker_cleanup_uses_known_name_before_cidfile_exists(self) -> None:
        runner = DockerRunner(
            "false",
            timeout_seconds=1,
            image="fixture:local",
            executable="/fake/docker",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cidfile = root / "not-created.cid"
            argv = list(runner.build_argv(root, cidfile))
            container_name = argv[argv.index("--name") + 1]
            completed = mock.Mock(returncode=0)
            with mock.patch(
                "repomin.execution.subprocess.run",
                return_value=completed,
            ) as run:
                self.assertTrue(runner._remove_container(cidfile))

        actions = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [["/fake/docker", "rm", "-f", container_name]],
            actions,
        )

    def test_docker_rejects_invalid_environment_name(self) -> None:
        runner = DockerRunner(
            "false",
            timeout_seconds=1,
            image="fixture:local",
            environment={"NOT-VALID": "value"},
            executable="/fake/docker",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RunnerError, "environment variable"):
                runner.build_argv(root, root / "container.cid")

    def test_docker_rejects_invalid_resource_limits(self) -> None:
        invalid = (
            ({"cpus": 0}, "CPU"),
            ({"memory_bytes": 1024}, "6 MiB"),
            ({"pids_limit": 0}, "PID"),
            ({"tmpfs_bytes": 0}, "tmpfs"),
            ({"workspace_limit_bytes": 0}, "workspace"),
        )
        for options, message in invalid:
            with self.subTest(options=options):
                with self.assertRaisesRegex(RunnerError, message):
                    DockerRunner(
                        "false",
                        timeout_seconds=1,
                        image="fixture:local",
                        executable="/fake/docker",
                        **options,
                    )

    def test_workspace_growth_stops_the_command_and_is_rejected(self) -> None:
        class WorkspaceRunner(DockerRunner):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cleanup_requested = False

            def build_argv(self, cwd: Path, cidfile: Path):
                cidfile.write_text("a" * 64, encoding="ascii")
                script = (
                    "from pathlib import Path; import time; "
                    "print('ORIGINAL_FAILURE', flush=True); "
                    "Path('growth.bin').write_bytes(b'x' * 4096); time.sleep(10)"
                )
                return [sys.executable, "-c", script]

            def _remove_container(self, cidfile: Path) -> bool:
                self.cleanup_requested = True
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = WorkspaceRunner(
                "unused",
                timeout_seconds=5,
                image="fixture:local",
                workspace_limit_bytes=1024,
                executable="/fake/docker",
            )

            result = runner.run(root)
            oracle = FailureOracle(runner, FailureSpec("ORIGINAL_FAILURE"))

        self.assertTrue(result.resource_exhausted)
        self.assertIn("exceeding the 1024-byte limit", result.resource_reason or "")
        self.assertTrue(runner.cleanup_requested)
        self.assertFalse(result.timed_out)
        self.assertFalse(oracle.accepts(result))

    @unittest.skipUnless(os.name == "posix", "POSIX shell command fixture")
    def test_excessive_command_output_is_a_resource_failure(self) -> None:
        capture_limit = 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = _python_command(
                "import os,time; chunk=b'x' * 65536; "
                "\nfor _ in range(4096): os.write(1, chunk)\n"
                "time.sleep(10)"
            )
            runner = CommandRunner(command, timeout_seconds=5)
            with mock.patch(
                "repomin.execution._MAX_CAPTURE_BYTES",
                capture_limit,
            ):
                result = runner.run(root)

        self.assertTrue(result.resource_exhausted)
        self.assertFalse(result.timed_out)
        self.assertIn("command output exceeded", result.resource_reason or "")
        self.assertLess(result.duration_seconds, 2)
        self.assertLessEqual(len(result.stdout.encode()), capture_limit + 128)
        self.assertNotIn("268435456", result.resource_reason or "")
        self.assertFalse(FailureOracle(runner, FailureSpec("x")).accepts(result))

    def test_workspace_limit_rejects_an_oversized_initial_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.bin").write_bytes(b"x" * 2048)
            runner = DockerRunner(
                "false",
                timeout_seconds=1,
                image="fixture:local",
                workspace_limit_bytes=1024,
                executable="/fake/docker",
            )

            with self.assertRaisesRegex(RunnerError, "candidate workspace"):
                runner.run(root)

    def test_tree_size_tolerates_entries_removed_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transient = root / "transient"
            transient.mkdir()
            (transient / "data.bin").write_bytes(b"x" * 32)
            original_scandir = os.scandir

            def disappearing_scandir(path):
                if str(path) == str(transient):
                    (transient / "data.bin").unlink()
                    transient.rmdir()
                return original_scandir(path)

            with mock.patch(
                "repomin.execution.os.scandir",
                side_effect=disappearing_scandir,
            ):
                self.assertEqual(0, _tree_size(root))

    def test_exit_137_under_memory_limit_is_resource_exhaustion(self) -> None:
        class ExitRunner(DockerRunner):
            def build_argv(self, cwd: Path, cidfile: Path):
                return [sys.executable, "-c", "raise SystemExit(137)"]

        with tempfile.TemporaryDirectory() as directory:
            runner = ExitRunner(
                "unused",
                timeout_seconds=1,
                image="fixture:local",
                memory_bytes=64 * 1024 * 1024,
                executable="/fake/docker",
            )

            result = runner.run(Path(directory))

        self.assertTrue(result.resource_exhausted)
        self.assertIn("memory limit", result.resource_reason or "")


if __name__ == "__main__":
    unittest.main()
