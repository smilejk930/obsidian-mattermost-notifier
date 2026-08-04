from __future__ import annotations

import argparse
import signal
import sys
import threading

from .config import ConfigError, load_config
from .service import NotifierService, configure_logging

DEFAULT_CONFIG_PATH = "/etc/obsidian-mattermost-notifier/config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify Mattermost about new Obsidian documents"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="YAML configuration path"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.logging.level)
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


if __name__ == "__main__":
    raise SystemExit(main())
