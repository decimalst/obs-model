#!/usr/bin/env python3
"""Build the public studio from an explicit allowlist, never private catalog/environment files."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "index.html",
    "style.css",
    "app.mjs",
    "model.mjs",
    "policies.mjs",
    "operations.mjs",
    "iam.mjs",
)


def build(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # Refuse stale directories to prevent accidental publication of unrelated files.
    if any(destination.iterdir()):
        raise ValueError("Build destination must be empty")
    for name in FILES:
        shutil.copyfile(ROOT / "site" / name, destination / name)
    (destination / ".nojekyll").write_text("")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
