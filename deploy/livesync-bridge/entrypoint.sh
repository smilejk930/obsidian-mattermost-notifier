#!/bin/sh
set -eu

baked_cache="${LSB_BAKED_DENO_DIR:-/opt/livesync-bridge-deno-cache}"
runtime_cache="${DENO_DIR:-/var/lib/livesync-bridge/deno}"
baked_version="/usr/local/share/livesync-bridge-cache-version"
runtime_version="${runtime_cache}/.livesync-bridge-cache-version"

if [ ! -r "${baked_version}" ] || [ ! -d "${baked_cache}" ]; then
    echo "LiveSync Bridge 내장 Deno 캐시를 찾을 수 없습니다." >&2
    exit 1
fi

mkdir -p "${runtime_cache}"
if ! cmp -s "${baked_version}" "${runtime_version}"; then
    echo "LiveSync Bridge Deno 캐시를 초기화합니다: ${runtime_cache}"
    cp -a "${baked_cache}/." "${runtime_cache}/"
    cp "${baked_version}" "${runtime_version}"
fi

exec "$@"
