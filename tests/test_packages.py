import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from packages import archive_metadata, merge, releases


def fixture(kind="npm", version="1.0.0", path=None):
    manifest = {
        "manifest_version": 1,
        "protocol_version": 1,
        "plugin": {"id": "example", "name": "Example", "version": version},
        "runtime": {"kind": "process", "executables": {"linux-x86_64": "adapter.py"}},
    }
    stream = io.BytesIO()
    source = {
        "kind": kind,
        ("package" if kind == "npm" else "project"): "example-plugin",
    }
    if kind == "npm":
        files = {
            "package/plugin.json": json.dumps(manifest).encode(),
            "package/package.json": json.dumps(
                {
                    "name": "example-plugin",
                    "version": version,
                    "repository": "https://github.com/example/plugin",
                }
            ).encode(),
        }
        if path:
            files[path] = b"untrusted"
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, value in files.items():
                header = tarfile.TarInfo(name)
                header.size = len(value)
                archive.addfile(header, io.BytesIO(value))
        data = stream.getvalue()
        metadata = {
            "dist-tags": {"latest": version},
            "versions": {
                version: {
                    "dist": {
                        "tarball": "https://example.com/archive.tgz",
                        "integrity": "sha512-"
                        + base64.b64encode(hashlib.sha512(data).digest()).decode(),
                    }
                }
            },
        }
    else:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("example_plugin/plugin.json", json.dumps(manifest))
            archive.writestr(
                "example_plugin-1.0.0.dist-info/METADATA",
                f"Name: example-plugin\nVersion: {version}\nProject-URL: Repository, https://github.com/example/plugin\n",
            )
            if path:
                archive.writestr(path, b"untrusted")
        data = stream.getvalue()
        metadata = {
            "info": {"version": version},
            "releases": {
                version: [
                    {
                        "filename": "example_plugin-1.0.0-py3-none-any.whl",
                        "url": "https://example.com/archive.whl",
                        "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                        "yanked": False,
                    }
                ]
            },
        }
    return source, data, metadata


class PackageTests(unittest.TestCase):
    def test_registry_manifests_are_read_and_pinned_without_executing_plugins(self):
        for kind, directory in (("npm", "package"), ("pypi", "example_plugin")):
            with self.subTest(kind=kind):
                source, data, metadata = fixture(kind)
                repository, found = releases(
                    "example", source, lambda _: metadata, lambda _: data
                )
                self.assertEqual(repository, "https://github.com/example/plugin.git")
                self.assertEqual(found[0]["package_directory"], directory)
                self.assertEqual(
                    found[0]["source"]["sha256"], hashlib.sha256(data).hexdigest()
                )
                catalogue = {
                    "catalogue_version": 1,
                    "type": "sloppy.plugin-catalogue",
                    "name": "Test",
                    "plugins": [],
                }
                result = merge(
                    catalogue, {"example": source}, lambda _: metadata, lambda _: data
                )
                self.assertEqual(
                    merge(
                        result, {"example": source}, lambda _: metadata, lambda _: data
                    ),
                    result,
                )
                self.assertEqual(catalogue["plugins"], [])
                changed = deepcopy(result)
                changed["plugins"][0]["versions"][0]["source"]["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "immutable"):
                    merge(
                        changed, {"example": source}, lambda _: metadata, lambda _: data
                    )

    def test_mismatched_hash_and_manifest_identity_abort_generation(self):
        for kind in ("npm", "pypi"):
            source, data, metadata = fixture(kind)
            with self.assertRaisesRegex(ValueError, "mismatch"):
                releases(
                    "example", source, lambda _: metadata, lambda _: data + b"tampered"
                )
            with self.assertRaisesRegex(ValueError, "identity"):
                releases("another-plugin", source, lambda _: metadata, lambda _: data)

    def test_archives_reject_traversal_and_multiple_manifests(self):
        for kind, format in (("npm", "tar.gz"), ("pypi", "zip")):
            for path in ("../escape", "C:/escape", "other/plugin.json"):
                with self.subTest(kind=kind, path=path):
                    _, data, _ = fixture(kind, path=path)
                    with self.assertRaises(ValueError):
                        archive_metadata(data, format)

    def test_prereleases_and_yanked_packages_are_not_advertised(self):
        source, data, metadata = fixture("npm", version="1.0.0-rc.1")
        self.assertEqual(
            releases("example", source, lambda _: metadata, lambda _: data), (None, [])
        )
        source, data, metadata = fixture("pypi")
        metadata["releases"]["1.0.0"][0]["yanked"] = True
        self.assertEqual(
            releases("example", source, lambda _: metadata, lambda _: data), (None, [])
        )
