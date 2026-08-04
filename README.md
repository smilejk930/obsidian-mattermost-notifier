# Obsidian Mattermost Notifier

Rocky Linux에서 Self-hosted LiveSync로 동기화된 Obsidian 보관함을 감시하고, 새 Markdown 문서가 생기면 지정된 Mattermost 채널에 알리는 서버 서비스입니다.

Phase 1 로컬 코어와 Phase 2 Mattermost 연결이 구현되어 있습니다. 다중 보관함 감시, SQLite baseline 및 중복 방지, Bearer 인증 검증, 채널 조회, 비동기 게시와 지속 재시도, rate limit 처리, 명시적 smoke test를 포함합니다. 운영용 systemd 구성은 Phase 3 범위입니다.

## 운영 경로

아래는 공개 문서와 예제 설정에서 사용하는 예시 배치입니다. 실제 운영 경로는 환경에 맞게 변경합니다.

```text
/srv/obsidian/vaults/example_vault                  # LiveSync Bridge 보관함 미러(읽기 전용)
/opt/obsidian-mattermost-notifier/                  # 애플리케이션 설치 루트
/etc/obsidian-mattermost-notifier/config.yaml       # 운영 설정
/var/lib/obsidian-mattermost-notifier/notifier.db   # SQLite 상태
```

Obsidian URI의 `vault` 값은 서버 디렉터리명이 아니라 각 사용자 PC의 Obsidian에 등록된 보관함 이름이어야 합니다. 예시에서는 서버 경로와 클라이언트 보관함 이름을 `example_vault`로 통일합니다.

## 설치 및 실행

Python 3.11 이상이 필요합니다.

```bash
cd /opt/obsidian-mattermost-notifier
python3 -m venv .venv
.venv/bin/pip install .
cp config.example.yaml config.yaml
```

`config.yaml`의 Mattermost URL, 토큰, 팀 및 채널을 실제 값으로 바꾼 뒤 실행합니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config /etc/obsidian-mattermost-notifier/config.yaml
```

로그는 stdout/stderr로 출력되므로 systemd에서는 journald가 수집할 수 있습니다. `SIGTERM`과 `SIGINT`를 받으면 감시기, HTTP 세션, SQLite 연결을 순서대로 닫습니다.

### Mattermost 연결 검증

인증과 모든 활성 보관함의 채널 조회만 검증하며 메시지는 보내지 않습니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config /etc/obsidian-mattermost-notifier/config.yaml \
  --check-mattermost
```

실제 채널 smoke test는 보관함 이름을 명시했을 때만 테스트 메시지 한 건을 보냅니다. 이 명령은 자동 테스트에서 실행되지 않습니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config /etc/obsidian-mattermost-notifier/config.yaml \
  --send-test example_vault
```

## 설정 정책

설정 형식은 [config.example.yaml](config.example.yaml)을 참고합니다.

- `obsidian_notifications`에 두 개 이상의 보관함을 지정할 수 있습니다.
- `enabled: false`인 항목은 경로 존재 여부를 검사하거나 감시하지 않습니다.
- 활성 보관함의 `vault_name`과 정규화된 `vault_path`는 중복될 수 없습니다.
- `channel_id`가 있으면 이를 우선 사용하고 채널 조회를 생략합니다.
- `channel_id`가 없으면 `team_name`과 `channel_name`이 모두 필요합니다.
- 상태 DB는 활성 보관함 경로 밖의 절대 경로여야 합니다.
- TLS 인증서 검증은 기본으로 활성화됩니다.
- `mattermost.token`은 PAT 또는 Bot token의 Bearer 인증에 사용합니다.
- `request_timeout_seconds`는 각 HTTP 요청 제한 시간입니다.
- `immediate_retry_attempts` 동안 짧은 지수 백오프를 사용하고, 이후에는 `retry_max_seconds` 간격으로 백그라운드 재시도합니다.
- HTTP 429에서는 Mattermost의 `Retry-After`가 로컬 backoff보다 길면 해당 값을 우선합니다.

## 문서 판정과 중복 방지

최초 실행 시 기존 Markdown 파일을 SQLite에 baseline으로 기록하며 알림을 보내지 않습니다. 이후 재시작할 때 파일 목록과 DB를 비교하여 서비스가 정지한 동안 생긴 문서를 pending으로 복구합니다.

생성 및 move/rename 이벤트만 새 문서 후보가 됩니다. 일반 수정 이벤트는 알림을 만들지 않습니다. 파일 크기와 수정 시각이 `settle_seconds` 동안 유지된 뒤 처리하며, 중복 이벤트는 `vault_name + 상대 경로`로 제거합니다.

삭제가 관측된 뒤 같은 상대 경로에 Markdown 파일이 다시 생기면 새 generation으로 간주해 다시 알립니다. 전송에 실패한 문서는 시도 횟수와 다음 재시도 시각을 SQLite에 보존하며 서비스 재시작 없이 전용 worker가 다시 시도합니다. 재시도 불가능한 4xx는 pending 상태로 보존하되 자동 재시도를 멈추고, 설정 수정 후 서비스가 재시작되면 다시 확인합니다.

파일 이벤트 중복과 늦게 도착한 이전 generation 응답은 SQLite 조건부 갱신으로 차단합니다. 다만 Mattermost가 POST를 처리한 뒤 HTTP 응답만 유실되는 경우에는 현재 구현이 create-post 요청에 idempotency key를 사용하지 않으므로 재시도 과정에서 동일 메시지가 중복될 가능성이 있습니다. 이 경우 유실 방지를 우선하는 at-least-once 정책을 사용합니다.

문서 제목은 다음 순서로 결정합니다.

1. YAML frontmatter의 `title`
2. 최초 H1
3. 확장자를 제외한 파일명

Mattermost에는 제목과 경로 등 메타데이터만 보내며 문서 본문은 포함하지 않습니다.

## 개발 및 테스트

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

Debian/Ubuntu 계열 WSL에서 `ensurepip is not available` 오류가 나면 먼저 배포판의 `python3-venv` 패키지가 필요합니다. Rocky Linux에서는 사용하는 Python 버전에 맞는 venv 패키지를 설치합니다.

설계 배경과 단계별 요구사항은 [개발 인수인계](docs/HANDOFF.md), Phase 2 구현 요청은 [Phase 2 개발 프롬프트](docs/PHASE2_PROMPT.md)를 참고합니다. 문서의 경로와 이름은 모두 공개용 예시값입니다.

## 프로젝트 구조

```text
src/obsidian_mattermost_notifier/
├── __main__.py     # CLI, signal 처리
├── config.py       # YAML 설정 모델과 검증
├── delivery.py     # 비동기 게시 및 지속 재시도 worker
├── mattermost.py   # 채널 조회 및 게시 REST 클라이언트
├── message.py      # 제목, 메시지, Obsidian URI
├── retry.py        # backoff 및 jitter 정책
├── service.py      # 보관함별 수명주기와 전송 조정
├── state.py        # SQLite baseline, pending, 중복 방지
└── watcher.py      # 생성·이동 감시와 파일 안정화
```

## 라이선스

이 저장소는 [LICENSE](LICENSE)를 따릅니다.
