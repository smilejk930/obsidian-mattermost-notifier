#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"
LIVESYNC_BRIDGE_REF="${LIVESYNC_BRIDGE_REF:-454f7611e88f681b3430234e03424be28ed3c7be}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

for required_command in docker git python3 sha256sum; do
    if ! command -v "${required_command}" >/dev/null 2>&1; then
        echo "필수 명령을 찾을 수 없습니다: ${required_command}" >&2
        exit 1
    fi
done

if [[ "${TARGET_PLATFORM}" == *,* ]]; then
    echo "오프라인 tar 번들은 단일 TARGET_PLATFORM만 지원합니다: ${TARGET_PLATFORM}" >&2
    exit 1
fi

if [[ "${TARGET_PLATFORM}" != "linux/amd64" ]]; then
    echo "현재 hash lock은 linux/amd64만 지원합니다: ${TARGET_PLATFORM}" >&2
    echo "다른 아키텍처는 requirements.runtime.lock의 wheel hash를 먼저 갱신하세요." >&2
    exit 1
fi

docker info >/dev/null
docker buildx version >/dev/null

if [[ -n "$(git -C "${REPOSITORY_DIR}" status --porcelain)" && "${ALLOW_DIRTY}" != "1" ]]; then
    echo "작업 트리에 미커밋 변경이 있습니다. 커밋 후 다시 실행하세요." >&2
    echo "검증 목적의 임시 빌드만 ALLOW_DIRTY=1로 허용됩니다." >&2
    exit 1
fi

PROJECT_VERSION="$(cd "${REPOSITORY_DIR}" && python3 -c 'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
PROJECT_REVISION="$(git -C "${REPOSITORY_DIR}" rev-parse HEAD)"
PROJECT_SHORT_REVISION="$(git -C "${REPOSITORY_DIR}" rev-parse --short=12 HEAD)"
DIRTY_SUFFIX=""
if [[ -n "$(git -C "${REPOSITORY_DIR}" status --porcelain)" ]]; then
    DIRTY_SUFFIX="-dirty"
fi

NOTIFIER_IMAGE="obsidian-mattermost-notifier:${PROJECT_VERSION}-${PROJECT_SHORT_REVISION}${DIRTY_SUFFIX}"
TASK_TEMP_DIR="$(mktemp -d -t obsidian-notifier-build.XXXXXXXX)"
trap 'rm -rf -- "${TASK_TEMP_DIR}"' EXIT

BRIDGE_SOURCE_DIR="${TASK_TEMP_DIR}/livesync-bridge"
git init -q "${BRIDGE_SOURCE_DIR}"
git -C "${BRIDGE_SOURCE_DIR}" remote add origin https://github.com/vrtmrz/livesync-bridge.git
git -C "${BRIDGE_SOURCE_DIR}" fetch -q --depth 1 origin "${LIVESYNC_BRIDGE_REF}"
git -C "${BRIDGE_SOURCE_DIR}" checkout -q --detach FETCH_HEAD
BRIDGE_REVISION="$(git -C "${BRIDGE_SOURCE_DIR}" rev-parse HEAD)"
BRIDGE_SHORT_REVISION="$(git -C "${BRIDGE_SOURCE_DIR}" rev-parse --short=12 HEAD)"
BRIDGE_IMAGE="livesync-bridge:${BRIDGE_SHORT_REVISION}"

DEFAULT_BUNDLE_DIR="${REPOSITORY_DIR}/dist/offline-bundle-${PROJECT_VERSION}-${PROJECT_SHORT_REVISION}${DIRTY_SUFFIX}"
BUNDLE_DIR="${BUNDLE_DIR:-${DEFAULT_BUNDLE_DIR}}"
if [[ -e "${BUNDLE_DIR}" ]]; then
    echo "번들 출력 경로가 이미 존재합니다: ${BUNDLE_DIR}" >&2
    echo "다른 BUNDLE_DIR을 지정하거나 기존 번들을 보존한 뒤 다시 실행하세요." >&2
    exit 1
fi

echo "notifier 이미지 빌드: ${NOTIFIER_IMAGE} (${TARGET_PLATFORM})"
docker buildx build \
    --platform "${TARGET_PLATFORM}" \
    --pull \
    --load \
    --build-arg "SOURCE_REVISION=${PROJECT_REVISION}" \
    --build-arg "PROJECT_VERSION=${PROJECT_VERSION}" \
    --tag "${NOTIFIER_IMAGE}" \
    "${REPOSITORY_DIR}"

echo "LiveSync Bridge 이미지 빌드: ${BRIDGE_IMAGE} (${TARGET_PLATFORM})"
docker buildx build \
    --platform "${TARGET_PLATFORM}" \
    --pull \
    --load \
    --file "${REPOSITORY_DIR}/deploy/livesync-bridge/Dockerfile" \
    --label "org.opencontainers.image.revision=${BRIDGE_REVISION}" \
    --label "org.opencontainers.image.source=https://github.com/vrtmrz/livesync-bridge" \
    --tag "${BRIDGE_IMAGE}" \
    "${BRIDGE_SOURCE_DIR}"

mkdir -p "${BUNDLE_DIR}"
docker image save --output "${BUNDLE_DIR}/images.tar" \
    "${NOTIFIER_IMAGE}" "${BRIDGE_IMAGE}"

cp "${REPOSITORY_DIR}/compose.example.yaml" "${BUNDLE_DIR}/compose.example.yaml"
cp "${REPOSITORY_DIR}/config.example.yaml" "${BUNDLE_DIR}/notifier-config.example.yaml"
cp "${REPOSITORY_DIR}/deploy/livesync-bridge/config.example.json" \
    "${BUNDLE_DIR}/livesync-bridge-config.example.json"
cp "${REPOSITORY_DIR}/README.md" "${BUNDLE_DIR}/README.md"

{
    echo "NOTIFIER_IMAGE=${NOTIFIER_IMAGE}"
    echo "LIVESYNC_BRIDGE_IMAGE=${BRIDGE_IMAGE}"
} >"${BUNDLE_DIR}/image-versions.env"

{
    echo "target_platform=${TARGET_PLATFORM}"
    echo "notifier_revision=${PROJECT_REVISION}"
    echo "livesync_bridge_revision=${BRIDGE_REVISION}"
    echo "notifier_base_image=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    echo "livesync_bridge_base_image=denoland/deno:2.6.9@sha256:2221eafd8a7556693307ad9889a79d4974e1844a6964acbe466ab9faa15d37bf"
    echo "notifier_image=${NOTIFIER_IMAGE}"
    echo "livesync_bridge_image=${BRIDGE_IMAGE}"
} >"${BUNDLE_DIR}/BUILD-METADATA.txt"

docker image inspect "${NOTIFIER_IMAGE}" "${BRIDGE_IMAGE}" \
    >"${BUNDLE_DIR}/image-inspect.json"

(
    cd "${BUNDLE_DIR}"
    sha256sum \
        images.tar \
        compose.example.yaml \
        notifier-config.example.yaml \
        livesync-bridge-config.example.json \
        image-versions.env \
        BUILD-METADATA.txt \
        image-inspect.json \
        README.md \
        >SHA256SUMS
)

echo "오프라인 배포 번들 생성 완료: ${BUNDLE_DIR}"
echo "서버에서는 SHA256SUMS 검증 후 images.tar를 docker image load로 반입하세요."
