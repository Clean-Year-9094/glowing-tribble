#!/usr/bin/env python3
"""Plan missing SteamOS modules and stage changed release assets."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

INVENTORY = "steamos-build-inputs-v1.tsv"
BUILD_PATHS = ("Makefile", "inc", "src")
BUILD_SCRIPT = Path(".github/scripts/steamos-build.sh")
RELEASE_TAG = re.compile(r"v\d{4}\.\d{2}\.\d{2}-([0-9a-f]{7})(?:-\d+-\d+)?")
MODULE_NAME = re.compile(r"cpuid_fault_emulation-[A-Za-z0-9._+-]+\.ko")
SHA256 = re.compile(r"[0-9a-f]{64}")
HEADER_ROW = re.compile(
    r'href="((linux-neptune-[A-Za-z0-9_-]+-headers)-(\d+\.\d+)[A-Za-z0-9._+-]*\.pkg\.tar\.zst)"'
    r'[^<]*</a></td><td class="size">([^<]+)</td><td class="date">([^<]+)</td>'
)


def command(*args):
    return subprocess.check_output(args, text=True)


def get(url, method="GET"):
    with urlopen(Request(url, method=method), timeout=30) as response:
        return response.read().decode()


def discover(mirror, minimum, keep):
    repos = sorted(set(re.findall(r'href="((?:jupiter|holo)-[A-Za-z0-9._-]+)/"', get(mirror + "/"))))

    def read_repo(repo):
        base = f"{mirror}/{repo}/os/x86_64"
        try:
            get(f"{base}/{repo}.db", method="HEAD")
        except HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        return [(base, row) for row in HEADER_ROW.findall(get(base + "/"))]

    series = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(read_repo, repos):
            for base, (filename, name, version, size, modified) in rows:
                if tuple(map(int, version.split("."))) >= minimum:
                    url = f"{base}/{filename}"
                    identity = hashlib.sha256(f"{url}\t{size}\t{modified}".encode()).hexdigest()
                    series.setdefault(name, {}).setdefault(filename, (identity, url))

    if not series:
        raise SystemExit("No supported SteamOS headers packages found.")
    return {
        name: [packages[filename] for filename in version_sort(packages)[-keep:]]
        for name, packages in sorted(series.items())
    }


def version_sort(packages):
    return subprocess.check_output(["sort", "-V"], input="\n".join(packages), text=True).splitlines()


def github_pages(endpoint):
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", endpoint))
    return [item for page in pages for item in page]


def steamos_releases(repository):
    releases = github_pages(f"repos/{repository}/releases?per_page=100")
    return sorted(
        (release for release in releases
         if not release["draft"] and not release["prerelease"]
         and release["published_at"] and RELEASE_TAG.fullmatch(release["tag_name"])),
        key=lambda release: (release["published_at"], release["id"]),
        reverse=True,
    )


def release_assets(repository, release):
    endpoint = f"repos/{repository}/releases/{release['id']}/assets?per_page=100"
    return {asset["name"]: asset for asset in github_pages(endpoint) if asset["state"] == "uploaded"}


def has_current_source(tag):
    match = RELEASE_TAG.fullmatch(tag)
    rev = subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", f"refs/tags/{tag}^{{commit}}"],
        capture_output=True, text=True,
    )
    if rev.returncode or not rev.stdout.strip().startswith(match.group(1)):
        return False
    comparison = subprocess.run(
        ["git", "diff", "--quiet", rev.stdout.strip(), "HEAD", "--", *BUILD_PATHS]
    )
    if comparison.returncode == 1:
        return False
    comparison.check_returncode()
    return True


def released_inputs(repository, requested):
    remaining = set(requested)
    latest_assets = {}
    script_digest = hashlib.sha256(BUILD_SCRIPT.read_bytes()).hexdigest()
    for release in steamos_releases(repository):
        tag = release["tag_name"]
        assets = release_assets(repository, release)
        for name, asset in assets.items():
            if MODULE_NAME.fullmatch(name):
                latest_assets.setdefault(name, asset)
        if INVENTORY not in assets or not has_current_source(tag):
            continue

        inventory = command("gh", "release", "download", tag, "--repo", repository,
                            "--pattern", INVENTORY, "--output", "-")
        for line in inventory.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                continue
            identity, filename, checksum, recorded_script = fields
            if (identity not in remaining or recorded_script != script_digest
                    or not MODULE_NAME.fullmatch(filename) or not SHA256.fullmatch(checksum)):
                continue
            expected_digest = f"sha256:{checksum}"
            if (assets.get(filename, {}).get("digest") == expected_digest
                    and latest_assets[filename].get("digest") == expected_digest):
                remaining.remove(identity)
        if not remaining:
            break
    return requested - remaining


def plan(series, published):
    builds = []
    for name, packages in series.items():
        missing = [f"{identity}\t{url}\t-\t-\t-" for identity, url in packages
                   if identity not in published]
        if missing:
            builds.append({"series": name, "packages": "\n".join(missing)})
    return builds


def local_inventory(source):
    records = {}
    for manifest in sorted(source.glob("*.tsv")):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            identity, filename, checksum, script_digest = line.split("\t")
            if (not MODULE_NAME.fullmatch(filename)
                    or not all(SHA256.fullmatch(value) for value in (identity, checksum, script_digest))):
                raise ValueError(f"Invalid module inventory row in {manifest}")
            records.setdefault(filename, set()).add(line)

    modules = {module.name: module for module in source.glob("*.ko")}
    if modules.keys() != records.keys():
        raise ValueError("The module files and build inventory do not match.")

    candidates = {}
    for filename, module in modules.items():
        checksum = hashlib.sha256(module.read_bytes()).hexdigest()
        if any(line.split("\t")[2] != checksum for line in records[filename]):
            raise ValueError(f"The build inventory checksum does not match {filename}")
        candidates[filename] = (module, f"sha256:{checksum}", records[filename])
    return candidates


def stage_release(repository, source, destination):
    candidates = local_inventory(source)
    latest_assets = {}
    if candidates:
        for release in steamos_releases(repository):
            assets = release_assets(repository, release)
            for filename in candidates.keys() & assets.keys():
                latest_assets.setdefault(filename, assets[filename].get("digest"))
            if latest_assets.keys() == candidates.keys():
                break

    rows = []
    for filename, (module, digest, records) in sorted(candidates.items()):
        if latest_assets.get(filename) == digest:
            print(f"Already published: {filename}")
            continue
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(module, destination / filename)
        rows.extend(records)
    if rows:
        (destination / INVENTORY).write_text("\n".join(sorted(rows)) + "\n", encoding="utf-8")
    return bool(rows)


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    output_path = Path(os.environ["GITHUB_OUTPUT"])
    if sys.argv[1:] == ["publish"]:
        publish = stage_release(repository, Path("out"), Path("publish"))
        with output_path.open("a", encoding="utf-8") as output:
            print(f"publish={str(publish).lower()}", file=output)
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: steamos-plan.py [publish]")

    minimum = tuple(map(int, os.environ["MIN_KVER"].split(".")))
    series = discover(os.environ["MIRROR"], minimum, int(os.environ["KEEP"]))
    requested = {identity for packages in series.values() for identity, _ in packages}
    published = set()
    if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
        published = released_inputs(repository, requested)
    builds = plan(series, published)
    print(f"Selected {len(requested)} headers packages; {len(published)} already published.")
    with output_path.open("a", encoding="utf-8") as output:
        print(f"build={str(bool(builds)).lower()}", file=output)
        print(f"matrix={json.dumps({'include': builds}, separators=(',', ':'))}", file=output)


if __name__ == "__main__":
    main()
