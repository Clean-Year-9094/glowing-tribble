#!/usr/bin/env bash
set -euo pipefail

out="$RUNNER_TEMP/out"
cache="$RUNNER_TEMP/modcache"
mkdir -p "$out" "$cache/modules"
manifest="$cache/built.tsv"
touch "$manifest"
build_script_digest=$(sha256sum "$0")
build_script_digest=${build_script_digest%% *}

record_module() {
  local identity=$1 filename=$2 digest
  digest=$(sha256sum "$out/$filename")
  printf '%s\t%s\t%s\t%s\n' "$identity" "$filename" "${digest%% *}" "$build_script_digest" >> "$out/$SERIES.tsv"
}

if [[ ${1:-} == restore ]]; then
  while IFS=$'\t' read -r identity url filename asset checksum; do
    [[ "$asset" != - ]] || continue
    echo "Reusing released $filename"
    curl --fail --silent --show-error --location --retry 3 \
      --header "Authorization: Bearer $GH_TOKEN" --header 'Accept: application/octet-stream' \
      --output "$out/$filename" "$GITHUB_API_URL/repos/$GITHUB_REPOSITORY/releases/assets/$asset"
    printf '%s  %s\n' "$checksum" "$out/$filename" | sha256sum --check
    record_module "$identity" "$filename"
  done <<< "$PACKAGES"
  exit 0
fi

toolchain_ready=false
while IFS=$'\t' read -r identity url released_filename asset checksum; do
  if [[ "$asset" != - ]]; then
    test -s "$out/$released_filename"
    continue
  fi

  cached_version=$(awk -F '\t' -v id="$identity" '$1 == id { print $2 }' "$manifest" | tail -n1)
  if [[ -n "$cached_version" && -f "$cache/modules/$identity.ko" ]]; then
    filename="cpuid_fault_emulation-$cached_version.ko"
    echo "Reusing cached $filename"
    cp "$cache/modules/$identity.ko" "$out/$filename"
    record_module "$identity" "$filename"
    continue
  fi

  if [[ "$toolchain_ready" == false ]]; then
    sed -i 's/^SigLevel.*/SigLevel = Never/' /etc/pacman.conf
    repos=$(curl -fsSL "$MIRROR/" | sed -nE 's/.*href="((jupiter|holo)-[A-Za-z0-9._-]+)\/".*/\1/p' | sort -u)
    for repo in $repos; do
      if curl -fsSI -o /dev/null "$MIRROR/$repo/os/x86_64/$repo.db"; then
        printf '\n[%s]\nServer = %s/$repo/os/$arch\nSigLevel = Never\n' "$repo" "$MIRROR" >> /etc/pacman.conf
      fi
    done
    pacman -Syu --noconfirm base-devel clang make lld llvm git
    toolchain_ready=true
  fi

  if ! pacman -U --noconfirm --overwrite '*' "$url"; then
    echo "Headers installation failed: $url" >&2
    continue
  fi
  build_dir=$(pacman -Ql "$SERIES" | awk '$2 ~ /^\/usr\/lib\/modules\/[^/]+\/build\/?$/ { print $2 }')
  if [[ "$build_dir" =~ ^/usr/lib/modules/([A-Za-z0-9._+-]+)/build/?$ ]]; then
    kernel=${BASH_REMATCH[1]}
    filename="cpuid_fault_emulation-$kernel.ko"
    echo "Building $filename"
    KERNEL="$kernel" make clean || true
    if KERNEL="$kernel" make; then
      cp cpuid_fault_emulation.ko "$out/$filename"
      cp cpuid_fault_emulation.ko "$cache/modules/$identity.ko"
      printf '%s\t%s\n' "$identity" "$kernel" >> "$manifest"
      record_module "$identity" "$filename"
    else
      echo "Module compilation failed: $kernel" >&2
    fi
  else
    echo "Could not identify the kernel build directory for $url" >&2
  fi
  pacman -Rdd --noconfirm "$SERIES"
done <<< "$PACKAGES"
