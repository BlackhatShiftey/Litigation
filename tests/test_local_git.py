from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from local_git import LocalGit, ProjectGitError, validate_code_output


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
    }
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
         "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return result.stdout.decode("utf-8", "strict").strip()


class CodeOutputValidationTests(unittest.TestCase):
    def test_valid_envelope(self) -> None:
        payload = {
            "version": 1,
            "summary": "Update greeting",
            "files": [{
                "path": "src/a.py", "operation": "write",
                "base_sha256": None, "content": "print('hello')\n",
            }],
            "acceptance_notes": ["Added greeting"],
            "checks": [{"name": "unit tests", "status": "not_run"}],
        }
        self.assertEqual(validate_code_output(json.dumps(payload)), payload)

    def test_rejects_traversal_deletes_secrets_and_invalid_checks(self) -> None:
        base = {
            "version": 1, "summary": "Change", "acceptance_notes": [],
            "checks": [{"name": "tests", "status": "not_run"}],
        }
        bad_files = (
            [{"path": "../escape", "operation": "write", "base_sha256": None, "content": "x"}],
            [{"path": "a.txt", "operation": "delete", "base_sha256": None, "content": ""}],
            [{"path": ".env", "operation": "write", "base_sha256": None, "content": "x"}],
            [{"path": "a.txt", "operation": "write", "base_sha256": None,
              "content": 'api_key = "' + "".join(("abcdefgh", "ijklmnop")) + '"'}],
        )
        for files in bad_files:
            with self.subTest(files=files), self.assertRaises(ProjectGitError):
                validate_code_output(json.dumps({**base, "files": files}))
        with self.assertRaisesRegex(ProjectGitError, "invalid checks"):
            validate_code_output(json.dumps({
                **base,
                "files": [{"path": "a.txt", "operation": "write", "base_sha256": None, "content": "x"}],
                "checks": [{"name": "tests", "status": "passed"}],
            }))


class LocalGitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-git-test-")
        self.root = Path(self.tempdir.name)
        self.managed = self.root / "managed"
        self.local_git = LocalGit(self.managed)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_source(self, name: str = "source") -> Path:
        source = self.root / name
        source.mkdir()
        git(source, "init", "--initial-branch=main")
        (source / "a.txt").write_text("original\n", encoding="utf-8")
        git(source, "add", "--", "a.txt")
        git(source, "commit", "-m", "Initial")
        return source

    async def test_create_empty_project(self) -> None:
        created = await self.local_git.create_project("p1")
        self.assertRegex(created["head"], r"^[0-9a-f]{40}$")
        self.assertEqual(created["excluded"], [])
        self.assertEqual(await self.local_git.list_files("p1"), [])
        status_out = await self.local_git.status("p1")
        self.assertEqual(status_out["branch"], "main")
        self.assertTrue(status_out["clean"])

    async def test_imports_only_safe_committed_head_without_touching_source(self) -> None:
        source = self.make_source()
        (source / ".env").write_text("NOT_A_REAL_SECRET=value\n", encoding="utf-8")
        (source / "binary.dat").write_bytes(b"a\x00b")
        (source / "exec.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(source / "exec.sh", 0o755)
        os.symlink("a.txt", source / "link.txt")
        (source / ".gitattributes").write_text("*.txt filter=malicious\n", encoding="utf-8")
        git(source, "add", "--", ".env", "binary.dat", "exec.sh", "link.txt", ".gitattributes")
        git(source, "commit", "-m", "Unsafe entries")
        # This hook and filter must never execute during the read-only import.
        sentinel = self.root / "hook-ran"
        hook = source / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
        os.chmod(hook, 0o755)
        git(source, "config", "filter.malicious.clean", f"touch '{sentinel}'")
        (source / "a.txt").write_text("dirty but not committed\n", encoding="utf-8")
        (source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        before = git(source, "status", "--porcelain=v1", "--untracked-files=all")

        created = await self.local_git.create_project("imported", str(source))

        after = git(source, "status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(after, before)
        self.assertFalse(sentinel.exists())
        files = {item["path"] for item in await self.local_git.list_files("imported")}
        self.assertEqual(files, {"a.txt", ".gitattributes"})
        content = await self.local_git.read_files("imported", ["a.txt"], created["head"])
        self.assertEqual(content[0]["content"], "original\n")
        reasons = {item["reason"] for item in created["excluded"]}
        self.assertIn("working tree changes were not imported", reasons)
        self.assertIn("secret_detected", reasons)
        self.assertIn("non-regular or executable entry skipped", reasons)
        self.assertIn("binary content skipped", reasons)

    async def test_proposal_keeps_main_unchanged_until_fast_forward_apply(self) -> None:
        source = self.make_source()
        created = await self.local_git.create_project("p1", str(source))
        base = created["head"]
        original = await self.local_git.read_files("p1", ["a.txt"], base)
        proposal = await self.local_git.create_proposal(
            "p1", "h1", base,
            [
                {"path": "a.txt", "operation": "write",
                 "base_sha256": original[0]["sha256"], "content": "changed\n"},
                {"path": "new.txt", "operation": "write",
                 "base_sha256": None, "content": "new\n"},
            ],
            "Change files",
        )
        self.assertEqual(proposal["branch"], "codex/h1")
        self.assertEqual(await self.local_git.head("p1"), base)
        still_original = await self.local_git.read_files("p1", ["a.txt"], base)
        self.assertEqual(still_original[0]["content"], "original\n")
        self.assertTrue((self.managed / "p1" / "worktrees" / "h1").is_dir())

        applied = await self.local_git.apply_proposal("p1", proposal, base)

        self.assertEqual(applied, {"previous_head": base, "head": proposal["commit_sha"]})
        current = await self.local_git.read_files("p1", ["a.txt", "new.txt"], applied["head"])
        self.assertEqual([item["content"] for item in current], ["changed\n", "new\n"])

    async def test_literal_git_pathspec_name_is_staged_exactly(self) -> None:
        base = (await self.local_git.create_project("p1"))["head"]
        magic_name = ":(glob)literal.txt"
        proposal = await self.local_git.create_proposal(
            "p1", "h1", base,
            [{"path": magic_name, "operation": "write", "base_sha256": None,
              "content": "literal\n"}],
            "Add literal path",
        )
        self.assertEqual(proposal["changed_paths"], [magic_name])
        applied = await self.local_git.apply_proposal("p1", proposal, base)
        files = await self.local_git.list_files("p1", applied["head"])
        self.assertEqual([item["path"] for item in files], [magic_name])

    async def test_apply_rejects_tampered_proposal_metadata(self) -> None:
        source = self.make_source()
        base = (await self.local_git.create_project("p1", str(source)))["head"]
        original = await self.local_git.read_files("p1", ["a.txt"], base)
        proposal = await self.local_git.create_proposal(
            "p1", "h1", base,
            [{"path": "a.txt", "operation": "write", "base_sha256": original[0]["sha256"],
              "content": "changed\n"}], "Change",
        )
        tampered = {**proposal, "diff_text": proposal["diff_text"] + "tampered"}
        with self.assertRaises(ProjectGitError) as caught:
            await self.local_git.apply_proposal("p1", tampered, base)
        self.assertEqual(caught.exception.category, "invalid_proposal")
        self.assertEqual(await self.local_git.head("p1"), base)

    async def test_rejects_stale_file_hash_before_commit(self) -> None:
        source = self.make_source()
        base = (await self.local_git.create_project("p1", str(source)))["head"]
        with self.assertRaisesRegex(ProjectGitError, "base hash") as caught:
            await self.local_git.create_proposal(
                "p1", "h1", base,
                [{"path": "a.txt", "operation": "write", "base_sha256": "0" * 64,
                  "content": "changed\n"}],
                "Stale",
            )
        self.assertEqual(caught.exception.category, "stale_file")
        self.assertEqual(await self.local_git.head("p1"), base)
        self.assertTrue((self.managed / "p1" / "worktrees" / "h1").is_dir())

    async def test_rejects_stale_or_dirty_main_on_apply(self) -> None:
        source = self.make_source("source1")
        base = (await self.local_git.create_project("p1", str(source)))["head"]
        original = await self.local_git.read_files("p1", ["a.txt"], base)
        proposal = await self.local_git.create_proposal(
            "p1", "h1", base,
            [{"path": "a.txt", "operation": "write", "base_sha256": original[0]["sha256"],
              "content": "proposal\n"}], "Proposal",
        )
        repo = self.managed / "p1" / "repo"
        git(repo, "commit", "--allow-empty", "-m", "Advance main")
        advanced = await self.local_git.head("p1")
        with self.assertRaisesRegex(ProjectGitError, "main changed") as stale:
            await self.local_git.apply_proposal("p1", proposal, base)
        self.assertEqual(stale.exception.category, "stale_base")
        self.assertEqual(await self.local_git.head("p1"), advanced)

        source2 = self.make_source("source2")
        base2 = (await self.local_git.create_project("p2", str(source2)))["head"]
        original2 = await self.local_git.read_files("p2", ["a.txt"], base2)
        proposal2 = await self.local_git.create_proposal(
            "p2", "h2", base2,
            [{"path": "a.txt", "operation": "write", "base_sha256": original2[0]["sha256"],
              "content": "proposal\n"}], "Proposal",
        )
        (self.managed / "p2" / "repo" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectGitError, "uncommitted") as dirty:
            await self.local_git.apply_proposal("p2", proposal2, base2)
        self.assertEqual(dirty.exception.category, "dirty_main")
        self.assertEqual(await self.local_git.head("p2"), base2)

    async def test_managed_repository_hooks_do_not_execute(self) -> None:
        source = self.make_source()
        base = (await self.local_git.create_project("p1", str(source)))["head"]
        repo = self.managed / "p1" / "repo"
        sentinel = self.root / "managed-hook-ran"
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
        os.chmod(hook, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        original = await self.local_git.read_files("p1", ["a.txt"], base)
        proposal = await self.local_git.create_proposal(
            "p1", "h1", base,
            [{"path": "a.txt", "operation": "write", "base_sha256": original[0]["sha256"],
              "content": "safe\n"}], "Safe proposal",
        )
        await self.local_git.apply_proposal("p1", proposal, base)
        self.assertFalse(sentinel.exists())

    async def test_rejects_symlink_tree_and_secret_proposal(self) -> None:
        await self.local_git.create_project("p1")
        base = await self.local_git.head("p1")
        with self.assertRaises(ProjectGitError) as traversal:
            await self.local_git.create_proposal(
                "p1", "h1", base,
                [{"path": "../outside", "operation": "write", "base_sha256": None,
                  "content": "x"}], "Bad",
            )
        self.assertEqual(traversal.exception.category, "invalid_path")
        with self.assertRaises(ProjectGitError) as secret:
            await self.local_git.create_proposal(
                "p1", "h2", base,
                [{"path": "config.pem", "operation": "write", "base_sha256": None,
                  "content": "not actually used"}], "Bad",
            )
        self.assertEqual(secret.exception.category, "secret_detected")


if __name__ == "__main__":
    unittest.main()
