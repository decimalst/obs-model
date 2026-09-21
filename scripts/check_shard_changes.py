#!/usr/bin/env python3
"""Guard ClickHouse expansion and reject stateful topology reductions in PRs."""

import argparse
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = "charts/platform/values.yaml"


def count(values, default=1):
    return values.get("clickhouse", {}).get("shards", default)


def check(old_defaults, new_defaults, old_regions, new_regions):
    problems = []
    if count(new_defaults) < count(old_defaults):
        problems.append("Default ClickHouse shard count cannot decrease")
    if new_defaults.get("scaling", {}).get("logPartitions", 12) < old_defaults.get(
        "scaling", {}
    ).get("logPartitions", 12):
        problems.append("Default Kafka log partition count cannot decrease")
    for name, old in old_regions.items():
        if name not in new_regions:
            problems.append(
                f"Regional release removed: {name}; use the reviewed decommission procedure"
            )
        elif count(new_regions[name], count(new_defaults)) < count(old, count(old_defaults)):
            problems.append(
                f"ClickHouse shards cannot decrease in {name}; migrate data before decommissioning"
            )
    for name, old_values, new_values in [("defaults", old_defaults, new_defaults)] + [
        (
            name,
            {
                **old_defaults,
                "scaling": {
                    **old_defaults.get("scaling", {}),
                    **old_regions[name].get("scaling", {}),
                },
                "clickhouse": {
                    **old_defaults.get("clickhouse", {}),
                    **old_regions[name].get("clickhouse", {}),
                },
            },
            {
                **new_defaults,
                "scaling": {
                    **new_defaults.get("scaling", {}),
                    **new_regions[name].get("scaling", {}),
                },
                "clickhouse": {
                    **new_defaults.get("clickhouse", {}),
                    **new_regions[name].get("clickhouse", {}),
                },
            },
        )
        for name in old_regions.keys() & new_regions.keys()
    ]:
        old_partitions = old_defaults.get("scaling", {}).get("logPartitions", 12)
        new_partitions = new_defaults.get("scaling", {}).get("logPartitions", 12)
        if new_values.get("scaling", {}).get("logPartitions", new_partitions) < old_values.get(
            "scaling", {}
        ).get("logPartitions", old_partitions):
            problems.append(f"Kafka log partitions cannot decrease in {name}")
        # The pre-0.3 format requires the explicit legacy migration instead.
        if count(old_values) >= 2 and count(new_values) > count(old_values):
            if not all(
                v.get("clickhouse", {}).get("pauseWrites", False) for v in [old_values, new_values]
            ):
                problems.append(
                    f"Pause ClickHouse writers in a separate reconciled PR before expanding {name}"
                )
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args()
    # Pass refs as arguments, never executable shell text.
    subprocess.run(
        ["git", "rev-parse", "--verify", args.base_ref + "^{commit}"],
        check=True,
        capture_output=True,
    )

    def at(path):
        return yaml.safe_load(
            subprocess.run(
                ["git", "show", f"{args.base_ref}:{path}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )

    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", args.base_ref, "gitops/clusters"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    old = {
        name: at(name)["spec"].get("values", {})
        for name in names
        if name.endswith("/platform/release.yaml")
    }
    new = {
        str(path.relative_to(ROOT)): yaml.safe_load(path.read_text())["spec"].get("values", {})
        for path in (ROOT / "gitops/clusters").glob("*/platform/release.yaml")
    }
    problems = check(at(DEFAULTS), yaml.safe_load((ROOT / DEFAULTS).read_text()), old, new)
    if problems:
        raise SystemExit("\n".join(problems))
    print("No shard/partition reductions; nonlegacy shard increases require a prior write pause.")


if __name__ == "__main__":
    main()
