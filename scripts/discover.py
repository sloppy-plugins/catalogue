"""Discover versions at named Git, npm and PyPI locations without running plugins."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def request_json(url):
    request = Request(
        url, headers={"User-Agent": "sloppy-catalogue", "Accept": "application/json"}
    )
    with urlopen(request, timeout=30) as response:
        body = response.read(32 * 1024 * 1024 + 1)
    if len(body) > 32 * 1024 * 1024:
        raise ValueError("Registry metadata exceeds 32 MB")
    return json.loads(body)


def load_sources(file):
    data = json.loads(file.read_text())
    if set(data) != {"sources_version", "plugins"} or data["sources_version"] != 1:
        raise ValueError("Expected version-1 source locations")
    sources = data["plugins"]
    if not isinstance(sources, dict) or not sources or len(sources) > 512:
        raise ValueError("Expected 1–512 named plugin locations")
    for name, source in sources.items():
        if not re.fullmatch(r"[a-z0-9-]{1,64}", name) or not isinstance(source, dict):
            raise ValueError("Expected a valid plugin ID and location object")
        kind = source.get("kind")
        fields = {
            "git": {"kind", "repository"},
            "npm": {"kind", "package"},
            "pypi": {"kind", "project"},
        }
        if kind not in fields or set(source) != fields[kind]:
            raise ValueError(f"{name}: expected a Git, npm or PyPI location")
        if kind == "git" and not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
            source["repository"],
        ):
            raise ValueError(f"{name}: expected a public GitHub HTTPS repository")
        if kind == "npm" and not re.fullmatch(
            r"(?:@[a-z0-9_.-]+/)?[a-z0-9][a-z0-9_.-]*", source["package"]
        ):
            raise ValueError(f"{name}: invalid npm package")
        if kind == "pypi" and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", source["project"]
        ):
            raise ValueError(f"{name}: invalid PyPI project")
    return sources


def registry_versions(source, request=request_json):
    if source["kind"] == "npm":
        metadata = request(
            "https://registry.npmjs.org/" + quote(source["package"], safe="")
        )
        return {
            "latest": metadata.get("dist-tags", {}).get("latest"),
            "versions": [
                {
                    "version": version,
                    "distribution": data["dist"],
                    **(
                        {"deprecated": data["deprecated"]}
                        if "deprecated" in data
                        else {}
                    ),
                }
                for version, data in sorted(metadata["versions"].items())
            ],
        }
    metadata = request(
        "https://pypi.org/pypi/" + quote(source["project"], safe="") + "/json"
    )
    return {
        "latest": metadata["info"]["version"],
        "versions": [
            {
                "version": version,
                "files": [
                    {
                        "filename": f["filename"],
                        "url": f["url"],
                        "sha256": f["digests"]["sha256"],
                        "yanked": f.get("yanked", False),
                    }
                    for f in files
                ],
            }
            for version, files in sorted(metadata["releases"].items())
            if files
        ],
    }


def checkout(sources, destination):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Plugin checkout destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    inventory = {"discovery_version": 1, "plugins": {}}
    for name, source in sorted(sources.items()):
        if source["kind"] != "git":
            inventory["plugins"][name] = {
                "location": source,
                **registry_versions(source),
            }
            print(
                f"{name}: discovered package versions; package installation is not yet supported"
            )
            continue
        repo = destination / name
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                "main",
                "--",
                source["repository"],
                str(repo),
            ],
            check=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        manifest_file = repo / "plugin.json"
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise ValueError(f"{name}: expected a regular root plugin.json")
        manifest = json.loads(manifest_file.read_text())
        if manifest["plugin"]["id"] != name:
            raise ValueError(f"{name}: source manifest identifies a different plugin")
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        version = manifest["plugin"]["version"]
        inventory["plugins"][name] = {
            "location": source,
            "latest": version,
            "versions": [{"version": version, "commit": commit}],
        }
        print(f"{name}: discovered Git version {version} at {commit}")
    return inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        inventory = checkout(load_sources(args.sources), args.destination)
        args.report.write_text(json.dumps(inventory, indent=2) + "\n")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Version discovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
