"""Fixed, networkless test profiles for Litigation coding agents."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
import hashlib
import re
from pathlib import Path
from typing import Any


PROFILES: dict[str, tuple[str, ...]] = {
    "python-unittest": ("python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
    "python-pytest": ("python3", "-B", "-m", "pytest", "-q"),
    "node-test": ("node", "--test"),
}
MAX_OUTPUT_BYTES = 128 * 1024


class SandboxError(RuntimeError):
    pass


class TestSandbox:
    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = max(1.0, min(float(timeout), 300.0))
        self.bwrap = shutil.which("bwrap")
        if not self.bwrap:
            raise SandboxError("bubblewrap is required for coding-agent tests")

    async def run(self, workspace: Path, profile: str) -> dict[str, Any]:
        command = PROFILES.get(profile)
        if command is None:
            raise SandboxError("unknown test profile")
        executable = shutil.which(command[0])
        if not executable:
            raise SandboxError(f"test profile runtime is unavailable: {profile}")
        started = time.monotonic()
        memory_limit = 3 * 1024 * 1024 * 1024 if profile == "node-test" else 1024 * 1024 * 1024
        runtime_executable = executable
        extra_binds: list[str] = []
        if not executable.startswith(("/usr/", "/bin/")):
            runtime_executable = "/runtime/tool"
            extra_binds = ["--dir", "/runtime", "--ro-bind", executable, runtime_executable]
        inner = ["/usr/bin/prlimit", f"--as={memory_limit}", "--cpu=120", "--nproc=64",
                 "--nofile=256", "--fsize=33554432", "--", runtime_executable, *command[1:]]
        argv = [
            self.bwrap, "--die-with-parent", "--new-session", "--unshare-all",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
        ]
        if Path("/lib64").exists():
            argv += ["--ro-bind", "/lib64", "/lib64"]
        argv += extra_binds
        argv += [
            "--ro-bind", str(workspace), "/workspace", "--proc", "/proc",
            "--dev", "/dev", "--remount-ro", "/dev", "--size", "67108864", "--tmpfs", "/tmp", "--dir", "/home",
            "--dir", "/home/agent", "--chdir", "/workspace",
            "--setenv", "HOME", "/home/agent", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--", *inner,
        ]
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        timed_out = False
        limited = False
        chunks = bytearray()
        async def consume() -> None:
            nonlocal limited
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    return
                room = MAX_OUTPUT_BYTES - len(chunks)
                if room > 0:
                    chunks.extend(chunk[:room])
                if len(chunk) > room:
                    limited = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return
        reader = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(proc.wait(), self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            reader.cancel()
            raise
        try:
            await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 2.0)
        except asyncio.TimeoutError:
            reader.cancel()
        fingerprint = hashlib.sha256()
        file_count = 0
        for path in sorted(workspace.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.name == ".git":
                continue
            relative = path.relative_to(workspace).as_posix()
            raw = path.read_bytes()
            fingerprint.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(raw).digest())
            file_count += 1
        text_output = bytes(chunks).decode("utf-8", "replace")
        test_count = None
        patterns = {
            "python-unittest": r"Ran (\d+) tests? in ",
            "node-test": r"(?m)^(?:#|ℹ) tests (\d+)\s*$",
        }
        if profile in patterns:
            match = re.search(patterns[profile], text_output)
            test_count = int(match.group(1)) if match else None
        status = "timeout" if timed_out else ("output_limit" if limited else ("passed" if proc.returncode == 0 else "failed"))
        if test_count == 0:
            status = "no_tests"
        return {
            "profile": profile,
            "status": status,
            "exit_code": None if timed_out else proc.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": text_output,
            "truncated": limited,
            "network": "disabled",
            "workspace": "read-only",
            "command": list(command),
            "tree_sha256": fingerprint.hexdigest(),
            "file_count": file_count,
            "test_count": test_count,
            "limits": {"seconds": self.timeout, "memory_bytes": memory_limit, "processes": 64,
                       "output_bytes": MAX_OUTPUT_BYTES},
        }
