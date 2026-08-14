from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from obsidian_mattermost_notifier.config import VaultConfig, parse_config
from obsidian_mattermost_notifier.mattermost import MattermostRequestError
from obsidian_mattermost_notifier.service import NotifierService


class FakePublisher:
    def __init__(self, *, fail_posts: bool = False) -> None:
        self.posts: list[tuple[str, str]] = []
        self.fail_posts = fail_posts
        self.closed = False
        self.auth_calls = 0
        self.post_attempts = 0

    def validate_auth(self) -> None:
        self.auth_calls += 1

    def channel_id_for(self, vault: object) -> str:
        return "channel-1"

    def post_message(self, channel_id: str, message: str) -> str:
        self.post_attempts += 1
        if self.fail_posts:
            raise MattermostRequestError(
                "temporary outage",
                code="connection_error",
                retryable=True,
            )
        self.posts.append((channel_id, message))
        return f"post-{len(self.posts)}"

    def close(self) -> None:
        self.closed = True


class FailFirstVaultOncePublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__()
        self.channel_calls: list[str] = []
        self.failed_once = False

    def channel_id_for(self, vault: VaultConfig) -> str:
        self.channel_calls.append(vault.vault_name)
        if vault.vault_name == "broken" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("temporary channel lookup failure")
        return f"channel-{vault.vault_name}"


class FakeWatcher:
    instances: ClassVar[list[FakeWatcher]] = []

    def __init__(
        self,
        vault: object,
        on_file: object,
        on_moved: object,
        on_missing: object,
        **_kwargs: object,
    ) -> None:
        self.vault = vault
        self.on_file = on_file
        self.on_moved = on_moved
        self.on_missing = on_missing
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    @property
    def is_alive(self) -> bool:
        return self.started


def app_config(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "enabled": True,
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0,
                    "notification_quiet_seconds": 0,
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )


def wait_until(predicate, *, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_untitled_draft_is_not_posted_until_renamed_and_quiet(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0.01,
                    "notification_quiet_seconds": 0.05,
                    "draft_name_patterns": [r"무제(?: \d+)?", r"Untitled(?: \d+)?"],
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    publisher = FakePublisher()
    service = NotifierService(config, publisher=publisher)
    service.start()
    try:
        draft = vault / "무제.md"
        draft.write_text("", encoding="utf-8")
        time.sleep(0.1)
        assert publisher.posts == []

        draft.write_text("# 회의록", encoding="utf-8")
        completed = vault / "회의록.md"
        draft.rename(completed)

        wait_until(lambda: len(publisher.posts) == 1)
        _, message = publisher.posts[0]
        assert "회의록.md" in message
        assert "무제.md" not in message
        time.sleep(0.1)
        assert len(publisher.posts) == 1
    finally:
        service.stop()


def test_quiet_period_restarts_on_document_modification(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0.01,
                    "notification_quiet_seconds": 0.15,
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    publisher = FakePublisher()
    service = NotifierService(config, publisher=publisher)
    service.start()
    try:
        document = vault / "회의록.md"
        document.write_text("초안", encoding="utf-8")
        time.sleep(0.1)
        document.write_text("# 완료", encoding="utf-8")
        time.sleep(0.1)
        assert publisher.posts == []

        wait_until(lambda: len(publisher.posts) == 1)
    finally:
        service.stop()


def test_rename_after_notification_does_not_post_again(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0.01,
                    "notification_quiet_seconds": 0.03,
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    publisher = FakePublisher()
    service = NotifierService(config, publisher=publisher)
    service.start()
    try:
        original = vault / "첫이름.md"
        original.write_text("# 문서", encoding="utf-8")
        wait_until(lambda: len(publisher.posts) == 1)

        original.rename(vault / "바뀐이름.md")
        time.sleep(0.1)

        assert len(publisher.posts) == 1
    finally:
        service.stop()


def test_restart_does_not_post_untitled_file_found_while_stopped(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(vault),
                    "vault_name": "vault",
                    "channel_id": "channel-1",
                    "settle_seconds": 0.01,
                    "notification_quiet_seconds": 0.03,
                }
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    baseline = NotifierService(config, publisher=FakePublisher())
    baseline.start()
    baseline.stop()

    draft = vault / "무제.md"
    draft.write_text("# 작성 중", encoding="utf-8")
    publisher = FakePublisher()
    restarted = NotifierService(config, publisher=publisher)
    restarted.start()
    try:
        time.sleep(0.1)
        assert publisher.posts == []

        draft.rename(vault / "재시작후완료.md")
        wait_until(lambda: len(publisher.posts) == 1)
        assert "재시작후완료.md" in publisher.posts[0][1]
    finally:
        restarted.stop()


def test_first_start_baselines_and_restart_posts_file_created_while_down(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    config = app_config(tmp_path)
    existing = config.enabled_vaults[0].vault_path / "existing.md"
    existing.write_text("# Existing", encoding="utf-8")

    first_publisher = FakePublisher()
    first = NotifierService(config, publisher=first_publisher)
    first.start()
    assert first_publisher.posts == []
    first.stop()

    new_document = config.enabled_vaults[0].vault_path / "during-stop.md"
    new_document.write_text(
        "---\ntitle: Created while stopped\n---\nsecret body", encoding="utf-8"
    )
    second_publisher = FakePublisher()
    second = NotifierService(config, publisher=second_publisher)
    try:
        second.start()
        wait_until(lambda: len(second_publisher.posts) == 1)
        assert len(second_publisher.posts) == 1
        _, message = second_publisher.posts[0]
        assert "Created while stopped" in message
        assert "during-stop.md" in message
        assert "secret body" not in message
    finally:
        second.stop()


def test_failed_post_stays_pending_for_next_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    config = app_config(tmp_path)
    baseline = NotifierService(config, publisher=FakePublisher())
    baseline.start()
    baseline.stop()

    document = config.enabled_vaults[0].vault_path / "retry.md"
    document.write_text("# Retry", encoding="utf-8")
    failed = NotifierService(config, publisher=FakePublisher(fail_posts=True))
    failed_publisher = failed._publisher
    try:
        failed.start()
        wait_until(lambda: failed_publisher.post_attempts == 1)
    finally:
        failed.stop()

    recovered_publisher = FakePublisher()
    recovered = NotifierService(config, publisher=recovered_publisher)
    try:
        recovered.start()
        wait_until(lambda: len(recovered_publisher.posts) == 1)
        assert len(recovered_publisher.posts) == 1
    finally:
        recovered.stop()


def test_one_vault_failure_does_not_stop_others_and_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    broken = tmp_path / "broken"
    healthy = tmp_path / "healthy"
    broken.mkdir()
    healthy.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(broken),
                    "vault_name": "broken",
                    "channel_id": "broken-channel",
                },
                {
                    "vault_path": str(healthy),
                    "vault_name": "healthy",
                    "channel_id": "healthy-channel",
                },
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )
    publisher = FailFirstVaultOncePublisher()
    service = NotifierService(config, publisher=publisher)
    try:
        service.start()
        assert publisher.channel_calls == ["broken", "healthy"]

        service.restart_failed_watchers()
        assert publisher.channel_calls == ["broken", "healthy", "broken"]
    finally:
        service.stop()


def test_nonretryable_channel_failure_does_not_loop_or_stop_other_vault(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    broken = tmp_path / "broken"
    healthy = tmp_path / "healthy"
    broken.mkdir()
    healthy.mkdir()
    config = parse_config(
        {
            "mattermost": {
                "url": "https://mattermost.example.com",
                "token": "test-token",
            },
            "obsidian_notifications": [
                {
                    "vault_path": str(broken),
                    "vault_name": "broken",
                    "channel_id": "broken-channel",
                },
                {
                    "vault_path": str(healthy),
                    "vault_name": "healthy",
                    "channel_id": "healthy-channel",
                },
            ],
            "state": {"database_path": str(tmp_path / "state" / "notifier.db")},
        }
    )

    class ForbiddenChannelPublisher(FakePublisher):
        def __init__(self) -> None:
            super().__init__()
            self.channel_calls: list[str] = []

        def channel_id_for(self, vault: VaultConfig) -> str:
            self.channel_calls.append(vault.vault_name)
            if vault.vault_name == "broken":
                raise MattermostRequestError(
                    "forbidden",
                    code="http_403",
                    retryable=False,
                    status_code=403,
                )
            return "healthy-channel"

    publisher = ForbiddenChannelPublisher()
    service = NotifierService(config, publisher=publisher)
    try:
        service.start()
        service.restart_failed_watchers()
        assert publisher.channel_calls == ["broken", "healthy"]
    finally:
        service.stop()


def test_authentication_is_validated_before_watchers_start(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    FakeWatcher.instances.clear()
    config = app_config(tmp_path)

    class RejectedPublisher(FakePublisher):
        def validate_auth(self) -> None:
            raise MattermostRequestError(
                "Mattermost 인증 검증 실패 (HTTP 401).",
                code="http_401",
                retryable=False,
                status_code=401,
            )

    publisher = RejectedPublisher()
    service = NotifierService(config, publisher=publisher)
    try:
        with pytest.raises(MattermostRequestError):
            service.start()
        assert FakeWatcher.instances == []
    finally:
        service.stop()


def test_watchdog_callback_does_not_wait_for_slow_mattermost(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_mattermost_notifier.service.VaultWatcher", FakeWatcher
    )
    FakeWatcher.instances.clear()
    config = app_config(tmp_path)
    post_started = threading.Event()
    release_post = threading.Event()

    class SlowPublisher(FakePublisher):
        def post_message(self, channel_id: str, message: str) -> str:
            post_started.set()
            release_post.wait(timeout=2)
            return super().post_message(channel_id, message)

    publisher = SlowPublisher()
    service = NotifierService(config, publisher=publisher)
    service.start()
    try:
        document = config.enabled_vaults[0].vault_path / "new.md"
        document.write_text("# New", encoding="utf-8")
        callback = FakeWatcher.instances[-1].on_file
        started_at = time.monotonic()
        callback(document)
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.1
        assert post_started.wait(timeout=1)
    finally:
        release_post.set()
        service.stop()
    assert publisher.closed
    assert not service._delivery_worker.is_alive
