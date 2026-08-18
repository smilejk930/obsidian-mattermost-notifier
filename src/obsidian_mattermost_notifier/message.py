from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

_H1_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_READ_LIMIT = 256 * 1024


def document_title(path: Path) -> str:
    """Return frontmatter title, first H1, or the file stem in that order."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(_READ_LIMIT)
    except OSError:
        return path.stem

    title = _frontmatter_title(text)
    if title:
        return title
    match = _H1_PATTERN.search(text)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    return path.stem


def obsidian_uri(vault_name: str, relative_path: str) -> str:
    normalized_path = relative_path.replace("\\", "/")
    return (
        "obsidian://open?vault="
        + quote(vault_name, safe="")
        + "&file="
        + quote(normalized_path, safe="")
    )


def build_message(
    *,
    vault_name: str,
    relative_path: str,
    title: str,
    observed_at: datetime,
) -> str:
    resolved_title = title.strip() if title else ""
    if not resolved_title:
        last_segment = relative_path.replace("\\", "/").rstrip("/").split("/")[-1]
        resolved_title = last_segment.removesuffix(".md")

    local_time = observed_at.astimezone()
    timestamp = local_time.strftime("%Y-%m-%d %H:%M:%S %Z").rstrip()
    uri = obsidian_uri(vault_name, relative_path)
    return "\n".join(
        (
            # "📄 새 Obsidian 문서가 생성되었습니다.",
            # "",
            # f"보관함: {_escape_markdown(vault_name)}",
            f"## {_escape_markdown(resolved_title)}",
            "",
            f"경로: {_escape_markdown(relative_path)}",
            f"감지: {timestamp}",
            "",
            f"[Obsidian에서 열기]({uri})",
        )
    )


def _frontmatter_title(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _escape_markdown(value: str) -> str:
    return re.sub(r"([\\`*<>])", r"\\\1", value.replace("\n", " "))
