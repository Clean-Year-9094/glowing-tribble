#!/usr/bin/env bash
set -euo pipefail

# Repeat validation at the compilation boundary.
[[ "$KERNEL_VERSION" =~ ^[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}-[0-9]{1,4}(\.[0-9]{1,4})?(-[A-Za-z0-9]{1,24}){1,6}$ ]]
[[ "$HEADERS_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.-]+/[A-Za-z0-9_.+-]+\.pkg\.tar\.zst$ ]]
[[ "$HEADERS_SHA256" =~ ^[a-f0-9]{64}$ ]]

pacman -Syu --noconfirm --needed base-devel clang llvm lld libelf openssl pahole kmod curl libarchive python
curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 \
    --connect-timeout 30 --max-time 600 --max-filesize 1073741824 \
    "$HEADERS_URL" --output /tmp/headers.pkg.tar.zst
printf '%s  %s\n' "$HEADERS_SHA256" /tmp/headers.pkg.tar.zst | sha256sum --check -

# Extract only the matching build tree; never install package hooks or other paths.
bsdtar -tf /tmp/headers.pkg.tar.zst > /tmp/headers-members
python - <<'PY'
import os
from pathlib import PurePosixPath, Path
kernel = os.environ['KERNEL_VERSION']
prefix = f'usr/lib/modules/{kernel}/build/'
members = Path('/tmp/headers-members').read_text().splitlines()
selected = []
for name in members:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise SystemExit('Unsafe archive member path')
    if name.startswith(prefix):
        selected.append(name)
if not selected:
    raise SystemExit('Headers package does not contain the requested kernel build directory')
Path('/tmp/selected-members').write_text('\n'.join(selected) + '\n')
PY
bsdtar -xpf /tmp/headers.pkg.tar.zst -C / --no-same-owner -T /tmp/selected-members
build="/usr/lib/modules/$KERNEL_VERSION/build"
test -s "$build/.config"
test -s "$build/Module.symvers"
test "$(cat "$build/include/config/kernel.release")" = "$KERNEL_VERSION"
# Kernel package content runs only inside this disposable, token-free container.
make KERNEL="$KERNEL_VERSION" clean
make -j"$(nproc)" KERNEL="$KERNEL_VERSION"
vermagic=$(modinfo -F vermagic cpuid_fault_emulation.ko)
test "${vermagic%% *}" = "$KERNEL_VERSION"
test "$(modinfo -F name cpuid_fault_emulation.ko)" = cpuid_fault_emulation
mkdir -p out
module="cpuid_fault_emulation-$KERNEL_VERSION.ko"
install -m644 cpuid_fault_emulation.ko "out/$module"
printf '%s\n' "$vermagic" > out/vermagic.txt
(cd out && sha256sum "$module" > SHA256SUMS)
{ gcc --version; clang --version; } > out/toolchain.txt
cat > out/BUILD.md <<EOF
Module compiled for **$KERNEL_VERSION** (x86_64).

Headers package: $HEADERS_URL
Headers SHA-256: $HEADERS_SHA256
Module vermagic: $vermagic

Compilation and vermagic were checked. Loading and runtime behavior were not tested.
The module is unsigned and requires AMD SVM.
EOF
