import json
import os
import re
from pathlib import Path

OGC = r"[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}-ogc[0-9]{1,3}\.[0-9]{1,3}\.fc[0-9]{2}\.x86_64"
ARCH = r"[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}-[0-9]{1,4}(?:\.[0-9]{1,4})?(?:-[A-Za-z0-9]{1,24}){1,6}"
ASSET = r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.-]+/[A-Za-z0-9_.+-]+\.pkg\.tar\.zst"


def field(body, heading):
    # Reject ambiguous/duplicate fields instead of silently choosing one.
    parts = re.findall(r"^### " + re.escape(heading) + r"\r?\n\r?\n(.*?)(?=^### |\Z)", body, re.M | re.S)
    if len(parts) != 1:
        raise ValueError(f"Expected exactly one {heading} field; use the Kernel module build form.")
    value = parts[0].strip()
    return "" if value == "_No response_" else value


def validate(body):
    kernel = field(body, "Kernel release")
    if len(kernel) > 128 or not re.fullmatch(r"(?:" + OGC + "|" + ARCH + ")", kernel, re.ASCII):
        raise ValueError("Invalid kernel release. Paste the complete uname -r output without spaces, commands, paths, or code fences.")
    url = field(body, "Headers package URL")
    checksum = field(body, "Headers SHA256")
    if re.fullmatch(OGC, kernel, re.ASCII):
        if url or checksum:
            raise ValueError("OGC requests use the official package source; leave both headers fields blank.")
        mode = "ogc"
    else:
        if not re.fullmatch(ASSET, url, re.ASCII):
            raise ValueError("This Arch/custom kernel requires an exact headers .pkg.tar.zst from a public GitHub Release. Set Headers package URL.")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", checksum, re.ASCII):
            raise ValueError("Headers SHA256 must contain exactly 64 hexadecimal characters.")
        mode = "arch"
    return {"kernel": kernel, "mode": mode, "headers_url": url, "headers_sha256": checksum.lower()}


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    try:
        if not event["issue"].get("title", "").startswith("[Kernel build]"):
            raise ValueError("Invalid issue title. Use the Kernel module build issue template and keep the [Kernel build] prefix.")
        result = validate(event["issue"].get("body") or "")
    except ValueError as error:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"error={error}\n")
        print(str(error), flush=True)
        raise SystemExit(1)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
