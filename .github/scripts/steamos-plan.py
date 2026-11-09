#!/usr/bin/env python3
"""Plan SteamOS module builds from the mirror and the latest published inventory."""

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

INVENTORY = "steamos-build-inputs-v1.tsv"
BUILD_PATHS = ("Makefile", "inc", "src")
BUILD_SCRIPT = Path(".github/scripts/steamos-build.sh")
RELEASE_TAG = re.compile(r"v\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7}")
MODULE_NAME = re.compile(r"cpuid_fault_emulation-[A-Za-z0-9._+-]+\.ko")
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
    return [item for page in json.loads(command("gh", "api", "--paginate", "--slurp", endpoint))
            for item in page]


def released_modules(repository):
    releases = github_pages(f"repos/{repository}/releases?per_page=100")
    release = max(
        (r for r in releases if not r["draft"] and not r["prerelease"]
         and RELEASE_TAG.fullmatch(r["tag_name"])),
        key=lambda r: r["published_at"],
        default=None,
    )
    if release is None:
        return "", "", {}, set()

    tag = release["tag_name"]
    commit = command("git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").strip()
    if not commit.startswith(tag.rsplit("-", 1)[1]):
        return "", "", {}, set()
    assets = {asset["name"]: asset for asset in
              github_pages(f"repos/{repository}/releases/{release['id']}/assets?per_page=100")}
    comparison = subprocess.run(["git", "diff", "--quiet", commit, "HEAD", "--", *BUILD_PATHS])
    if comparison.returncode == 1:
        return "", "", {}, set()
    comparison.check_returncode()

    if INVENTORY not in assets:
        return tag, str(release["id"]), {}, set()

    inventory = command("gh", "release", "download", tag, "--repo", repository,
                        "--pattern", INVENTORY, "--output", "-")
    build_script_digest = hashlib.sha256(BUILD_SCRIPT.read_bytes()).hexdigest()
    modules = {}
    for line in inventory.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[3] != build_script_digest:
            continue
        identity, filename, checksum, _ = fields
        asset = assets.get(filename)
        if (re.fullmatch(r"[0-9a-f]{64}", identity) and MODULE_NAME.fullmatch(filename)
                and re.fullmatch(r"[0-9a-f]{64}", checksum)
                and asset and asset["state"] == "uploaded"
                and asset.get("digest") == f"sha256:{checksum}"):
            modules[identity] = (filename, str(asset["id"]), checksum)
    return tag, str(release["id"]), modules, {name for name in assets if name.endswith(".ko")}


def plan(series, modules, released_names):
    requested = {identity for packages in series.values() for identity, _ in packages}
    if requested == modules.keys() and released_names == {entry[0] for entry in modules.values()}:
        return []
    return [
        {"series": name, "packages": "\n".join(
            "\t".join((identity, url, *modules.get(identity, ("-", "-", "-"))))
            for identity, url in packages
        )}
        for name, packages in series.items()
    ]


def main():
    minimum = tuple(map(int, os.environ["MIN_KVER"].split(".")))
    series = discover(os.environ["MIRROR"], minimum, int(os.environ["KEEP"]))
    if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
        tag, release_id, modules, names = "", "", {}, set()
    else:
        tag, release_id, modules, names = released_modules(os.environ["GITHUB_REPOSITORY"])
    builds = plan(series, modules, names)
    print(f"Selected {sum(map(len, series.values()))} headers packages across {len(series)} series.")
    if not builds:
        print("The latest SteamOS release already covers the selected packages and source.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print(f"build={'true' if builds else 'false'}", file=output)
        print(f"matrix={json.dumps({'include': builds}, separators=(',', ':'))}", file=output)
        print(f"matching_tag={tag}", file=output)
        print(f"matching_release={release_id}", file=output)


if __name__ == "__main__":
    main()
