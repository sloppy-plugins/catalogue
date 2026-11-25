"""Build catalogue releases from clean sibling plugins already pushed to GitHub."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from sloppy_sdk.catalogue import add_release, read, repository_url, write

ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError(
            f"{repo.name}: git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def github_url(url):
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    parsed = urlsplit(url)
    if (
        parsed.scheme == "ssh"
        and parsed.hostname == "github.com"
        and parsed.username == "git"
    ):
        url = "https://github.com/" + parsed.path.lstrip("/")
        parsed = urlsplit(url)
    if parsed.hostname != "github.com" or len(parsed.path.strip("/").split("/")) != 2:
        raise ValueError(
            "Plugin origins must identify an owner/repository on github.com"
        )
    return repository_url(url)


def synchronize(plugins_dir, catalogue_file, check_only=False, sources=None):
    original = catalogue_file.read_bytes()
    catalogue = read(catalogue_file)
    discovered = sorted(
        p for p in plugins_dir.iterdir() if p.is_dir() and (p / "plugin.json").is_file()
    )
    if sources is not None:
        expected = {name for name, source in sources.items() if source["kind"] == "git"}
        discovered = [plugins_dir / name for name in sorted(expected)]
        for repo in discovered:
            if not (repo / "plugin.json").is_file():
                raise ValueError(
                    f"{repo.name}: configured Git plugin checkout is missing"
                )
    if not discovered and sources is None:
        raise ValueError(
            f"No plugin repositories with plugin.json found in {plugins_dir}"
        )
    seen, added = set(), []
    for repo in discovered:
        if repo.is_symlink() or not (repo / ".git").exists():
            raise ValueError(f"{repo}: expected a separate Git checkout")
        if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
            raise ValueError(f"{repo}: not a repository root")
        if git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError(
                f"{repo.name}: commit and push the plugin changes before synchronizing"
            )
        commit = git(repo, "rev-parse", "--verify", "HEAD")
        repository = github_url(git(repo, "config", "--get", "remote.origin.url"))
        if sources is not None and repository != github_url(
            sources[repo.name]["repository"]
        ):
            raise ValueError(
                f"{repo.name}: checkout identity differs from its configured location"
            )
        git(
            repo,
            "fetch",
            "--quiet",
            "--tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        if commit != git(repo, "rev-parse", "refs/remotes/origin/main"):
            raise ValueError(
                f"{repo.name}: HEAD is not the pushed GitHub main; push or synchronize it first"
            )
        candidates = []
        remote_tags = {}
        for line in git(repo, "ls-remote", "--tags", "origin").splitlines():
            sha, ref = line.split()
            if ref.endswith("^{}"):
                continue
            tag = ref.removeprefix("refs/tags/")
            if re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag):
                remote_tags[tag] = sha
        for tag, sha in sorted(
            remote_tags.items(),
            key=lambda item: tuple(map(int, item[0].removeprefix("v").split("."))),
        ):
            if git(repo, "rev-parse", f"refs/tags/{tag}") != sha:
                raise ValueError(
                    f"{repo.name}: remote tag {tag} changed during synchronization"
                )
            tag_commit = git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
            candidates.append((tag_commit, tag.removeprefix("v")))
        candidates.append((commit, None))
        plugin_seen = None
        for commit, tag_version in candidates:
            manifest = json.loads(git(repo, "show", f"{commit}:plugin.json"))
            descriptor = manifest["plugin"]
            plugin_id, version = descriptor["id"], descriptor["version"]
            if sources is not None and (
                plugin_id != repo.name
                or repository != github_url(sources[repo.name]["repository"])
            ):
                raise ValueError(
                    f"{repo.name}: checkout identity differs from its configured location"
                )
            if tag_version is not None and version != tag_version:
                raise ValueError(
                    f"{repo.name}: tag {tag_version} disagrees with its manifest version"
                )
            if plugin_seen is None:
                if plugin_id in seen:
                    raise ValueError(
                        f"Duplicate plugin ID {plugin_id} in sibling repositories"
                    )
                seen.add(plugin_id)
                plugin_seen = plugin_id
            elif plugin_id != plugin_seen:
                raise ValueError(
                    f"{repo.name}: plugin ID changed across published versions"
                )
            existing = next(
                (p for p in catalogue["plugins"] if p["id"] == plugin_id), None
            )
            release = (
                next((r for r in existing["versions"] if r["version"] == version), None)
                if existing
                else None
            )
            if release:
                if (
                    release["manifest"] != manifest
                    or repository_url(existing["repository"]) != repository
                ):
                    raise ValueError(
                        f"{plugin_id}@{version} is already released; bump its version before publishing another commit"
                    )
                print(
                    f"{plugin_id}@{version}: already released at {release['commit']}; retaining its immutable commit"
                )
                continue
            catalogue = add_release(
                catalogue, manifest, repository, commit, new_plugin=existing is None
            )
            added.append(f"{plugin_id}@{version}")
            print(f"{plugin_id}@{version}: verified GitHub commit {commit}")
    if catalogue_file.read_bytes() != original:
        raise ValueError(
            "The catalogue changed during synchronization; rerun without concurrent edits"
        )
    if sources is not None:
        from discover import request_json
        from packages import merge

        previous = catalogue
        catalogue = merge(catalogue, sources, request_json)
        if catalogue != previous:
            old_keys = {
                (p["id"], r["version"])
                for p in previous["plugins"]
                for r in p["versions"]
            }
            added.extend(
                f"{p['id']}@{r['version']}"
                for p in catalogue["plugins"]
                for r in p["versions"]
                if (p["id"], r["version"]) not in old_keys
            )
    if catalogue_file.read_bytes() != original:
        raise ValueError("The catalogue changed during synchronization")
    if check_only:
        print(
            f"Validation passed: {len(added)} releases would be added; no files changed."
        )
    elif added:
        write(catalogue_file, catalogue)
        print(
            f"Added {len(added)} releases. Review and commit catalogue.json yourself; no commit or push was performed."
        )
    else:
        print("Catalogue is already synchronized; no files changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins-dir", type=Path, default=ROOT.parent)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        from discover import load_sources

        synchronize(
            args.plugins_dir.resolve(),
            ROOT / "catalogue.json",
            args.check,
            load_sources(ROOT / "sources.json"),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Catalogue synchronization failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
