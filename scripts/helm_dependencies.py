#!/usr/bin/env python3
"""Register selected HTTPS repositories and rebuild only locked Helm dependencies.

This prepares local chart archives; it never contacts AWS/Kubernetes or installs
releases. Existing repository aliases are preserved. Helm verifies the lock
digest and downloads the locked versions, without dependency resolution/update.
"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml

try:
    from .bootstrap_crds import UniqueLoader
except ImportError:
    from bootstrap_crds import UniqueLoader


def dependencies(document, source):
    entries = document.get("dependencies") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{source}: a nonempty dependency list is required")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: invalid dependency")
        name, version, repository = (entry.get(k) for k in ("name", "version", "repository"))
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError(f"{source}: invalid dependency name")
        if not isinstance(version, str) or not re.fullmatch(
            r"v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?", version
        ):
            raise ValueError(f"{source}: dependency versions must be exact pins")
        url = urlsplit(repository) if isinstance(repository, str) else None
        if (
            url is None
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() for c in repository)
        ):
            raise ValueError(f"{source}: dependency repositories must be credential-free HTTPS")
        result.append((name, version, repository))
    if len({item[0] for item in result}) != len(result):
        raise ValueError(f"{source}: duplicate dependency names are unsupported")
    return result


def selected_charts(paths):
    charts, repositories = {}, set()
    for directory in dict.fromkeys(Path(path).resolve() for path in paths):
        originals = {}
        for filename in ("Chart.yaml", "Chart.lock"):
            path = directory / filename
            if not path.is_file():
                raise ValueError(f"Missing required chart file: {path}")
            originals[filename] = path.read_bytes()
        chart = yaml.load(originals["Chart.yaml"], Loader=UniqueLoader)
        lock = yaml.load(originals["Chart.lock"], Loader=UniqueLoader)
        wanted = dependencies(chart, directory / "Chart.yaml")
        locked = dependencies(lock, directory / "Chart.lock")
        if wanted != locked:
            raise ValueError(f"{directory}: Chart.yaml dependencies differ from Chart.lock")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(lock.get("digest", ""))):
            raise ValueError(f"{directory}: Chart.lock must contain a SHA256 lock digest")
        repositories.update(item[2] for item in locked)
        charts[directory] = originals
    if not charts:
        raise ValueError("Select at least one chart")
    return charts, sorted(repositories)


def repository_alias(url):
    return "olly-" + hashlib.sha256(url.encode()).hexdigest()[:16]


def build_locked(paths, runner=subprocess.run):
    # Validate every selected chart before changing repository configuration.
    charts, repositories = selected_charts(paths)
    listing = runner(["helm", "repo", "list", "--output", "json"], capture_output=True, text=True)
    if listing.returncode:
        if "no repositories to show" not in listing.stderr.lower():
            raise ValueError("Unable to read Helm repository configuration")
        existing = []
    else:
        existing = json.loads(listing.stdout)
    if not isinstance(existing, list) or any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("name"), str)
        or not isinstance(entry.get("url"), str)
        for entry in existing
    ):
        raise ValueError("Invalid Helm repository listing")
    aliases = {entry["name"]: entry["url"] for entry in existing}
    commands = []
    for repository in repositories:
        same = next((name for name, url in aliases.items() if url == repository), None)
        if same is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", same):
                raise ValueError("Selected Helm repository has an unsupported alias")
            # Refresh only a selected index, never unrelated configured repositories.
            commands.append(["helm", "repo", "update", same])
        else:
            alias = repository_alias(repository)
            if alias in aliases:
                raise ValueError(f"Helm repository alias collision: {alias}; nothing overwritten")
            aliases[alias] = repository
            commands.append(["helm", "repo", "add", alias, repository])
    for command in commands:
        runner(command, check=True)
    for directory, originals in charts.items():
        runner(["helm", "dependency", "build", str(directory), "--skip-refresh"], check=True)
        for filename, original in originals.items():
            if (directory / filename).read_bytes() != original:
                raise ValueError(f"{directory / filename}: unexpected mutation during locked build")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "charts", nargs="+", type=Path, help="Chart directories with committed locks"
    )
    args = parser.parse_args()
    try:
        build_locked(args.charts)
    except (ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Helm dependency preparation failed: {error}\n")


if __name__ == "__main__":
    main()
