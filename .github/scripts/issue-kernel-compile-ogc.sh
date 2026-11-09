#!/usr/bin/env bash
set -euo pipefail

: "${KERNEL_VERSION:?KERNEL_VERSION must be set}"
PACKAGE="/work/kernel-rpms/kernel-devel-$KERNEL_VERSION.rpm"
ACTUAL=$(rpm -qp --queryformat '%{VERSION}-%{RELEASE}.%{ARCH}' "$PACKAGE")
if [[ "$ACTUAL" != "$KERNEL_VERSION" ]]; then
    echo "Kernel development package mismatch: $ACTUAL != $KERNEL_VERSION" >&2
    exit 1
fi

# OGC RPMs come from the official OCI artifact and are digest-checked by the caller.
dnf -y install gcc gcc-c++ clang llvm lld make elfutils-libelf-devel \
    openssl-devel flex bison bc dwarves kmod python3 perl diffutils findutils
# Only the locally downloaded OGC RPM bypasses RPM signature checks.
dnf -y --nogpgcheck install "$PACKAGE"
BUILD_DIR="/usr/src/kernels/$KERNEL_VERSION"
test -s "$BUILD_DIR/Module.symvers"
test -s "$BUILD_DIR/.config"
test "$(cat "$BUILD_DIR/include/config/kernel.release")" = "$KERNEL_VERSION"
mkdir -p "/lib/modules/$KERNEL_VERSION"
ln -sfn "$BUILD_DIR" "/lib/modules/$KERNEL_VERSION/build"

make KERNEL="$KERNEL_VERSION" clean
make -j"$(nproc)" KERNEL="$KERNEL_VERSION"
VERMAGIC=$(modinfo -F vermagic cpuid_fault_emulation.ko)
if [[ "${VERMAGIC%% *}" != "$KERNEL_VERSION" ]]; then
    echo "Built module kernel mismatch: $VERMAGIC" >&2
    exit 1
fi
test "$(modinfo -F name cpuid_fault_emulation.ko)" = cpuid_fault_emulation
install -Dm644 cpuid_fault_emulation.ko "out/cpuid_fault_emulation-$KERNEL_VERSION.ko"
printf '%s\n' "$VERMAGIC" > out/vermagic.txt
{ gcc --version; clang --version; } > out/toolchain.txt
