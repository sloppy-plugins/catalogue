"""Read immutable plugin manifests from registry archives without executing code."""

import base64
from copy import deepcopy
from email.parser import BytesParser
import gzip
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import tarfile
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

from sloppy_sdk.catalogue import repository_url, validate
from discover import registry_versions

LIMIT = 64 * 1024 * 1024
EXPANDED = 256 * 1024 * 1024
STABLE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def download(url):
    repository_url(url)
    with urlopen(
        Request(url, headers={"User-Agent": "sloppy-catalogue"}), timeout=60
    ) as response:
        repository_url(response.geturl())
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Plugin archive exceeds 64 MB")
    return data


def _archive_metadata(data, format):
    entries = {}
    seen = set()
    total = 0

    def inspect(name, size, directory):
        nonlocal total
        path = PurePosixPath(name)
        if (
            not name
            or len(name) > 512
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("Unsafe path in plugin archive")
        normalized = str(path)
        if normalized == "." and directory:
            return normalized
        if normalized.casefold() in seen or len(seen) >= 20000:
            raise ValueError("Duplicate archive paths or too many entries")
        seen.add(normalized.casefold())
        total += size
        if total > EXPANDED:
            raise ValueError("Plugin archive expands beyond 256 MB")
        return normalized

    def wanted(name):
        return PurePosixPath(name).name in {
            "plugin.json",
            "package.json",
            "PKG-INFO",
            "METADATA",
        }

    if format == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for item in archive.infolist():
                if (item.external_attr >> 16) & 0o170000 not in (0, 0o100000, 0o040000):
                    raise ValueError(
                        "Plugin archives cannot contain links or special files"
                    )
                name = inspect(item.filename, item.file_size, item.is_dir())
                if not item.is_dir() and wanted(name):
                    if item.file_size > 2 * 1024 * 1024:
                        raise ValueError("Plugin archive metadata exceeds 2 MB")
                    entries[name] = archive.read(item)
    elif format == "tar.gz":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            expanded = stream.read(EXPANDED + 1)
        if len(expanded) > EXPANDED:
            raise ValueError("Plugin archive expands beyond 256 MB")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for item in archive:
                if not item.isfile() and not item.isdir():
                    raise ValueError(
                        "Plugin archives cannot contain links or special files"
                    )
                name = inspect(item.name, item.size, item.isdir())
                if item.isfile() and wanted(name):
                    if item.size > 2 * 1024 * 1024:
                        raise ValueError("Plugin archive metadata exceeds 2 MB")
                    with archive.extractfile(item) as stream:
                        entries[name] = stream.read()
    else:
        raise ValueError("Unsupported archive format")
    manifests = [
        (name, json.loads(value))
        for name, value in entries.items()
        if PurePosixPath(name).name == "plugin.json"
    ]
    if len(manifests) != 1:
        raise ValueError("A plugin archive must contain exactly one plugin.json")
    name, manifest = manifests[0]
    return str(PurePosixPath(name).parent), manifest, entries


def archive_metadata(data, format):
    try:
        return _archive_metadata(data, format)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as error:
        raise ValueError(f"Invalid plugin archive: {error}") from error


def same_project(a, b):
    return re.sub(r"[-_.]+", "-", a).lower() == re.sub(r"[-_.]+", "-", b).lower()


def npm_integrity(data, integrity):
    tokens = integrity.split()
    for algorithm in ("sha512", "sha384", "sha256"):
        values = [
            token.split("-", 1)[1]
            for token in tokens
            if token.startswith(algorithm + "-")
        ]
        if values:
            actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
            if actual not in values:
                raise ValueError("npm archive integrity mismatch")
            return
    raise ValueError("npm distributions require SHA-256 or stronger registry integrity")


def releases(plugin_id, source, request, fetch=download):
    inventory = registry_versions(source, request)
    results = []
    repository = None
    for item in inventory["versions"]:
        version = item["version"]
        if not STABLE.fullmatch(version) or item.get("deprecated"):
            continue
        if source["kind"] == "npm":
            distribution = item["distribution"]
            url, format = distribution["tarball"], "tar.gz"
            data = fetch(url)
            npm_integrity(data, distribution.get("integrity", ""))
        else:
            files = [file for file in item["files"] if not file["yanked"]]
            # Universal wheels need no platform selection or build execution.
            candidates = [
                file for file in files if file["filename"].endswith("-py3-none-any.whl")
            ]
            if not candidates:
                candidates = [
                    file for file in files if file["filename"].endswith(".tar.gz")
                ]
            if not candidates:
                continue
            distribution = sorted(candidates, key=lambda f: f["filename"])[0]
            url = distribution["url"]
            format = "zip" if distribution["filename"].endswith(".whl") else "tar.gz"
            data = fetch(url)
            if hashlib.sha256(data).hexdigest() != distribution["sha256"]:
                raise ValueError("PyPI archive SHA-256 mismatch")
        package_directory, manifest, entries = archive_metadata(data, format)
        if (
            manifest["plugin"]["id"] != plugin_id
            or manifest["plugin"]["version"] != version
        ):
            raise ValueError(
                f"{plugin_id}@{version}: archive manifest identity/version mismatch"
            )
        if source["kind"] == "npm":
            package = json.loads(
                entries.get(package_directory + "/package.json", b"{}")
            )
            if (
                package.get("name") != source["package"]
                or package.get("version") != version
            ):
                raise ValueError("npm package identity/version mismatch")
            origin = package.get("repository")
            origin = origin.get("url") if isinstance(origin, dict) else origin
            origin = origin.removeprefix("git+") if isinstance(origin, str) else ""
        else:
            metadata = [
                BytesParser().parsebytes(value)
                for name, value in entries.items()
                if name.endswith(".dist-info/METADATA")
                or PurePosixPath(name).name == "PKG-INFO"
            ]
            metadata = [
                m
                for m in metadata
                if same_project(m.get("Name", ""), source["project"])
                and m.get("Version") == version
            ]
            if not metadata:
                raise ValueError("PyPI project identity/version mismatch")
            urls = dict(
                value.split(", ", 1)
                for value in metadata[0].get_all("Project-URL", [])
                if ", " in value
            )
            origin = urls.get("Repository", urls.get("Source", ""))
        current_repository = repository_url(origin)
        if repository is not None and current_repository != repository:
            raise ValueError("Registry versions disagree on their source repository")
        repository = current_repository
        record = {
            "version": version,
            "package_directory": package_directory,
            "manifest": manifest,
            "source": {
                **source,
                "url": url,
                "sha256": hashlib.sha256(data).hexdigest(),
                "format": format,
            },
        }
        descriptor = manifest["plugin"]
        validate(
            {
                "catalogue_version": 1,
                "type": "sloppy.plugin-catalogue",
                "name": "Candidate",
                "plugins": [
                    {
                        "id": plugin_id,
                        "name": descriptor["name"],
                        "repository": repository,
                        "versions": [record],
                    }
                ],
            }
        )
        results.append(record)
    return repository, results


def merge(catalogue, sources, request, fetch=download):
    result = deepcopy(catalogue)
    for plugin_id, source in sorted(sources.items()):
        if source["kind"] == "git":
            continue
        repository, candidates = releases(plugin_id, source, request, fetch)
        if not candidates:
            raise ValueError(
                f"{plugin_id}: no supported stable registry plugin releases"
            )
        plugin = next((p for p in result["plugins"] if p["id"] == plugin_id), None)
        if plugin is None:
            descriptor = candidates[-1]["manifest"]["plugin"]
            plugin = {
                "id": plugin_id,
                "name": descriptor["name"],
                "description": descriptor.get("description", ""),
                "repository": repository,
                "versions": [],
            }
            result["plugins"].append(plugin)
        elif repository_url(plugin["repository"]) != repository:
            raise ValueError(
                "Registry repository differs from the existing catalogue plugin"
            )
        for candidate in candidates:
            existing = next(
                (r for r in plugin["versions"] if r["version"] == candidate["version"]),
                None,
            )
            if existing is not None:
                if existing != candidate:
                    raise ValueError(
                        f"{plugin_id}@{candidate['version']}: published catalogue releases are immutable"
                    )
                continue
            plugin["versions"].append(candidate)
            print(
                f"{plugin_id}@{candidate['version']}: verified {source['kind']} archive"
            )
    return validate(result)
