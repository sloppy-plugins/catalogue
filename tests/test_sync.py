import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sloppy_sdk.catalogue import read, write

SPEC = importlib.util.spec_from_file_location(
    "sync_releases", Path(__file__).resolve().parents[1] / "scripts/sync-releases.py"
)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.catalogue = self.root / "catalogue.json"
        write(
            self.catalogue,
            {
                "catalogue_version": 1,
                "type": "sloppy.plugin-catalogue",
                "name": "Test",
                "plugins": [],
            },
        )

    def plugin(self, name="example", plugin_id=None):
        remote = self.root / f"{name}.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        repo = self.plugins / name
        git(self.plugins, "init", "--initial-branch=main", str(repo))
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.com")
        url = f"https://github.com/example/{name}.git"
        git(repo, "remote", "add", "origin", url)
        git(repo, "config", f"url.{remote.as_uri()}.insteadOf", url)
        manifest = {
            "manifest_version": 1,
            "protocol_version": 1,
            "plugin": {
                "id": plugin_id or name,
                "name": name.title(),
                "version": "1.0.0",
            },
            "runtime": {"kind": "process", "executables": {"linux-x86_64": "main.py"}},
        }
        (repo / "plugin.json").write_text(json.dumps(manifest))
        (repo / "main.py").write_text("print('example')\n")
        self.commit(repo)
        return repo

    def commit(self, repo, push=True):
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Example")
        if push:
            git(repo, "push", "origin", "main")

    def test_sync_uses_pushed_hash_and_is_idempotent_without_committing(self):
        repo = self.plugin()
        head = git(repo, "rev-parse", "HEAD")
        sync.synchronize(self.plugins, self.catalogue)
        catalogue = read(self.catalogue)
        self.assertEqual(
            catalogue["plugins"][0]["repository"],
            "https://github.com/example/example.git",
        )
        self.assertEqual(catalogue["plugins"][0]["versions"][0]["commit"], head)
        before = self.catalogue.read_bytes()
        sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(self.catalogue.read_bytes(), before)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_check_only_and_later_invalid_plugin_do_not_write(self):
        self.plugin("a")
        before = self.catalogue.read_bytes()
        sync.synchronize(self.plugins, self.catalogue, check_only=True)
        self.assertEqual(self.catalogue.read_bytes(), before)
        bad = self.plugin("z")
        (bad / "dirty").touch()
        with self.assertRaisesRegex(ValueError, "commit and push"):
            sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(self.catalogue.read_bytes(), before)

    def test_unpushed_changes_are_rejected(self):
        repo = self.plugin()
        (repo / "main.py").write_text("print('new')\n")
        self.commit(repo, push=False)
        with self.assertRaisesRegex(ValueError, "not the pushed GitHub main"):
            sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(read(self.catalogue)["plugins"], [])

    def test_only_configured_git_sources_are_added_and_missing_ones_block_writes(self):
        repo = self.plugin("example")
        self.plugin("unlisted")
        sources = {
            "example": {
                "kind": "git",
                "repository": "https://github.com/example/example.git",
            },
            "future": {"kind": "npm", "package": "@example/future"},
        }
        with patch("packages.merge", side_effect=lambda catalogue, *_: catalogue):
            sync.synchronize(self.plugins, self.catalogue, sources=sources)
        self.assertEqual(
            [p["id"] for p in read(self.catalogue)["plugins"]], ["example"]
        )
        before = self.catalogue.read_bytes()
        sources["missing"] = {
            "kind": "git",
            "repository": "https://github.com/example/missing.git",
        }
        with self.assertRaisesRegex(
            ValueError, "configured Git plugin checkout is missing"
        ):
            sync.synchronize(self.plugins, self.catalogue, sources=sources)
        self.assertEqual(self.catalogue.read_bytes(), before)
        sources.pop("missing")
        git(repo, "remote", "set-url", "origin", "https://github.com/example/wrong.git")
        # Reject the configured identity before attempting a fetch.
        with self.assertRaisesRegex(ValueError, "configured location"):
            sync.synchronize(self.plugins, self.catalogue, sources=sources)

    def test_registry_only_sources_delegate_without_git_checkout_requirements(self):
        before = self.catalogue.read_bytes()
        with patch("packages.merge", side_effect=lambda catalogue, *_: catalogue):
            sync.synchronize(
                self.plugins,
                self.catalogue,
                sources={"future": {"kind": "pypi", "project": "future"}},
            )
        self.assertEqual(self.catalogue.read_bytes(), before)

    def test_published_tags_recover_versions_between_polls(self):
        repo = self.plugin()
        first = git(repo, "rev-parse", "HEAD")
        git(repo, "tag", "-a", "v1.0.0", "-m", "First public version")
        path = repo / "plugin.json"
        manifest = json.loads(path.read_text())
        manifest["plugin"]["version"] = "1.0.1"
        path.write_text(json.dumps(manifest))
        self.commit(repo)
        git(repo, "push", "origin", "--tags")
        sync.synchronize(self.plugins, self.catalogue)
        versions = read(self.catalogue)["plugins"][0]["versions"]
        self.assertEqual([v["version"] for v in versions], ["1.0.0", "1.0.1"])
        self.assertEqual(versions[0]["commit"], first)
        git(repo, "tag", "v2.0.0")
        git(repo, "push", "origin", "--tags")
        before = self.catalogue.read_bytes()
        with self.assertRaisesRegex(ValueError, "disagrees with its manifest"):
            sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(self.catalogue.read_bytes(), before)

    def test_existing_release_is_immutable_and_new_version_preserves_it(self):
        repo = self.plugin()
        sync.synchronize(self.plugins, self.catalogue)
        before = read(self.catalogue)
        (repo / "main.py").write_text("print('new')\n")
        self.commit(repo)
        sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(read(self.catalogue), before)
        path = repo / "plugin.json"
        manifest = json.loads(path.read_text())
        manifest["plugin"]["name"] = "Changed without a version bump"
        path.write_text(json.dumps(manifest))
        self.commit(repo)
        with self.assertRaisesRegex(ValueError, "already released"):
            sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(read(self.catalogue), before)
        manifest["plugin"]["version"] = "1.0.1"
        path.write_text(json.dumps(manifest))
        self.commit(repo)
        sync.synchronize(self.plugins, self.catalogue)
        versions = read(self.catalogue)["plugins"][0]["versions"]
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0], before["plugins"][0]["versions"][0])

    def test_wrong_origin_and_duplicate_ids_abort(self):
        repo = self.plugin("a")
        git(repo, "remote", "set-url", "origin", "https://example.com/a.git")
        with self.assertRaisesRegex(ValueError, "github.com"):
            sync.synchronize(self.plugins, self.catalogue)
        git(repo, "remote", "set-url", "origin", "https://github.com/example/a.git")
        self.plugin("b", plugin_id="a")
        with self.assertRaisesRegex(ValueError, "Duplicate plugin ID"):
            sync.synchronize(self.plugins, self.catalogue)
        self.assertEqual(read(self.catalogue)["plugins"], [])

    def test_empty_directory_and_unborn_repository_are_actionable(self):
        with self.assertRaisesRegex(ValueError, "No plugin repositories"):
            sync.synchronize(self.plugins, self.catalogue)
        repo = self.plugins / "unborn"
        git(self.plugins, "init", "--initial-branch=main", str(repo))
        (repo / "plugin.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "commit and push"):
            sync.synchronize(self.plugins, self.catalogue)


if __name__ == "__main__":
    unittest.main()
