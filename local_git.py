"""Entirely local, hook-disabled Git storage for Litigation projects."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import uuid
from typing import Any


MAX_FILES = 20
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
IMPORT_MAX_FILES = 2000
IMPORT_MAX_FILE_BYTES = 1024 * 1024
IMPORT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
GIT_OUTPUT_LIMIT = 4 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REF_RE = re.compile(r"^(?:main|[0-9a-f]{40}|codex/[A-Za-z0-9._-]{1,64})$")
_SECRET_FILE_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)|"
    r"credentials?(?:\.[^/]*)?|secrets?(?:\.[^/]*)?|[^/]+\.(?:pem|p12|pfx|key))$",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    ),
)


class ProjectGitError(RuntimeError):
    """A sanitized Git or proposal validation error."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class _OutputLimit(RuntimeError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProjectGitError("invalid_identifier", f"invalid {label}")
    return value


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ProjectGitError("invalid_path", "invalid proposal path")
    if ("\x00" in value or "\\" in value or value.startswith("/")
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ProjectGitError("invalid_path", "invalid proposal path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} or part.lower() == ".git" for part in path.parts):
        raise ProjectGitError("invalid_path", "invalid proposal path")
    if any(len(part.encode("utf-8")) > 255 for part in path.parts):
        raise ProjectGitError("invalid_path", "invalid proposal path")
    normalized = path.as_posix()
    if normalized != value:
        raise ProjectGitError("invalid_path", "proposal path must be normalized")
    if _SECRET_FILE_RE.search(normalized):
        raise ProjectGitError("secret_detected", "secret-bearing file path is not allowed")
    return normalized


def _safe_ref(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF_RE.fullmatch(value):
        raise ProjectGitError("invalid_ref", "invalid Git reference")
    return value


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _validate_files(files: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ProjectGitError("invalid_output", f"code output must contain 1-{MAX_FILES} files")
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or item.get("operation") != "write":
            raise ProjectGitError("unsupported_operation", "only file writes are supported")
        path = _safe_path(item.get("path"))
        if path in seen:
            raise ProjectGitError("invalid_output", "duplicate proposal path")
        seen.add(path)
        content = item.get("content")
        if not isinstance(content, str) or "\x00" in content:
            raise ProjectGitError("binary_content", "proposal content must be UTF-8 text")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ProjectGitError("output_too_large", "proposal file exceeds size limit")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ProjectGitError("output_too_large", "proposal exceeds combined size limit")
        if _contains_secret(content):
            raise ProjectGitError("secret_detected", "secret-shaped proposal content is not allowed")
        base_hash = item.get("base_sha256")
        if base_hash is not None and (
            not isinstance(base_hash, str) or not _SHA256_RE.fullmatch(base_hash)
        ):
            raise ProjectGitError("invalid_output", "invalid base_sha256")
        checked.append({
            "path": path, "operation": "write", "base_sha256": base_hash,
            "content": content,
        })
    return checked, total


def validate_code_output(raw_text: str) -> dict[str, Any]:
    """Parse and validate the versioned, write-only code proposal envelope."""
    if not isinstance(raw_text, str) or len(raw_text.encode("utf-8")) > 2 * MAX_TOTAL_BYTES:
        raise ProjectGitError("invalid_output", "code output is missing or too large")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProjectGitError("invalid_output", "code output is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ProjectGitError("invalid_output", "unsupported code output version")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
        raise ProjectGitError("invalid_output", "proposal summary is required")
    files, _ = _validate_files(payload.get("files"))
    result: dict[str, Any] = {"version": 1, "summary": summary.strip(), "files": files}
    notes = payload.get("acceptance_notes", [])
    if (not isinstance(notes, list) or len(notes) > 100
            or any(not isinstance(value, str) or len(value) > 4000 for value in notes)):
        raise ProjectGitError("invalid_output", "invalid acceptance_notes")
    result["acceptance_notes"] = notes
    checks = payload.get("checks", [])
    if (not isinstance(checks, list) or len(checks) > 100
            or any(
                not isinstance(check, dict)
                or set(check) != {"name", "status"}
                or not isinstance(check["name"], str)
                or not check["name"].strip()
                or len(check["name"]) > 500
                or check["status"] != "not_run"
                for check in checks
            )):
        raise ProjectGitError("invalid_output", "invalid checks")
    result["checks"] = checks
    return result


class LocalGit:
    """Managed local Git repositories with no remotes, hooks, or shell commands."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock(self, project_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(project_id, asyncio.Lock())

    def _project_root(self, project_id: str) -> Path:
        return self.root / _safe_id(project_id, "project id")

    def _repo(self, project_id: str) -> Path:
        return self._project_root(project_id) / "repo"

    def _worktree(self, project_id: str, handoff_id: str) -> Path:
        return self._project_root(project_id) / "worktrees" / _safe_id(handoff_id, "handoff id")

    async def prepare_agent_workspace(self, project_id: str, handoff_id: str, working_base: str) -> Path:
        project_id = _safe_id(project_id, "project id")
        handoff_id = _safe_id(handoff_id, "handoff id")
        if not _SHA_RE.fullmatch(working_base):
            raise ProjectGitError("invalid_ref", "agent working base is invalid")
        lock = await self._lock(project_id)
        async with lock:
            repo, worktree = self._repo(project_id), self._worktree(project_id, handoff_id)
            await self._run_git(repo, ["cat-file", "-e", f"{working_base}^{{commit}}"])
            if worktree.exists():
                _, head, _ = await self._run_git(worktree, ["rev-parse", "HEAD"])
                if head.decode("ascii", "strict").strip() != working_base:
                    raise ProjectGitError("workspace_exists", "agent workspace base does not match")
                return worktree
            worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await self._run_git(repo, ["worktree", "add", "-b", f"codex/{handoff_id}", str(worktree), working_base])
            return worktree

    async def agent_list_files(self, project_id: str, handoff_id: str) -> list[dict[str, Any]]:
        project_id, handoff_id = _safe_id(project_id, "project id"), _safe_id(handoff_id, "handoff id")
        lock = await self._lock(project_id)
        async with lock:
            worktree = self._worktree(project_id, handoff_id)
            _, listing, _ = await self._run_git(
                worktree, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                output_limit=MAX_TOTAL_BYTES,
            )
            result, total = [], 0
            for raw_path in (item for item in listing.split(b"\0") if item):
                path = _safe_path(raw_path.decode("utf-8", "strict"))
                self._assert_no_symlink(worktree, path)
                raw = (worktree / path).read_bytes()
                total += len(raw)
                if len(result) >= IMPORT_MAX_FILES or total > IMPORT_MAX_TOTAL_BYTES:
                    raise ProjectGitError("project_too_large", "agent workspace exceeds listing limits")
                result.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
            return result

    async def agent_read_files(self, project_id: str, handoff_id: str, paths: list[str]) -> list[dict[str, Any]]:
        if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES:
            raise ProjectGitError("invalid_paths", f"read requires 1-{MAX_FILES} paths")
        project_id, handoff_id = _safe_id(project_id, "project id"), _safe_id(handoff_id, "handoff id")
        lock = await self._lock(project_id)
        async with lock:
            worktree = self._worktree(project_id, handoff_id)
            result, total = [], 0
            for value in paths:
                path = _safe_path(value)
                self._assert_no_symlink(worktree, path)
                target = worktree / path
                if not target.is_file():
                    raise ProjectGitError("file_not_found", "requested agent file is not present")
                raw = target.read_bytes()
                total += len(raw)
                if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ProjectGitError("read_too_large", "requested agent files exceed limits")
                try:
                    content = raw.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise ProjectGitError("binary_content", "requested file is not UTF-8 text") from exc
                result.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "content": content})
            return result

    async def agent_tree_fingerprint(self, project_id: str, handoff_id: str) -> str:
        items = await self.agent_list_files(project_id, handoff_id)
        digest = hashlib.sha256()
        worktree = self._worktree(_safe_id(project_id, "project id"), _safe_id(handoff_id, "handoff id"))
        for item in sorted(items, key=lambda value: value["path"]):
            raw = (worktree / item["path"]).read_bytes()
            digest.update(item["path"].encode("utf-8") + b"\0" + hashlib.sha256(raw).digest())
        return digest.hexdigest()

    async def agent_write_file(self, project_id: str, handoff_id: str, path_value: Any,
                               content: Any, base_sha256: Any) -> None:
        item = _validate_files([{"path": path_value, "operation": "write", "content": content,
                                 "base_sha256": base_sha256}])[0][0]
        project_id, handoff_id = _safe_id(project_id, "project id"), _safe_id(handoff_id, "handoff id")
        lock = await self._lock(project_id)
        async with lock:
            worktree, path = self._worktree(project_id, handoff_id), item["path"]
            if PurePosixPath(path).name == ".gitignore":
                raise ProjectGitError("unsupported_operation", "coding agents cannot change ignore rules")
            ignored, _, _ = await self._run_git(
                worktree, ["--no-literal-pathspecs", "check-ignore", "-q", "--", path], check=False
            )
            if ignored == 0:
                raise ProjectGitError("ignored_path", "agent writes cannot target ignored paths")
            if ignored != 1:
                raise ProjectGitError("git_failed", "could not validate the agent path")
            self._assert_no_symlink(worktree, path)
            target = worktree / path
            if target.exists():
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != item["base_sha256"]:
                    raise ProjectGitError("stale_file", "agent file hash does not match")
            elif item["base_sha256"] is not None:
                raise ProjectGitError("stale_file", "new agent file must have a null base hash")
            target.parent.mkdir(parents=True, exist_ok=True)
            self._assert_no_symlink(worktree, path)
            target.write_text(item["content"], encoding="utf-8", newline="")
            os.chmod(target, 0o644)

    async def finish_agent_workspace(self, project_id: str, handoff_id: str, integration_base: str,
                                     working_base: str, summary: str) -> dict[str, Any]:
        """Commit agent writes and validate the entire cumulative safe diff."""
        project_id, handoff_id = _safe_id(project_id, "project id"), _safe_id(handoff_id, "handoff id")
        if not _SHA_RE.fullmatch(integration_base) or not _SHA_RE.fullmatch(working_base):
            raise ProjectGitError("invalid_ref", "agent lineage commit is invalid")
        if not summary or len(summary) > 4000:
            raise ProjectGitError("invalid_output", "agent finish summary is required")
        lock = await self._lock(project_id)
        async with lock:
            repo, worktree = self._repo(project_id), self._worktree(project_id, handoff_id)
            rc, _, _ = await self._run_git(repo, ["merge-base", "--is-ancestor", integration_base, working_base], check=False)
            if rc != 0:
                raise ProjectGitError("invalid_lineage", "working base is outside the integration lineage")
            _, status_raw, _ = await self._run_git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
            changed = []
            for record in (item for item in status_raw.split(b"\0") if item):
                try:
                    line = record.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise ProjectGitError("invalid_path", "agent workspace contains an invalid path") from exc
                if len(line) < 4 or line[:2].strip() not in {"M", "??", "A"}:
                    raise ProjectGitError("unsupported_operation", "agent workspace contains an unsupported change")
                changed.append(_safe_path(line[3:]))
            if not changed:
                return None
            if len(changed) > MAX_FILES:
                raise ProjectGitError("output_too_large", "agent changed too many files")
            await self._run_git(worktree, ["add", "--", *changed])
            await self._run_git(worktree, ["commit", "-m", summary, "--", *changed], output_limit=1024 * 1024)
            commit_sha = await self._head_unlocked(worktree)
            rc, _, _ = await self._run_git(repo, ["merge-base", "--is-ancestor", working_base, commit_sha], check=False)
            if rc != 0:
                raise ProjectGitError("invalid_lineage", "agent commit does not descend from its working base")
            _, names_raw, _ = await self._run_git(repo, ["diff", "--name-status", "-z", "--no-renames",
                                                      integration_base, commit_sha, "--"], output_limit=MAX_TOTAL_BYTES)
            fields = [field for field in names_raw.split(b"\0") if field]
            if len(fields) % 2 or not fields or len(fields) // 2 > MAX_FILES:
                raise ProjectGitError("output_too_large", "cumulative proposal exceeds file limits")
            cumulative: list[str] = []
            for index in range(0, len(fields), 2):
                if fields[index] not in {b"A", b"M"}:
                    raise ProjectGitError("unsupported_operation", "cumulative proposal contains an unsupported change")
                cumulative.append(_safe_path(fields[index + 1].decode("utf-8", "strict")))
            files = await self._read_files_unlocked(repo, cumulative, commit_sha)
            if sum(len(item["content"].encode("utf-8")) for item in files) > MAX_TOTAL_BYTES:
                raise ProjectGitError("output_too_large", "cumulative proposal exceeds size limits")
            if any(_contains_secret(item["content"]) for item in files):
                raise ProjectGitError("secret_detected", "cumulative proposal contains secret-shaped content")
            _, diff_raw, _ = await self._run_git(repo, ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                                    "--full-index", integration_base, commit_sha, "--", *cumulative],
                                                  output_limit=2 * MAX_TOTAL_BYTES)
            return {"id": uuid.uuid4().hex, "base_commit": integration_base, "working_base": working_base,
                    "branch": f"codex/{handoff_id}", "commit_sha": commit_sha, "summary": summary,
                    "diff_text": diff_raw.decode("utf-8", "strict"), "changed_paths": cumulative}

    @staticmethod
    def _git_env() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_AUTHOR_NAME": "Litigation",
            "GIT_AUTHOR_EMAIL": "litigation@localhost",
            "GIT_COMMITTER_NAME": "Litigation",
            "GIT_COMMITTER_EMAIL": "litigation@localhost",
        })
        return env

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
        data = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise _OutputLimit

    @staticmethod
    async def _kill_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    async def _run_git(
        self,
        cwd: Path,
        args: list[str],
        *,
        timeout: float = 30.0,
        output_limit: int = GIT_OUTPUT_LIMIT,
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        command = [
            "git", "--no-pager",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
            "-c", "protocol.allow=never",
            "-c", "core.fsmonitor=false",
            "-c", "diff.external=",
            "-C", str(cwd), *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._git_env(),
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        out_task = asyncio.create_task(self._read_limited(proc.stdout, output_limit))
        err_task = asyncio.create_task(self._read_limited(proc.stderr, min(output_limit, 256 * 1024)))
        wait_task = asyncio.create_task(proc.wait())
        try:
            await asyncio.wait_for(
                asyncio.gather(wait_task, out_task, err_task), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            await self._kill_group(proc)
            for task in (out_task, err_task, wait_task):
                task.cancel()
            raise ProjectGitError("git_timeout", "local Git operation timed out") from exc
        except _OutputLimit as exc:
            await self._kill_group(proc)
            for task in (out_task, err_task, wait_task):
                task.cancel()
            raise ProjectGitError("git_output_limit", "local Git output exceeded its limit") from exc
        except asyncio.CancelledError:
            await self._kill_group(proc)
            for task in (out_task, err_task, wait_task):
                task.cancel()
            raise
        stdout, stderr = out_task.result(), err_task.result()
        if check and proc.returncode != 0:
            raise ProjectGitError("git_failed", "local Git operation failed")
        return int(proc.returncode or 0), stdout, stderr

    async def create_project(self, project_id: str, source_path: str | None = None) -> dict[str, Any]:
        project_id = _safe_id(project_id, "project id")
        lock = await self._lock(project_id)
        async with lock:
            project_root = self._project_root(project_id)
            repo = self._repo(project_id)
            if project_root.exists():
                raise ProjectGitError("project_exists", "managed project already exists")
            project_root.mkdir(parents=True, mode=0o700)
            (project_root / "worktrees").mkdir(mode=0o700)
            await self._run_git(project_root, ["init", "--initial-branch=main", "repo"])
            await self._run_git(repo, ["commit", "--allow-empty", "-m", "Initialize Litigation project"])
            excluded: list[dict[str, str]] = []
            source_description = ""
            if source_path:
                source_description, excluded = await self._import_head(repo, source_path)
            head = await self._head_unlocked(repo)
            return {"head": head, "source_description": source_description, "excluded": excluded}

    async def _import_head(
        self, repo: Path, source_path: str
    ) -> tuple[str, list[dict[str, str]]]:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ProjectGitError("invalid_source", "local source must be a directory")
        _, top_raw, _ = await self._run_git(source, ["rev-parse", "--show-toplevel"])
        top = Path(top_raw.decode("utf-8", "strict").strip()).resolve(strict=True)
        if top != source:
            source = top
        _, source_head_raw, _ = await self._run_git(source, ["rev-parse", "HEAD"])
        source_head = source_head_raw.decode("ascii", "strict").strip()
        if not _SHA_RE.fullmatch(source_head):
            raise ProjectGitError("invalid_source", "local source HEAD is invalid")
        _, status_raw, _ = await self._run_git(
            source, ["status", "--porcelain=v1", "--untracked-files=all"]
        )
        excluded: list[dict[str, str]] = []
        if status_raw:
            excluded.append({"path": "", "reason": "working tree changes were not imported"})
        _, listing, _ = await self._run_git(
            source, ["ls-tree", "-r", "-z", "-l", source_head, "--"],
            output_limit=8 * 1024 * 1024,
        )
        imported: list[str] = []
        total = 0
        records = [record for record in listing.split(b"\0") if record]
        if len(records) > IMPORT_MAX_FILES:
            raise ProjectGitError("import_too_large", "source contains too many tracked files")
        for record in records:
            try:
                header, path_raw = record.split(b"\t", 1)
                mode, object_type, object_id, size_raw = header.split(b" ", 3)
                path = path_raw.decode("utf-8", "strict")
            except (ValueError, UnicodeDecodeError):
                excluded.append({"path": "", "reason": "non-UTF-8 tree entry skipped"})
                continue
            try:
                safe_path = _safe_path(path)
            except ProjectGitError as exc:
                excluded.append({"path": path[:240], "reason": exc.category})
                continue
            if mode != b"100644" or object_type != b"blob":
                excluded.append({"path": safe_path, "reason": "non-regular or executable entry skipped"})
                continue
            try:
                size = int(size_raw)
            except ValueError:
                excluded.append({"path": safe_path, "reason": "unknown blob size"})
                continue
            if size > IMPORT_MAX_FILE_BYTES:
                excluded.append({"path": safe_path, "reason": "file exceeds import size limit"})
                continue
            if total + size > IMPORT_MAX_TOTAL_BYTES:
                excluded.append({"path": safe_path, "reason": "combined import size limit reached"})
                continue
            object_text = object_id.decode("ascii", "strict")
            if not _SHA_RE.fullmatch(object_text):
                excluded.append({"path": safe_path, "reason": "invalid blob id"})
                continue
            _, content, _ = await self._run_git(
                source, ["cat-file", "blob", object_text],
                output_limit=IMPORT_MAX_FILE_BYTES + 1,
            )
            try:
                text = content.decode("utf-8", "strict")
            except UnicodeDecodeError:
                excluded.append({"path": safe_path, "reason": "non-UTF-8 content skipped"})
                continue
            if "\x00" in text:
                excluded.append({"path": safe_path, "reason": "binary content skipped"})
                continue
            if _contains_secret(text):
                excluded.append({"path": safe_path, "reason": "secret-shaped content skipped"})
                continue
            destination = repo / safe_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8", newline="")
            os.chmod(destination, 0o644)
            imported.append(safe_path)
            total += size
        if imported:
            for start in range(0, len(imported), 100):
                await self._run_git(repo, ["add", "--", *imported[start:start + 100]])
            await self._run_git(
                repo, ["commit", "-m", f"Import local HEAD {source_head[:12]}", "--", *imported],
                output_limit=1024 * 1024,
            )
        return (
            f"Imported committed local HEAD {source_head[:12]}; source remained unchanged.",
            excluded,
        )

    async def _head_unlocked(self, repo: Path) -> str:
        _, stdout, _ = await self._run_git(repo, ["rev-parse", "HEAD"])
        head = stdout.decode("ascii", "strict").strip()
        if not _SHA_RE.fullmatch(head):
            raise ProjectGitError("git_failed", "managed repository HEAD is invalid")
        return head

    async def head(self, project_id: str) -> str:
        lock = await self._lock(_safe_id(project_id, "project id"))
        async with lock:
            return await self._head_unlocked(self._repo(project_id))

    async def status(self, project_id: str) -> dict[str, Any]:
        lock = await self._lock(_safe_id(project_id, "project id"))
        async with lock:
            repo = self._repo(project_id)
            head = await self._head_unlocked(repo)
            _, branch_raw, _ = await self._run_git(repo, ["symbolic-ref", "--short", "HEAD"])
            _, state, _ = await self._run_git(
                repo, ["status", "--porcelain=v1", "--untracked-files=all"]
            )
            return {
                "head": head,
                "branch": branch_raw.decode("utf-8", "strict").strip(),
                "clean": not bool(state),
            }

    async def _tree_entries(self, repo: Path, ref: str) -> list[tuple[str, str, int, str]]:
        ref = _safe_ref(ref)
        _, listing, _ = await self._run_git(
            repo, ["ls-tree", "-r", "-z", "-l", ref, "--"],
            output_limit=8 * 1024 * 1024,
        )
        entries: list[tuple[str, str, int, str]] = []
        for record in (item for item in listing.split(b"\0") if item):
            try:
                header, path_raw = record.split(b"\t", 1)
                mode, object_type, object_id, size_raw = header.split(b" ", 3)
                path = path_raw.decode("utf-8", "strict")
                size = int(size_raw)
            except (ValueError, UnicodeDecodeError):
                raise ProjectGitError("invalid_repository", "managed tree contains an invalid entry")
            if mode != b"100644" or object_type != b"blob":
                raise ProjectGitError("invalid_repository", "managed tree contains unsupported entries")
            entries.append((path, object_id.decode("ascii", "strict"), size, mode.decode("ascii")))
        return entries

    async def list_files(self, project_id: str, ref: str | None = None) -> list[dict[str, Any]]:
        lock = await self._lock(_safe_id(project_id, "project id"))
        async with lock:
            repo = self._repo(project_id)
            selected = _safe_ref(ref or await self._head_unlocked(repo))
            entries = await self._tree_entries(repo, selected)
            if len(entries) > IMPORT_MAX_FILES or sum(entry[2] for entry in entries) > IMPORT_MAX_TOTAL_BYTES:
                raise ProjectGitError("project_too_large", "managed project exceeds listing limits")
            result: list[dict[str, Any]] = []
            for path, object_id, size, _ in entries:
                _, content, _ = await self._run_git(
                    repo, ["cat-file", "blob", object_id],
                    output_limit=min(IMPORT_MAX_FILE_BYTES + 1, max(size + 1, 1)),
                )
                result.append({
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": size,
                })
            return result

    async def read_files(
        self, project_id: str, paths: list[str], ref: str
    ) -> list[dict[str, Any]]:
        if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES:
            raise ProjectGitError("invalid_paths", f"read requires 1-{MAX_FILES} paths")
        lock = await self._lock(_safe_id(project_id, "project id"))
        async with lock:
            repo = self._repo(project_id)
            selected = _safe_ref(ref)
            return await self._read_files_unlocked(repo, paths, selected)

    async def _read_files_unlocked(
        self, repo: Path, paths: list[str], ref: str
    ) -> list[dict[str, Any]]:
        entries = {path: (object_id, size, mode) for path, object_id, size, mode
                   in await self._tree_entries(repo, ref)}
        result: list[dict[str, Any]] = []
        total = 0
        for requested in paths:
            path = _safe_path(requested)
            if path not in entries:
                raise ProjectGitError("file_not_found", "requested file is not present at the selected ref")
            object_id, size, _ = entries[path]
            if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                raise ProjectGitError("read_too_large", "requested file context exceeds limits")
            _, content, _ = await self._run_git(
                repo, ["cat-file", "blob", object_id], output_limit=MAX_FILE_BYTES + 1
            )
            try:
                text = content.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ProjectGitError("binary_content", "requested file is not UTF-8 text") from exc
            result.append({
                "path": path, "sha256": hashlib.sha256(content).hexdigest(),
                "content": text,
            })
            total += size
        return result

    @staticmethod
    def _assert_no_symlink(root: Path, relative: str) -> None:
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ProjectGitError("symlink_rejected", "proposal path crosses a symlink")

    async def create_proposal(
        self,
        project_id: str,
        handoff_id: str,
        base_commit: str,
        files: list[dict[str, Any]],
        summary: str,
    ) -> dict[str, Any]:
        project_id = _safe_id(project_id, "project id")
        handoff_id = _safe_id(handoff_id, "handoff id")
        if not _SHA_RE.fullmatch(base_commit):
            raise ProjectGitError("invalid_ref", "proposal base commit is invalid")
        checked, _ = _validate_files(files)
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ProjectGitError("invalid_output", "proposal summary is required")
        lock = await self._lock(project_id)
        async with lock:
            repo = self._repo(project_id)
            await self._run_git(repo, ["cat-file", "-e", f"{base_commit}^{{commit}}"])
            branch = f"codex/{handoff_id}"
            worktree = self._worktree(project_id, handoff_id)
            if worktree.exists():
                raise ProjectGitError("proposal_exists", "proposal worktree already exists")
            worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await self._run_git(
                repo, ["worktree", "add", "-b", branch, str(worktree), base_commit]
            )
            changed: list[str] = []
            for item in checked:
                path = item["path"]
                try:
                    existing = await self._read_files_unlocked(repo, [path], base_commit)
                except ProjectGitError as exc:
                    if exc.category != "file_not_found":
                        raise
                    existing = []
                if existing:
                    if item["base_sha256"] != existing[0]["sha256"]:
                        raise ProjectGitError("stale_file", "proposal base hash does not match")
                elif item["base_sha256"] is not None:
                    raise ProjectGitError("stale_file", "new proposal file must have a null base hash")
                self._assert_no_symlink(worktree, path)
                destination = worktree / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._assert_no_symlink(worktree, path)
                destination.write_text(item["content"], encoding="utf-8", newline="")
                os.chmod(destination, 0o644)
                changed.append(path)
            for start in range(0, len(changed), 100):
                await self._run_git(worktree, ["add", "--", *changed[start:start + 100]])
            rc, _, _ = await self._run_git(
                worktree, ["diff", "--cached", "--quiet", "--exit-code"], check=False
            )
            if rc == 0:
                raise ProjectGitError("empty_proposal", "proposal does not change any files")
            if rc != 1:
                raise ProjectGitError("git_failed", "could not inspect proposal changes")
            await self._run_git(
                worktree, ["commit", "-m", summary.strip(), "--", *changed],
                output_limit=1024 * 1024,
            )
            commit_sha = await self._head_unlocked(worktree)
            _, diff_raw, _ = await self._run_git(
                repo,
                ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                 base_commit, commit_sha, "--", *changed],
                output_limit=2 * MAX_TOTAL_BYTES,
            )
            try:
                diff_text = diff_raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ProjectGitError("binary_content", "proposal diff is not UTF-8 text") from exc
            return {
                "id": uuid.uuid4().hex,
                "base_commit": base_commit,
                "branch": branch,
                "commit_sha": commit_sha,
                "summary": summary.strip(),
                "diff_text": diff_text,
                "changed_paths": changed,
            }

    async def apply_proposal(
        self, project_id: str, proposal: dict[str, Any], expected_main: str
    ) -> dict[str, Any]:
        project_id = _safe_id(project_id, "project id")
        if not isinstance(proposal, dict):
            raise ProjectGitError("invalid_proposal", "proposal metadata is invalid")
        base = proposal.get("base_commit")
        commit_sha = proposal.get("commit_sha")
        branch = proposal.get("branch")
        if (not isinstance(base, str) or not _SHA_RE.fullmatch(base)
                or not isinstance(commit_sha, str) or not _SHA_RE.fullmatch(commit_sha)
                or not isinstance(branch, str) or not re.fullmatch(r"codex/[A-Za-z0-9._-]{1,64}", branch)
                or not _SHA_RE.fullmatch(expected_main)):
            raise ProjectGitError("invalid_proposal", "proposal metadata is invalid")
        changed_paths = proposal.get("changed_paths")
        if (not isinstance(changed_paths, list) or not changed_paths
                or len(changed_paths) > MAX_FILES or len(set(changed_paths)) != len(changed_paths)):
            raise ProjectGitError("invalid_proposal", "proposal changed paths are missing")
        for path in changed_paths:
            _safe_path(path)
        lock = await self._lock(project_id)
        async with lock:
            repo = self._repo(project_id)
            current = await self._head_unlocked(repo)
            if current != expected_main or current != base:
                raise ProjectGitError("stale_base", "main changed after the proposal was created")
            _, branch_name, _ = await self._run_git(repo, ["symbolic-ref", "--short", "HEAD"])
            if branch_name.decode("utf-8", "strict").strip() != "main":
                raise ProjectGitError("invalid_repository", "managed repository is not on main")
            _, dirty, _ = await self._run_git(
                repo, ["status", "--porcelain=v1", "--untracked-files=all"]
            )
            if dirty:
                raise ProjectGitError("dirty_main", "managed main has uncommitted changes")
            _, branch_head, _ = await self._run_git(repo, ["rev-parse", branch])
            if branch_head.decode("ascii", "strict").strip() != commit_sha:
                raise ProjectGitError("invalid_proposal", "proposal branch no longer matches metadata")
            rc, _, _ = await self._run_git(
                repo, ["merge-base", "--is-ancestor", current, commit_sha], check=False
            )
            if rc != 0:
                raise ProjectGitError("stale_base", "proposal is not a fast-forward of main")
            _, names_raw, _ = await self._run_git(
                repo,
                ["diff", "--name-status", "-z", "--no-ext-diff", "--no-textconv",
                 "--no-renames", base, commit_sha, "--"],
                output_limit=MAX_TOTAL_BYTES,
            )
            fields = [field for field in names_raw.split(b"\0") if field]
            if len(fields) % 2:
                raise ProjectGitError("invalid_proposal", "proposal change metadata is invalid")
            actual_paths: list[str] = []
            for index in range(0, len(fields), 2):
                status = fields[index]
                if status not in {b"A", b"M"}:
                    raise ProjectGitError("unsupported_operation", "proposal contains an unsupported change")
                try:
                    actual_paths.append(_safe_path(fields[index + 1].decode("utf-8", "strict")))
                except UnicodeDecodeError as exc:
                    raise ProjectGitError("invalid_path", "proposal contains an invalid path") from exc
            if set(actual_paths) != set(changed_paths) or len(actual_paths) != len(changed_paths):
                raise ProjectGitError("invalid_proposal", "proposal changed paths do not match metadata")
            checked_files = await self._read_files_unlocked(repo, actual_paths, commit_sha)
            if any(_contains_secret(item["content"]) for item in checked_files):
                raise ProjectGitError("secret_detected", "proposal contains secret-shaped content")
            expected_diff = proposal.get("diff_text")
            if not isinstance(expected_diff, str):
                raise ProjectGitError("invalid_proposal", "proposal diff metadata is missing")
            _, actual_diff_raw, _ = await self._run_git(
                repo,
                ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                 base, commit_sha, "--", *changed_paths],
                output_limit=2 * MAX_TOTAL_BYTES,
            )
            try:
                actual_diff = actual_diff_raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ProjectGitError("binary_content", "proposal diff is not UTF-8 text") from exc
            if actual_diff != expected_diff:
                raise ProjectGitError("invalid_proposal", "proposal diff no longer matches metadata")
            await self._run_git(repo, ["merge", "--ff-only", "--no-edit", commit_sha])
            new_head = await self._head_unlocked(repo)
            if new_head != commit_sha:
                raise ProjectGitError("git_failed", "proposal apply did not reach the expected commit")
            return {"previous_head": current, "head": new_head}
