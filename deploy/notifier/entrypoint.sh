#!/bin/sh
set -eu

readiness_file="${NOTIFIER_READINESS_FILE:-}"

if [ -n "${readiness_file}" ]; then
    announced=0
    while [ ! -f "${readiness_file}" ]; do
        if [ "${announced}" -eq 0 ]; then
            echo "LiveSync Bridge 초기 동기화 승인 marker를 기다립니다: ${readiness_file}"
            announced=1
        fi
        sleep 10
    done
    echo "LiveSync Bridge 초기 동기화 승인 marker를 확인했습니다."
fi

case "${1:-}" in
    -*) set -- obsidian-mattermost-notifier "$@" ;;
esac

exec "$@"
