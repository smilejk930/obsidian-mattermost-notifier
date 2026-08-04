from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import UTC, datetime

from .config import AppConfig, ConfigError, load_config
from .mattermost import MattermostClient, MattermostError
from .service import NotifierService, configure_logging

DEFAULT_CONFIG_PATH = "/etc/obsidian-mattermost-notifier/config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify Mattermost about new Obsidian documents"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="YAML configuration path"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check-mattermost",
        action="store_true",
        help="인증과 활성 보관함 채널 조회만 검증하고 종료",
    )
    actions.add_argument(
        "--send-test",
        metavar="VAULT_NAME",
        help="지정한 보관함 채널에 테스트 메시지 한 건을 보내고 종료",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.logging.level)
    if args.check_mattermost or args.send_test:
        try:
            if args.check_mattermost:
                check_mattermost(config)
            else:
                assert args.send_test is not None
                post_id = send_test_message(config, args.send_test)
                print(f"테스트 메시지 게시 완료: post_id={post_id}")
        except (MattermostError, ValueError) as exc:
            print(f"Mattermost 검증 오류: {exc}", file=sys.stderr)
            return 1
        return 0

    service = NotifierService(config)
    shutdown = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        service.start()
        while not shutdown.wait(timeout=5):
            service.restart_failed_watchers()
    except Exception as exc:  # noqa: BLE001 - top-level service boundary
        print(f"서비스 오류: {exc}", file=sys.stderr)
        return 1
    finally:
        service.stop()
    return 0


def check_mattermost(config: AppConfig) -> None:
    client = MattermostClient(config.mattermost)
    try:
        client.validate_auth()
        for vault in config.enabled_vaults:
            client.channel_id_for(vault)
    finally:
        client.close()


def send_test_message(config: AppConfig, vault_name: str) -> str:
    vault = next(
        (vault for vault in config.enabled_vaults if vault.vault_name == vault_name),
        None,
    )
    if vault is None:
        raise ValueError(f"활성 보관함을 찾을 수 없습니다: {vault_name}")
    client = MattermostClient(config.mattermost)
    try:
        client.validate_auth()
        channel_id = client.channel_id_for(vault)
        checked_at = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        message = "\n".join(
            (
                "🧪 Obsidian Mattermost Notifier 연결 테스트",
                "",
                f"보관함: {vault.vault_name}",
                f"검증 시각: {checked_at}",
                "",
                "이 메시지는 --send-test 옵션으로 명시적으로 전송되었습니다.",
            )
        )
        return client.post_message(channel_id, message)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
