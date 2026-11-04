#!/usr/bin/env bash
set -euo pipefail

# Accept only an exact x86_64 OGC Fedora kernel release, never shell syntax or paths.
if [[ ! "${KERNEL_VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+-ogc[0-9]+\.[0-9]+\.fc([0-9]+)\.x86_64$ ]]; then
    echo "Expected a kernel release such as 7.2.9-ogc3.1.fc44.x86_64" >&2
    exit 1
fi
FEDORA_VERSION="${BASH_REMATCH[1]}"
PACKAGE_VERSION="${KERNEL_VERSION%.x86_64}"
OGC_VERSION="${PACKAGE_VERSION%.fc*}"
REPOSITORY=ghcr.io/opengamingcollective/kernel-packages-fedora
REFERENCE="$REPOSITORY:$OGC_VERSION-fc$FEDORA_VERSION"
PACKAGE="kernel-devel-$KERNEL_VERSION.rpm"

mkdir -p out kernel-rpms
# Resolve the tag once; both manifest and RPM are then addressed by immutable digest.
MANIFEST_DIGEST=$(oras manifest fetch --descriptor "$REFERENCE" | jq -er '.digest')
oras manifest fetch "$REPOSITORY@$MANIFEST_DIGEST" > kernel-rpms/manifest.json
PACKAGE_DIGEST=$(jq -er --arg name "$PACKAGE" '
    [.layers[] | select(.annotations["org.opencontainers.image.title"] == $name)]
    | if length == 1 then .[0].digest else error("Expected one matching kernel-devel RPM") end
' kernel-rpms/manifest.json)
oras blob fetch --output "kernel-rpms/$PACKAGE" "$REPOSITORY@$PACKAGE_DIGEST"
printf '%s  %s\n' "${PACKAGE_DIGEST#sha256:}" "kernel-rpms/$PACKAGE" | sha256sum --check -

# Build in the matching Fedora userspace; do not install or load anything on the host.
docker run --rm \
    --env KERNEL_VERSION \
    --volume "$PWD:/work" --workdir /work \
    "fedora:$FEDORA_VERSION" \
    bash .github/scripts/issue-kernel-compile-ogc.sh

MODULE="cpuid_fault_emulation-$KERNEL_VERSION.ko"
test -s "out/$MODULE"
VERMAGIC=$(cat out/vermagic.txt)
SOURCE_COMMIT=$(git rev-parse HEAD)
cat > out/BUILD.md <<EOF
Module built for Bazzite OGC kernel **$KERNEL_VERSION** (x86_64).

- Source commit: $SOURCE_COMMIT
- Kernel package source: $REFERENCE
- Kernel package manifest: $MANIFEST_DIGEST
- Kernel development RPM: $PACKAGE
- Kernel development RPM SHA-256: ${PACKAGE_DIGEST#sha256:}
- Module vermagic: $VERMAGIC

Download $MODULE and SHA256SUMS, then verify with sha256sum --check SHA256SUMS.
The module was compiled and its kernel release checked; loading and runtime behavior
have not been verified. The module is unsigned; Secure Boot may require local signing.
This module requires AMD SVM. A VM must expose SVM/nested virtualization to use it.
EOF
(cd out && sha256sum "$MODULE" > SHA256SUMS)
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'kernel_version=%s\n' "$KERNEL_VERSION" >> "$GITHUB_OUTPUT"
fi
