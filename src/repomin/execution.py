from __future__ import annotations

import hashlib
import locale
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence

from repomin.model import RunResult
from repomin.signature import collect_surefire_diagnostics


_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_DOCKER_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
_CAPTURE_PREVIEW_BYTES = 1024 * 1024


class RunnerError(RuntimeError):
    pass


class _ProcessCancelled(RunnerError):
    pass


class _BoundedOutput:
    def __init__(self) -> None:
        self.limit = _MAX_CAPTURE_BYTES
        self._lock = threading.Lock()
        self._buffers = (bytearray(), bytearray())
        self._stored = 0
        self._observed = 0
        self._exceeded = False

    def append(self, stream_index: int, content: bytes) -> None:
        if not content:
            return
        with self._lock:
            self._observed += len(content)
            remaining = max(0, self.limit - self._stored)
            retained = content[:remaining]
            self._buffers[stream_index].extend(retained)
            self._stored += len(retained)
            if len(retained) < len(content):
                self._exceeded = True

    def limit_reason(self) -> Optional[str]:
        with self._lock:
            if not self._exceeded:
                return None
            observed = self._observed
        return "command output exceeded the %d-byte limit after %d bytes" % (
            self.limit,
            observed,
        )

    def text(self) -> tuple:
        encoding = locale.getpreferredencoding(False)
        with self._lock:
            exceeded = self._exceeded
            buffers = tuple(bytes(value) for value in self._buffers)
        output = []
        for content in buffers:
            if exceeded:
                content = content[:_CAPTURE_PREVIEW_BYTES]
            text = content.decode(encoding, errors="replace")
            if exceeded and len(content) == _CAPTURE_PREVIEW_BYTES:
                text += "\n[ReproMin output preview truncated]\n"
            output.append(text)
        return tuple(output)


class _WindowsJob:
    """Own a Windows Job Object that closes an entire reproduction tree."""

    def __init__(self, kernel32, handle) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._lock = threading.Lock()

    @classmethod
    def create(cls) -> Optional["_WindowsJob"]:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            kernel32.CloseHandle(handle)
            return None
        return cls(kernel32, handle)

    def assign(self, process: subprocess.Popen) -> bool:
        from ctypes import wintypes

        with self._lock:
            if self._handle is None:
                return False
            process_handle = wintypes.HANDLE(int(process._handle))
            return bool(
                self._kernel32.AssignProcessToJobObject(
                    self._handle,
                    process_handle,
                )
            )

    def terminate(self) -> bool:
        with self._lock:
            if self._handle is None:
                return False
            return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _resume_windows_process(process_id: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    )
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    )
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    resumed = False
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        present = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while present:
            if entry.th32OwnerProcessID == process_id:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed = True
                    finally:
                        kernel32.CloseHandle(thread)
            present = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return resumed


class Runner(Protocol):
    def run(self, cwd: Path) -> RunResult:
        ...


class _ProcessRegistry:
    """Track runner processes so a parallel window can cancel every command."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = {}
        self._cancelled = set()
        self._cancellation_started = set()
        self._cancel_requested = False

    def register(
        self,
        process: subprocess.Popen,
        cleanup: Optional[Callable[[], object]],
        activate: Optional[Callable[[], None]] = None,
    ) -> None:
        with self._lock:
            if self._cancel_requested:
                raise _ProcessCancelled("reproduction command was cancelled")
            finished = threading.Event()
            self._active[process] = (cleanup, finished)
            try:
                if activate is not None:
                    activate()
            except BaseException:
                self._active.pop(process, None)
                raise

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._active.pop(process, None)
            self._cancelled.discard(process)
            self._cancellation_started.discard(process)

    def was_cancelled(self, process: subprocess.Popen) -> bool:
        with self._lock:
            return process in self._cancelled

    def wait_for_cancellation(self, process: subprocess.Popen) -> None:
        with self._lock:
            entry = self._active.get(process)
            finished = entry[1] if entry is not None else None
        if finished is not None:
            finished.wait()

    def cancel_all(self) -> None:
        with self._lock:
            self._cancel_requested = True
            active = [
                (process, entry)
                for process, entry in self._active.items()
                if process.poll() is None
            ]
            self._cancelled.update(process for process, _ in active)
            owned = []
            for process, (cleanup, finished) in active:
                if process in self._cancellation_started:
                    continue
                self._cancellation_started.add(process)
                owned.append((process, cleanup, finished))
        for process, _, _ in owned:
            _request_process_termination(process)
        workers = [
            (
                threading.Thread(
                    target=_cancel_registered_process,
                    args=(process, cleanup, finished),
                    name="repomin-process-cancel",
                ),
                process,
                cleanup,
                finished,
            )
            for process, cleanup, finished in owned
        ]
        started = []
        for worker, process, cleanup, finished in workers:
            try:
                worker.start()
            except RuntimeError:
                _cancel_registered_process(process, cleanup, finished)
            else:
                started.append(worker)
        for worker in started:
            worker.join()
        for _, (_, finished) in active:
            finished.wait()


class CommandRunner:
    def __init__(
        self,
        command: str,
        timeout_seconds: float,
        environment: Optional[Mapping[str, str]] = None,
        collect_java_diagnostics: bool = False,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.environment = dict(environment or {})
        self.collect_java_diagnostics = collect_java_diagnostics
        self._process_registry = _ProcessRegistry()

    def cancel(self) -> None:
        self._process_registry.cancel_all()

    def run(self, cwd: Path) -> RunResult:
        cwd_identity = _working_directory_identity(cwd)
        env = os.environ.copy()
        env.update(self.environment)
        env["REPOMIN"] = "1"
        result = _run_process(
            _host_shell_command(self.command),
            cwd,
            env,
            self.timeout_seconds,
            process_registry=self._process_registry,
        )
        _verify_working_directory_identity(cwd, cwd_identity)
        result = replace(result, python_execution_root=str(cwd.resolve()))
        return _attach_diagnostics(result, cwd, self.collect_java_diagnostics)


class DockerRunner:
    def __init__(
        self,
        command: str,
        timeout_seconds: float,
        image: str,
        network: str = "none",
        environment: Optional[Mapping[str, str]] = None,
        collect_java_diagnostics: bool = False,
        cpus: Optional[float] = None,
        memory_bytes: Optional[int] = None,
        pids_limit: int = 512,
        tmpfs_bytes: int = 1024 * 1024 * 1024,
        workspace_limit_bytes: Optional[int] = None,
        executable: Optional[str] = None,
    ) -> None:
        if (
            not isinstance(image, str)
            or not image
            or "\x00" in image
            or image.startswith("-")
            or any(char.isspace() for char in image)
        ):
            raise RunnerError("invalid Docker image reference: %s" % image)
        if network not in {"none", "bridge", "host"}:
            raise RunnerError("unsupported Docker network policy: %s" % network)
        if cpus is not None and (not math.isfinite(cpus) or cpus <= 0):
            raise RunnerError("Docker CPU limit must be greater than zero")
        if memory_bytes is not None and memory_bytes < 6 * 1024 * 1024:
            raise RunnerError("Docker memory limit must be at least 6 MiB")
        if pids_limit < 1:
            raise RunnerError("Docker PID limit must be at least 1")
        if tmpfs_bytes < 1:
            raise RunnerError("Docker tmpfs size must be at least 1 byte")
        if workspace_limit_bytes is not None and workspace_limit_bytes < 1:
            raise RunnerError("Docker workspace limit must be at least 1 byte")
        docker = executable or shutil.which("docker")
        if docker is None:
            raise RunnerError("Docker backend requires the docker CLI")
        self.command = command
        self.timeout_seconds = timeout_seconds
        # ``image`` remains the user-facing reference for compatibility. Once
        # validate() resolves it, command execution is pinned to the immutable
        # local image ID even if the tag is moved later in the session.
        self.image = image
        self.image_reference = image
        self.resolved_image_id: Optional[str] = None
        self.network = network
        self.environment = dict(environment or {})
        self.collect_java_diagnostics = collect_java_diagnostics
        self.cpus = cpus
        self.memory_bytes = memory_bytes
        self.pids_limit = pids_limit
        self.tmpfs_bytes = tmpfs_bytes
        self.workspace_limit_bytes = workspace_limit_bytes
        self.executable = docker
        self._process_registry = _ProcessRegistry()

    def cancel(self) -> None:
        self._process_registry.cancel_all()

    def validate(self) -> None:
        server = _run_check(
            [self.executable, "version", "--format", "{{.Server.Version}}"],
            "Docker daemon is unavailable",
        )
        if not server.strip():
            raise RunnerError("Docker daemon did not report a server version")
        inspect_target = self.resolved_image_id or self.image_reference
        image_id = _run_check(
            [
                self.executable,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                inspect_target,
            ],
            "Docker image is unavailable locally: %s" % inspect_target,
        )
        resolved = image_id.strip()
        if _DOCKER_IMAGE_ID.fullmatch(resolved) is None:
            raise RunnerError(
                "Docker image inspect did not return a valid sha256 image ID for %s"
                % inspect_target
            )
        if self.resolved_image_id is not None and resolved != self.resolved_image_id:
            raise RunnerError(
                "Docker image ID changed while validating the pinned image: %s"
                % self.resolved_image_id
            )
        self.resolved_image_id = resolved

    def build_argv(self, cwd: Path, cidfile: Path) -> Sequence[str]:
        source = str(cwd.absolute())
        if "," in source:
            raise RunnerError("Docker backend does not support commas in source paths")
        command = [
            self.executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--init",
            "--read-only",
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=%d" % self.tmpfs_bytes,
            "--cidfile",
            str(cidfile),
            "--name",
            _docker_container_name(cidfile),
            "--mount",
            "type=bind,source=%s,target=/workspace" % source,
            "--workdir",
            "/workspace",
        ]
        if self.cpus is not None:
            command.extend(["--cpus", str(self.cpus)])
        if self.memory_bytes is not None:
            command.extend(
                [
                    "--memory",
                    str(self.memory_bytes),
                    "--memory-swap",
                    str(self.memory_bytes),
                ]
            )
        if os.name == "posix":
            command.extend(["--user", "%d:%d" % (os.getuid(), os.getgid())])
        for name, value in sorted(self.environment.items()):
            if not _valid_environment_name(name):
                raise RunnerError("invalid container environment variable: %s" % name)
            if not isinstance(value, str):
                raise RunnerError(
                    "invalid container environment value: %s" % name
                )
            if "\x00" in value:
                raise RunnerError(
                    "invalid NUL in container environment variable: %s" % name
                )
            command.extend(["--env", "%s=%s" % (name, value)])
        # Keep these runner-owned values last so an explicit environment map
        # cannot override the fixed container contract.
        command.extend(
            [
                "--env",
                "REPOMIN=1",
                "--env",
                "HOME=/tmp",
            ]
        )
        execution_image = self.resolved_image_id or self.image_reference
        command.extend([execution_image, "/bin/sh", "-c", self.command])
        return command

    def run(self, cwd: Path) -> RunResult:
        cwd_identity = _working_directory_identity(cwd)
        if self.workspace_limit_bytes is not None:
            initial_size = _tree_size(cwd)
            if initial_size > self.workspace_limit_bytes:
                raise RunnerError(
                    "candidate workspace is %d bytes, exceeding the %d-byte limit"
                    % (initial_size, self.workspace_limit_bytes)
                )
        with tempfile.TemporaryDirectory(prefix="repomin-docker-") as directory:
            cidfile = Path(directory) / "container.cid"

            def cleanup():
                return self._remove_container(cidfile)

            result = _run_process(
                self.build_argv(cwd, cidfile),
                cwd,
                os.environ.copy(),
                self.timeout_seconds,
                on_timeout=cleanup,
                resource_check=(
                    lambda: _workspace_limit_reason(cwd, self.workspace_limit_bytes)
                )
                if self.workspace_limit_bytes is not None
                else None,
                on_resource=cleanup,
                on_cancel=cleanup,
                process_registry=self._process_registry,
            )
        if (
            self.memory_bytes is not None
            and result.returncode == 137
            and not result.timed_out
            and not result.resource_exhausted
        ):
            result = replace(
                result,
                resource_exhausted=True,
                resource_reason=(
                    "container exited with status 137 under the %d-byte memory limit"
                    % self.memory_bytes
                ),
            )
        if (
            not result.timed_out
            and not result.resource_exhausted
            and result.returncode == 125
        ):
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RunnerError(
                "Docker execution failed: %s"
                % (detail[0] if detail else "docker returned exit code 125")
            )
        _verify_working_directory_identity(cwd, cwd_identity)
        result = replace(result, python_execution_root="/workspace")
        return _attach_diagnostics(result, cwd, self.collect_java_diagnostics)

    def _remove_container(self, cidfile: Path) -> bool:
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except OSError:
            container_id = ""
        target = (
            container_id
            if _CONTAINER_ID.fullmatch(container_id) is not None
            else _docker_container_name(cidfile)
        )
        try:
            completed = subprocess.run(
                [self.executable, "rm", "-f", target],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def _run_process(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    on_timeout: Optional[Callable[[], None]] = None,
    resource_check: Optional[Callable[[], Optional[str]]] = None,
    on_resource: Optional[Callable[[], None]] = None,
    on_cancel: Optional[Callable[[], object]] = None,
    process_registry: Optional[_ProcessRegistry] = None,
) -> RunResult:
    started = time.monotonic()
    process: Optional[subprocess.Popen] = None
    windows_job: Optional[_WindowsJob] = None
    gate_read: Optional[int] = None
    gate_write: Optional[int] = None
    output = _BoundedOutput()
    output_readers = []
    try:
        windows_job = _WindowsJob.create()
        if os.name == "nt" and windows_job is None:
            raise RunnerError("failed to create a Windows Job Object")
        try:
            creationflags = (
                _WINDOWS_CREATE_SUSPENDED if os.name == "nt" else 0
            )
            command_argv = list(argv)
            popen_options = {}
            if os.name == "posix":
                gate_read, gate_write = os.pipe()
                gate_script = (
                    "IFS= read -r repomin_gate < /dev/fd/%d || exit 125\n"
                    "exec \"$@\""
                    % gate_read
                )
                command_argv = [
                    "/bin/sh",
                    "-c",
                    gate_script,
                    "repomin-gate",
                ] + command_argv
                popen_options["pass_fds"] = (gate_read,)
            process = subprocess.Popen(
                command_argv,
                cwd=str(cwd),
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=os.name == "posix",
                creationflags=creationflags,
                **popen_options,
            )
        except (OSError, ValueError) as exc:
            raise RunnerError(
                "failed to start reproduction command: %s" % exc
            ) from exc
        if windows_job is not None:
            if windows_job.assign(process):
                process._repomin_windows_job = windows_job
            else:
                raise RunnerError(
                    "failed to assign the reproduction command to its Windows Job"
                )

        assert process.stdout is not None
        assert process.stderr is not None
        if os.name == "posix":
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
        else:
            output_readers = _start_output_readers(process, output)

        if gate_read is not None:
            os.close(gate_read)
            gate_read = None

        def activate() -> None:
            nonlocal gate_write
            if os.name == "nt":
                if not _resume_windows_process(process.pid):
                    raise RunnerError("failed to resume the reproduction command")
            elif gate_write is not None:
                descriptor = gate_write
                gate_write = None
                try:
                    os.write(descriptor, b"start\n")
                except BrokenPipeError:
                    # The child closed the gate before activation (it already
                    # exited). Treat this as a failed run rather than an
                    # unhandled exception; the collection loop below will read
                    # the child's exit status.
                    pass
                finally:
                    os.close(descriptor)

        if process_registry is not None:
            process_registry.register(process, on_cancel, activate)
        else:
            activate()
        deadline = started + timeout_seconds
        while True:
            _drain_posix_output(process, output)
            output_reason = output.limit_reason()
            if output_reason is not None:
                _cancel_process(process, on_resource, retry_cleanup=True)
                _drain_process(process)
                _finish_output_capture(process, output, output_readers)
                stdout, stderr = output.text()
                return RunResult(
                    returncode=(
                        process.returncode if process.returncode is not None else 125
                    ),
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.monotonic() - started,
                    resource_exhausted=True,
                    resource_reason=output_reason,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _cancel_process(process, on_timeout, retry_cleanup=True)
                _drain_process(process)
                _finish_output_capture(process, output, output_readers)
                stdout, stderr = output.text()
                return RunResult(
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.monotonic() - started,
                    timed_out=True,
                )
            try:
                process.wait(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                if (
                    process_registry is not None
                    and process_registry.was_cancelled(process)
                ):
                    raise _ProcessCancelled("reproduction command was cancelled")
                _drain_posix_output(process, output)
                reason = output.limit_reason()
                if reason is None and resource_check is not None:
                    reason = resource_check()
                if reason is None:
                    continue
                _cancel_process(process, on_resource, retry_cleanup=True)
                _drain_process(process)
                _finish_output_capture(process, output, output_readers)
                stdout, stderr = output.text()
                return RunResult(
                    returncode=(
                        process.returncode if process.returncode is not None else 125
                    ),
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.monotonic() - started,
                    resource_exhausted=True,
                    resource_reason=reason,
                )
            _drain_posix_output(process, output)
            if (
                process_registry is not None
                and process_registry.was_cancelled(process)
            ):
                raise _ProcessCancelled("reproduction command was cancelled")
            if _finish_completed_process_tree(process, deadline):
                _finish_output_capture(process, output, output_readers)
                stdout, stderr = output.text()
                return RunResult(
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.monotonic() - started,
                    timed_out=True,
                )
            _finish_output_capture(process, output, output_readers)
            reason = output.limit_reason()
            if reason is None and resource_check is not None:
                reason = resource_check()
            stdout, stderr = output.text()
            return RunResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                resource_exhausted=reason is not None,
                resource_reason=reason,
            )
    except BaseException:
        if process is not None:
            externally_cancelled = (
                process_registry is not None
                and process_registry.was_cancelled(process)
            )
            if not externally_cancelled:
                _cancel_process(process, on_cancel, retry_cleanup=True)
            elif process_registry is not None:
                process_registry.wait_for_cancellation(process)
            _drain_process(process)
            _finish_output_capture(process, output, output_readers)
        raise
    finally:
        if windows_job is not None:
            windows_job.close()
        if process is not None and process_registry is not None:
            process_registry.unregister(process)
        for descriptor in (gate_read, gate_write):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            _finish_output_capture(process, output, output_readers)


def _cancel_process(
    process: subprocess.Popen,
    cleanup: Optional[Callable[[], object]] = None,
    retry_cleanup: bool = False,
) -> None:
    _terminate_process_tree(process)
    cleanup_succeeded = _invoke_cleanup(cleanup)
    if retry_cleanup and cleanup is not None and cleanup_succeeded is not True:
        deadline = time.monotonic() + 5.0
        delay = 0.05
        while time.monotonic() < deadline:
            time.sleep(delay)
            if _invoke_cleanup(cleanup) is True:
                break
            delay = min(delay * 2.0, 0.4)


def _cancel_registered_process(
    process: subprocess.Popen,
    cleanup: Optional[Callable[[], object]],
    finished: threading.Event,
) -> None:
    try:
        _cancel_process(process, cleanup, retry_cleanup=True)
    finally:
        finished.set()


def _invoke_cleanup(cleanup: Optional[Callable[[], object]]) -> Optional[object]:
    if cleanup is None:
        return None
    try:
        return cleanup()
    except BaseException:
        return None


def _drain_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _start_output_readers(
    process: subprocess.Popen,
    output: _BoundedOutput,
) -> list:
    readers = []
    for stream_index, stream in enumerate((process.stdout, process.stderr)):
        assert stream is not None
        reader = threading.Thread(
            target=_read_output_stream,
            args=(stream, stream_index, output),
            name="repomin-output-reader",
            daemon=True,
        )
        try:
            reader.start()
        except BaseException:
            for started in readers:
                started.join(timeout=0.1)
            raise
        readers.append(reader)
    return readers


def _read_output_stream(stream, stream_index: int, output: _BoundedOutput) -> None:
    try:
        while True:
            content = stream.read(64 * 1024)
            if not content:
                return
            output.append(stream_index, content)
    except (OSError, ValueError):
        return


def _drain_posix_output(
    process: subprocess.Popen,
    output: _BoundedOutput,
) -> None:
    if os.name != "posix":
        return
    for stream_index, stream in enumerate((process.stdout, process.stderr)):
        if stream is None or stream.closed:
            continue
        drained = 0
        while drained < 1024 * 1024:
            try:
                content = os.read(stream.fileno(), 64 * 1024)
            except BlockingIOError:
                break
            except OSError:
                break
            if not content:
                break
            output.append(stream_index, content)
            drained += len(content)


def _finish_output_capture(
    process: subprocess.Popen,
    output: _BoundedOutput,
    readers: Sequence[threading.Thread],
) -> None:
    _drain_posix_output(process, output)
    if os.name != "posix":
        for reader in readers:
            reader.join(timeout=1)
    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass
    if os.name != "posix":
        for reader in readers:
            reader.join(timeout=0.1)


def _finish_completed_process_tree(
    process: subprocess.Popen,
    deadline: float,
) -> bool:
    if os.name == "posix":
        if not _process_group_exists(process.pid):
            return False
        grace = max(0.0, min(1.0, deadline - time.monotonic()))
        _terminate_process_tree(process, grace_seconds=grace)
        return time.monotonic() >= deadline
    if os.name == "nt":
        job = getattr(process, "_repomin_windows_job", None)
        if job is not None:
            job.terminate()
    return False


def _terminate_process_tree(
    process: subprocess.Popen,
    grace_seconds: float = 1.0,
) -> None:
    _request_process_termination(process)
    if os.name == "posix":
        process_group = process.pid
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            process.poll()
            if not _process_group_exists(process_group):
                break
            time.sleep(0.02)
        if _process_group_exists(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return

    try:
        process.wait(timeout=1)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        pass


def _request_process_termination(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            process.poll()
            return
        except OSError:
            pass
    elif os.name == "nt":
        job = getattr(process, "_repomin_windows_job", None)
        if job is not None and job.terminate():
            return
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _attach_diagnostics(
    result: RunResult,
    cwd: Path,
    enabled: bool,
) -> RunResult:
    if not enabled or result.timed_out or result.resource_exhausted:
        return result
    return replace(result, diagnostics=collect_surefire_diagnostics(cwd))


def _working_directory_identity(cwd: Path) -> tuple[int, int]:
    try:
        metadata = cwd.lstat()
    except OSError as exc:
        raise RunnerError("working directory is unavailable: %s" % exc) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RunnerError("working directory must be a non-symlink directory")
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    if os.name == "nt":
        attributes = getattr(metadata, "st_file_attributes", None)
        if attributes is None or identity[0] == 0 or identity[1] == 0:
            raise RunnerError(
                "working directory identity is unavailable on this filesystem"
            )
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RunnerError("working directory must not be a reparse point")
    return identity


def _verify_working_directory_identity(
    cwd: Path,
    expected: tuple[int, int],
) -> None:
    try:
        actual = _working_directory_identity(cwd)
    except RunnerError as exc:
        raise RunnerError(
            "working directory changed during command execution: %s" % exc
        ) from exc
    if actual != expected:
        raise RunnerError("working directory changed during command execution")


def _host_shell_command(command: str) -> Sequence[str]:
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def _valid_environment_name(name: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None


def _docker_container_name(cidfile: Path) -> str:
    encoded = str(cidfile.absolute()).encode("utf-8", errors="surrogateescape")
    return "repomin-" + hashlib.sha256(encoded).hexdigest()[:32]


def _tree_size(root: Path) -> int:
    size = 0
    pending = [str(root)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                snapshot = list(entries)
        except OSError:
            continue
        for entry in snapshot:
            try:
                if entry.is_symlink():
                    size += len(
                        os.readlink(entry.path).encode(
                            "utf-8",
                            errors="surrogateescape",
                        )
                    )
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return size


def _workspace_limit_reason(root: Path, limit: Optional[int]) -> Optional[str]:
    if limit is None:
        return None
    size = _tree_size(root)
    if size <= limit:
        return None
    return "workspace grew to %d bytes, exceeding the %d-byte limit" % (size, limit)


def _run_check(argv: Sequence[str], message: str) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RunnerError("%s: %s" % (message, exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RunnerError(
            "%s%s"
            % (message, ": " + detail[0] if detail else "")
        )
    return completed.stdout
