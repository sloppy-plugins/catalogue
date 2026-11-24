import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "discover", Path(__file__).resolve().parents[1] / "scripts/discover.py"
)
discover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discover)


class DiscoveryTests(unittest.TestCase):
    def test_git_discovery_clones_only_configured_source_and_checks_manifest_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "seed"

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], text=True, stderr=subprocess.PIPE
                ).strip()

            git("init", "-b", "main", str(seed))
            git("-C", str(seed), "config", "user.name", "Test")
            git("-C", str(seed), "config", "user.email", "test@example.com")
            (seed / "plugin.json").write_text(
                json.dumps({"plugin": {"id": "example", "version": "1.0.0"}})
            )
            git("-C", str(seed), "add", ".")
            git("-C", str(seed), "commit", "-m", "Public plugin")
            expected = git("-C", str(seed), "rev-parse", "HEAD")
            source = {
                "kind": "git",
                "repository": "https://github.com/example/plugin.git",
            }
            with patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.{seed.as_uri()}.insteadOf",
                    "GIT_CONFIG_VALUE_0": source["repository"],
                },
            ):
                report = discover.checkout({"example": source}, root / "plugins")
                self.assertEqual(
                    report["plugins"]["example"]["versions"][0]["commit"], expected
                )
                with self.assertRaisesRegex(ValueError, "different plugin"):
                    discover.checkout({"wrong-id": source}, root / "wrong")

    def test_named_git_npm_and_pypi_locations_and_invalid_sources(self):
        sources = {
            "a": {"kind": "git", "repository": "https://github.com/example/plugin.git"},
            "b": {"kind": "npm", "package": "@example/plugin"},
            "c": {"kind": "pypi", "project": "Example_Plugin"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "sources.json"
            file.write_text(json.dumps({"sources_version": 1, "plugins": sources}))
            self.assertEqual(discover.load_sources(file), sources)
            for bad in (
                {"kind": "git", "repository": "https://user:secret@github.com/a/b.git"},
                {"kind": "git", "repository": "file:///tmp/repo"},
                {"kind": "npm", "package": "@scope/plugin", "extra": True},
                {"kind": "pypi", "project": "../escape"},
                {"kind": "unknown", "url": "https://example.com"},
            ):
                file.write_text(
                    json.dumps({"sources_version": 1, "plugins": {"bad": bad}})
                )
                with self.assertRaises(ValueError):
                    discover.load_sources(file)

    def test_npm_discovery_keeps_all_versions_tags_and_distribution_integrity(self):
        urls = []
        dist = {
            "tarball": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
            "integrity": "sha512-example",
        }

        def request(url):
            urls.append(url)
            return {
                "dist-tags": {"latest": "2.0.0"},
                "versions": {
                    "1.0.0": {"dist": dist, "deprecated": "Use v2"},
                    "2.0.0": {"dist": dist},
                },
            }

        result = discover.registry_versions(
            {"kind": "npm", "package": "@scope/plugin"}, request
        )
        self.assertEqual(urls, ["https://registry.npmjs.org/%40scope%2Fplugin"])
        self.assertEqual(result["latest"], "2.0.0")
        self.assertEqual(result["versions"][0]["distribution"], dist)
        self.assertEqual(result["versions"][0]["deprecated"], "Use v2")
        self.assertEqual(len(result["versions"]), 2)

    def test_pypi_discovery_keeps_hashes_and_yanked_status_without_empty_releases(self):
        def request(url):
            self.assertEqual(url, "https://pypi.org/pypi/example-plugin/json")
            return {
                "info": {"version": "1.0.0"},
                "releases": {
                    "0.9.0": [],
                    "1.0.0": [
                        {
                            "filename": "example.whl",
                            "url": "https://files.pythonhosted.org/example.whl",
                            "digests": {"sha256": "a" * 64},
                            "yanked": True,
                        }
                    ],
                },
            }

        result = discover.registry_versions(
            {"kind": "pypi", "project": "example-plugin"}, request
        )
        self.assertEqual(result["latest"], "1.0.0")
        self.assertEqual(len(result["versions"]), 1)
        self.assertTrue(result["versions"][0]["files"][0]["yanked"])
        self.assertEqual(result["versions"][0]["files"][0]["sha256"], "a" * 64)

    def test_registry_discovery_never_clones_or_runs_package_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "plugins"
            with (
                patch.object(
                    discover,
                    "registry_versions",
                    return_value={"latest": "1.0.0", "versions": []},
                ),
                patch.object(discover.subprocess, "run") as run,
            ):
                report = discover.checkout(
                    {"example": {"kind": "npm", "package": "example"}}, destination
                )
            run.assert_not_called()
            self.assertEqual(report["plugins"]["example"]["latest"], "1.0.0")
            (destination / "user-work").touch()
            with self.assertRaisesRegex(ValueError, "must be empty"):
                discover.checkout({}, destination)


if __name__ == "__main__":
    unittest.main()
