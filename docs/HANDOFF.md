# WSL Codex 개발 인수인계

## 1. 프로젝트 목적

팀은 Obsidian으로 개발 문서를 작성하고 Self-hosted LiveSync(CouchDB)로 공유한다. 새 Markdown 문서가 생성되면 24시간 운영되는 Rocky Linux 개발서버가 단일 발송자가 되어 Mattermost의 지정 채널로 알림을 게시해야 한다.

프로젝트명은 `obsidian-mattermost-notifier`이다.

별도 프로젝트 `mattermost-windows-toast`는 Windows 데스크톱 알림 수신 프로그램이며 수정하지 않는다. 필요하면 읽기 전용 참고 자료로만 사용한다.

## 2. 현재 환경과 확인된 사항

### 클라이언트

- Windows에서 Obsidian을 사용한다.
- 예시 보관함 경로: `D:\Obsidian\example_vault`
- 예시 보관함 이름: `example_vault`
- 커뮤니티 플러그인 Self-hosted LiveSync와 CouchDB로 팀 문서를 동기화한다.
- Mattermost Desktop을 사용한다.

### 서버

- 24시간 운영되는 Rocky Linux 개발서버가 있다.
- 기본 systemd target은 `multi-user.target`이다.
- GUI가 없는 systemd 서비스로 운영하는 것이 적합하다.
- LiveSync Bridge가 물질화하는 예시 보관함 경로는 `/srv/obsidian/vaults/example_vault`이다.
- notifier 애플리케이션의 예시 설치 경로는 `/opt/obsidian-mattermost-notifier`이다.

### 기존 Windows 앱

프로젝트명: `mattermost-windows-toast`

확인된 구조:

- Python 단일 실행 프로그램
- `requests`, `websocket-client`, `windows-toasts`, `PyYAML` 사용
- Mattermost PAT로 REST 인증
- Mattermost WebSocket `posted` 이벤트 수신
- Windows Toast 표시
- PyInstaller one-file/no-console 빌드
- 설정 파일은 실행 파일 옆의 `config.yaml`
- 확인 당시 Git 작업 트리는 깨끗했음

이 앱은 `windows-toasts`에 의존하므로 Rocky Linux에서 그대로 실행하지 않는다. Mattermost REST 처리 방식만 참고할 수 있다.

## 3. 결정된 아키텍처

```text
팀원 Obsidian
    -> Self-hosted LiveSync / CouchDB
    -> LiveSync Bridge
    -> /srv/obsidian/vaults/example_vault
    -> obsidian-mattermost-notifier
    -> Mattermost REST API
```

### LiveSync Bridge를 사용하는 이유

- Self-hosted LiveSync 데이터를 Rocky Linux의 일반 파일시스템으로 물질화할 수 있다.
- CouchDB 데이터의 청크, E2EE, 경로 난독화를 직접 해석하는 코드를 새로 만들 필요가 없다.
- 여러 보관함을 각각 로컬 경로로 미러링할 수 있다.
- Deno 직접 실행 또는 Docker Compose 운영이 가능하다.

LiveSync Bridge: <https://github.com/vrtmrz/livesync-bridge>

Obsidian 공식 Headless는 공식 Obsidian Sync용이다. Self-hosted LiveSync는 공식 Obsidian Sync와 호환되지 않으므로 현재 CouchDB 구성의 대체 수단으로 사용하지 않는다.

## 4. 핵심 운영 결정

### 서버 한 대만 알림 발송

각 팀원 PC에서 파일 감시를 실행하면 LiveSync가 모든 PC에 같은 문서를 생성하므로 중복 알림이 발생한다. Rocky Linux 서버 한 대만 Mattermost에 게시한다.

### 서버 미러 보호

LiveSync Bridge는 양방향 동기화가 가능하므로 서버 미러를 사람이 편집하지 않도록 한다.

- Bridge 전용 계정만 미러 경로에 쓰기 가능
- 알림 서비스 계정은 미러 경로 읽기만 가능
- SQLite 상태 파일과 로그는 보관함 밖에 저장
- 서버 미러에서 편집기, formatter, backup 프로그램이 파일을 변경하지 않도록 주의

예시 운영 경로:

```text
/srv/obsidian/vaults/example_vault                  # LiveSync Bridge 보관함 미러
/opt/obsidian-mattermost-notifier/                  # notifier 설치 루트
/etc/obsidian-mattermost-notifier/config.yaml       # 운영 설정(서비스 계정만 읽기)
/var/lib/obsidian-mattermost-notifier/notifier.db   # SQLite 상태(보관함 밖)
```

로그는 별도 파일보다 stdout/stderr와 journald를 기본으로 사용한다. notifier 서비스
계정은 `/srv/obsidian/vaults/example_vault`를 읽을 수 있고 설정 및 상태 파일에 필요한
최소 권한만 갖는다. 보관함 미러에 대한 쓰기 권한은 주지 않는다.

## 5. 설정 형식

다중 보관함은 `obsidian_notifications` 배열로 처리한다.

```yaml
mattermost:
  url: "https://mattermost.example.com"
  token: "CHANGE_ME"
  verify_ssl: true
  request_timeout_seconds: 10
  immediate_retry_attempts: 3
  retry_base_seconds: 1
  retry_max_seconds: 300
  retry_jitter_ratio: 0.2

obsidian_notifications:
  - enabled: true
    vault_path: "/srv/obsidian/vaults/example_vault"
    vault_name: "example_vault"
    team_name: "dev-team"
    channel_name: "development-docs"
    ignore_folders:
      - ".obsidian"
      - ".trash"
    settle_seconds: 2

  - enabled: false
    vault_path: "/srv/obsidian/vaults/another_vault"
    vault_name: "another_vault"
    team_name: "dev-team"
    channel_name: "another-docs"
    ignore_folders:
      - ".obsidian"
      - ".trash"
    settle_seconds: 2

state:
  database_path: "/var/lib/obsidian-mattermost-notifier/notifier.db"

logging:
  level: "INFO"
```

### 채널 식별

사람이 `channel_id`를 복사하지 않도록 `team_name + channel_name`을 기본 입력으로 사용하고 서비스 시작 시 Mattermost API로 `channel_id`를 조회한다.

API:

```text
GET /api/v4/teams/name/{team_name}/channels/name/{channel_name}
```

직접 `channel_id`를 지정하는 선택적 고급 설정을 지원해도 된다. 둘 다 지정되면 `channel_id`를 우선하거나 설정 오류로 처리하는 정책을 명시한다.

Mattermost API: <https://developers.mattermost.com/api-documentation/>

## 6. `settle_seconds` 의미

파일 생성 이벤트를 받은 후 Mattermost 전송 전까지 기다리는 초 단위 시간이다.

- LiveSync가 파일을 생성한 직후 내용을 여러 번 쓸 수 있다.
- 임시 파일 생성, 이동, 이름 변경 이벤트가 연속해서 발생할 수 있다.
- 대기 중 파일 크기와 수정 시간이 안정되었는지도 검사한다.
- 기본값은 2초이며 느린 환경에서는 3~5초로 조정한다.

이 값은 문서 생성 시각을 바꾸지 않고 알림 전송만 지연한다.

## 7. 파일 감시 요구사항

- `watchdog` 또는 동등한 Linux 파일 감시 기능 사용
- `.md` 파일만 대상
- `ignore_folders` 하위 경로 제외
- 생성 이벤트와 외부 동기화에서 자주 발생하는 move/rename 이벤트 처리
- 단순 내용 수정은 새 문서 알림으로 보내지 않음
- 각 보관함 감시기는 독립적으로 실패하고 재시작 가능해야 함
- 한 보관함 오류가 다른 보관함 감시를 중단시키지 않아야 함
- `enabled: false` 항목은 감시하지 않음
- 존재하지 않는 경로, 중복된 `vault_name`, 중복된 정규화 경로는 시작 시 명확히 보고

## 8. 상태 및 중복 방지

SQLite를 사용한다. 상태 파일은 미러 보관함 밖에 둔다.

최소 저장 정보:

- vault name
- normalized relative path
- observed file size
- modified timestamp
- optional content hash
- first observed time
- Mattermost post ID
- notification sent time

권장 동작:

1. 최초 설치 시 현재 존재하는 문서는 baseline으로 등록하고 과거 문서 알림은 보내지 않는다.
2. 그 이후 재시작 시 현재 파일 목록과 DB를 비교해 서비스 중단 중 생성된 문서를 보완 전송한다.
3. 같은 이벤트가 여러 번 들어와도 `vault_name + normalized relative path`와 상태를 이용해 한 번만 전송한다.
4. 삭제 후 같은 경로로 새 문서를 다시 만든 경우를 구분할 정책을 테스트로 명시한다.
5. 전송 실패 시 성공으로 기록하지 않고 제한된 지수 백오프로 재시도한다.

Linux에서는 Windows/macOS와 달리 신뢰할 수 있는 birth time이 항상 제공되지 않으므로 파일 생성 시각은 다음 우선순위를 검토한다.

1. 이벤트 최초 관측 시각
2. 파일시스템 제공 시각이 신뢰 가능한 경우 해당 값
3. 필요 시 frontmatter의 `created` 값

메시지에는 의미가 모호하지 않도록 `감지 시각` 또는 합의된 명칭을 사용한다.

## 9. Mattermost 메시지

필수 정보:

- 문서 제목
- 보관함 이름
- 보관함 기준 상대 경로
- 생성 또는 최초 감지 시각
- Obsidian 문서 열기 링크

예시:

```text
📄 새 Obsidian 문서가 생성되었습니다.

보관함: example_vault
제목: Mattermost 연동 설계
경로: 개발문서/Mattermost 연동 설계.md
감지: 2026-08-03 17:30:12 KST

[Obsidian에서 열기](obsidian://open?vault=example_vault&file=개발문서%2FMattermost%20연동%20설계.md)
```

### 문서 제목 결정

다음 우선순위를 권장한다.

1. frontmatter의 `title`
2. 최초 H1 제목
3. 확장자를 제외한 파일명

`obsidian://open` 링크의 vault와 file 값은 URL encoding한다. 수신자 PC의 Obsidian에 동일한 `vault_name`이 등록되어 있어야 한다.

## 10. Mattermost 인증

PAT 또는 Bot token을 Bearer 인증으로 사용한다. 자동화 신원과 권한을 개인 계정에서
분리할 수 있는 전용 Bot 계정을 권장하며, PAT는 초기 검증이나 제한적인 대안으로
사용할 수 있다.

- Bearer token으로 인증과 채널을 조회한다.
- `POST /api/v4/posts`로 메시지를 작성한다.
- 서비스 시작 시 `GET /api/v4/users/me`로 자격 증명을 검증한다.
- Incoming Webhook은 고정 채널 게시에는 단순하지만 API 조회용 별도 자격 증명이
  필요하므로 현재 구현 범위에서 제외한다.

## 11. systemd 운영 요구사항

- `network-online.target` 이후 시작
- 비정상 종료 시 자동 재시작
- 설정 파일은 `/etc/obsidian-mattermost-notifier/config.yaml`에서 읽기
- 비밀값은 Git에 저장하지 않기
- 전용 비로그인 서비스 계정 사용
- 로그는 stdout/stderr로 출력하여 journald가 수집
- SIGTERM을 받아 watcher와 DB를 정상 종료
- 서비스 시작 전 모든 enabled 보관함 경로와 Mattermost 인증을 검증

예상 단위:

```text
livesync-bridge.service         # 또는 Docker Compose systemd wrapper
obsidian-mattermost.service
```

알림 서비스는 LiveSync Bridge 이후 시작하도록 순서를 지정하되, 미러 경로가 늦게 생기는 상황에서도 재시도할 수 있어야 한다.

## 12. 보안 요구사항

- 실제 PAT, Webhook URL, CouchDB 비밀번호, E2EE passphrase를 저장소에 커밋하지 않음
- 예시 설정에는 `CHANGE_ME`만 사용
- `/etc/obsidian-mattermost-notifier/config.yaml`은 서비스 계정만 읽도록 제한
- Mattermost TLS 검증은 기본 `true`; 자체 CA를 설치하는 방식을 우선하고 무조건적인 검증 해제를 피함
- 로그에 token, webhook URL, CouchDB 암호, 문서 본문을 남기지 않음
- 알림 본문에는 제목과 경로만 포함하고 실제 문서 본문은 전송하지 않음
- LiveSync Bridge 암호 설정은 notifier 설정과 분리

## 13. 추천 프로젝트 구조

```text
obsidian-mattermost-notifier/
├── README.md
├── docs/
│   ├── HANDOFF.md
│   └── PHASE2_PROMPT.md
├── pyproject.toml
├── config.example.yaml
├── src/
│   └── obsidian_mattermost_notifier/
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py
│       ├── delivery.py
│       ├── mattermost.py
│       ├── retry.py
│       ├── watcher.py
│       ├── state.py
│       ├── message.py
│       └── service.py
├── tests/
│   ├── test_config.py
│   ├── test_delivery.py
│   ├── test_main.py
│   ├── test_mattermost.py
│   ├── test_watcher.py
│   ├── test_state.py
│   └── test_message.py
├── deploy/
│   ├── obsidian-mattermost.service
│   └── livesync-bridge/
└── .gitignore
```

## 14. 구현 단계

### Phase 1: 로컬 코어

- Python 패키지와 설정 모델 작성
- 배열형 다중 보관함 검증
- SQLite baseline 및 중복 방지
- 파일 생성/move 감시
- 제목 및 Obsidian URI 생성
- Mattermost API mock 기반 단위 테스트

### Phase 2: Mattermost 연결

- [x] PAT 또는 Bot token Bearer 인증 및 `/users/me` 시작 검증
- [x] `team_name + channel_name`을 channel ID로 해석
- [x] 비동기 게시, SQLite 재시도 상태 및 408/429/5xx 지속 재시도
- [x] 명시적인 `--send-test VAULT_NAME` 단일 메시지 검증 명령
- [ ] 실제 운영 자격 증명을 사용한 테스트 채널 검증

### Phase 3: Rocky Linux 배포

- 전용 계정 및 디렉터리
- systemd unit
- LiveSync Bridge 미러 연동
- 서비스 재시작/네트워크 장애/보관함별 장애 테스트

### Phase 4: 운영 안정화

- metrics 또는 health check 선택
- DB 정리 정책
- 설치 및 장애대응 문서

## 15. 완료 조건

- 두 개 이상의 보관함을 동시에 감시할 수 있다.
- 보관함마다 서로 다른 Mattermost 채널을 지정할 수 있다.
- 새 문서는 정확히 한 번 게시된다.
- 일반 문서 수정은 게시하지 않는다.
- 제외 폴더의 문서는 게시하지 않는다.
- 최초 실행 시 기존 문서를 일괄 게시하지 않는다.
- 재시작 중 생성된 문서는 복구 후 한 번 게시된다.
- Mattermost 장애 후 복구 시 유실 없이 재시도한다.
- 한 보관함 장애가 다른 보관함에 영향을 주지 않는다.
- SIGTERM 시 정상 종료한다.
- 실제 비밀값이 Git에 포함되지 않는다.

`정확히 한 번`은 동일 파일 이벤트와 SQLite generation 기준이다. Mattermost가 게시를
완료한 뒤 HTTP 응답만 유실되는 네트워크 경계에서는 현재 create-post 호출에
idempotency key를 사용하지 않으므로, 유실 방지를 우선하는 at-least-once 재시도에
따라 동일 메시지가 중복될 가능성이 남는다.

## 16. WSL Codex 첫 요청

```text
docs/HANDOFF.md 전체를 읽고 Phase 1을 구현해줘.
기존 mattermost-windows-toast 프로젝트는 수정하지 말고 읽기 전용 참고만 해.
먼저 현재 저장소 상태를 확인하고, pyproject 기반 패키지와 테스트를 만든 다음 테스트 결과까지 보고해줘.
```

## 17. 아직 결정할 사항

- 실제 운영에서 전용 Bot token을 사용할지 제한적으로 PAT를 사용할지
- LiveSync Bridge를 Docker Compose로 운영할지 Deno systemd 서비스로 운영할지
- LiveSync E2EE 및 path obfuscation 활성화 여부와 서버 비밀 배포 방식
- 서버가 과거에 놓친 문서를 어느 시간 범위까지 보완 전송할지

### Phase 1에서 확정한 정책

- 삭제 이벤트가 관측된 뒤 동일 상대 경로가 재생성되면 새 generation으로 기록하고 다시 알린다.
- 메시지 시각은 timezone 정보가 있는 이벤트 최초 감지 시각을 사용하며 `감지`로 표시한다.
- `channel_id`가 있으면 `team_name + channel_name` 조회보다 우선한다.
- 예시 기본 설정 경로는 `/etc/obsidian-mattermost-notifier/config.yaml`이다.

### Phase 2에서 확정한 정책

- Incoming Webhook은 제외하고 PAT 또는 Bot token의 Bearer 인증을 사용한다.
- 서비스 시작 시 `/api/v4/users/me`로 인증을 검증한다.
- 게시 요청은 watchdog 스레드가 아닌 전용 worker에서 처리한다.
- 재시도 가능 오류는 408, 429, 5xx, timeout 및 연결 오류이다.
- 재시도 불가능한 4xx는 자동 재시도를 중단하고 설정 수정 후 재시작 시 재개한다.
- `Retry-After`, capped exponential backoff 및 jitter를 적용하고 상태를 SQLite에 보존한다.
- 실제 메시지는 `--send-test VAULT_NAME`을 명시한 경우에만 smoke test로 전송한다.
