from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from obsidian_mattermost_notifier.message import (
    build_message,
    document_title,
    obsidian_uri,
)


def test_title_priority_frontmatter_then_h1_then_filename(tmp_path: Path) -> None:
    document = tmp_path / "filename.md"
    document.write_text(
        "---\ntitle: Frontmatter title\n---\n# Heading\n", encoding="utf-8"
    )
    assert document_title(document) == "Frontmatter title"

    document.write_text("---\ntags: [test]\n---\n# Heading title\n", encoding="utf-8")
    assert document_title(document) == "Heading title"

    document.write_text("ordinary text", encoding="utf-8")
    assert document_title(document) == "filename"


def test_malformed_frontmatter_falls_back_to_h1(tmp_path: Path) -> None:
    document = tmp_path / "fallback.md"
    document.write_text("---\ntitle: [broken\n---\n# Safe heading\n", encoding="utf-8")
    assert document_title(document) == "Safe heading"


def test_obsidian_uri_encodes_vault_and_entire_relative_path() -> None:
    uri = obsidian_uri("팀 vault", "개발 문서/design #1.md")
    assert uri == (
        "obsidian://open?vault=%ED%8C%80%20vault"
        "&file=%EA%B0%9C%EB%B0%9C%20%EB%AC%B8%EC%84%9C%2Fdesign%20%231.md"
    )


def test_message_contains_metadata_but_not_document_body() -> None:
    message = build_message(
        vault_name="example_vault",
        relative_path="개발문서/설계.md",
        title="Mattermost 연동 설계",
        observed_at=datetime(2026, 8, 4, 9, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert "## 제목: Mattermost 연동 설계" in message
    assert "경로: 개발문서/설계.md" in message
    assert "감지:" in message
    assert "obsidian://open" in message


def test_build_message_fallback_title_from_relative_path() -> None:
    message = build_message(
        vault_name="example_vault",
        relative_path="그린리모델링 기능고도화/1. BE/특일 정보(공휴일) OpenAPI 연동 API 명세서.md",
        title="",
        observed_at=datetime(2026, 8, 4, 9, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert "## 제목: 특일 정보(공휴일) OpenAPI 연동 API 명세서" in message
    assert "경로: 그린리모델링 기능고도화/1. BE/특일 정보(공휴일) OpenAPI 연동 API 명세서.md" in message
    assert "obsidian://open" in message
