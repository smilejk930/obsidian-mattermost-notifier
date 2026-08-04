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
- 외부 인터넷에는 접근할 수 없지만 내부 CouchDB, Mattermost 및 DNS에는 접근할 수 있다.
- 현재 운영 환경은 격리된 내부망의 Mattermost와 CouchDB에 HTTP로 연결한다.
- Docker Engine과 Compose plugin은 승인된 RPM 묶음 또는 사내 저장소로 미리 설치한다.
- GUI 없이 하나의 Docker Compose 프로젝트로 운영한다.
- LiveSync Bridge가 물질화하는 예시 호스트 보관함 경로는
  `/data/obsidian-mattermost-notifier/vaults/example_vault`이다.
- Compose, 설정, 상태 및 오프라인 번들은 `/data/obsidian-mattermost-notifier` 아래에 둔다.

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
    -> /data/obsidian-mattermost-notifier/vaults/example_vault
    -> obsidian-mattermost-notifier
    -> Mattermost REST API
```

배포 경로:

```text
인터넷 가능 개발 PC
    -> notifier image와 고정 commit의 LiveSync Bridge image 빌드
    -> docker image save로 images.tar 및 전송 손상 확인용 SHA256SUMS 생성
    -> 승인된 매체로 Rocky Linux 서버에 반입
    -> docker image load
    -> 하나의 compose.yaml로 두 컨테이너 실행(pull/build 금지)
```

### LiveSync Bridge를 사용하는 이유

- Self-hosted LiveSync 데이터를 Rocky Linux의 일반 파일시스템으로 물질화할 수 있다.
- CouchDB 데이터의 청크, E2EE, 경로 난독화를 직접 해석하는 코드를 새로 만들 필요가 없다.
- 여러 보관함을 각각 로컬 경로로 미러링할 수 있다.
- 운영 배포는 저장소의 `compose.example.yaml`을 서버에서 `compose.yaml`로 복사하여 사용하는
  Docker Compose 방식으로 한다.

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
/data/obsidian-mattermost-notifier/
├── config/notifier/config.yaml                     # notifier 운영 설정
├── config/livesync-bridge/config.json               # Bridge 운영 설정
├── state/notifier/notifier.db                       # SQLite 상태
├── state/livesync-bridge/                           # Bridge 상태
└── vaults/example_vault/                            # LiveSync Bridge 보관함 미러
```

로그는 별도 파일보다 stdout/stderr와 journald를 기본으로 사용한다. notifier 서비스
계정은 `/data/obsidian-mattermost-notifier/vaults/example_vault`를 읽을 수 있고 설정 및 상태 파일에 필요한
최소 권한만 갖는다. 보관함 미러에 대한 쓰기 권한은 주지 않는다.

## 5. 설정 형식

다중 보관함은 `obsidian_notifications` 배열로 처리한다. 아래 `vault_path`와
`database_path`는 notifier 컨테이너 내부 경로이며, 호스트 경로와의 대응은 README의
운영 경로 표를 따른다.

```yaml
mattermost:
  url: "http://mattermost.internal.example"
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

### 재시작 후 보완 전송 범위

보완 전송은 현재 완료 조건인 `재시작 중 생성된 문서는 복구 후 한 번 게시`를 만족하기
위해 필요하다. 이 기능을 제거하면 notifier 또는 서버가 정지한 동안 생성된 문서는 알림
없이 지나간다는 운영상 유실을 허용해야 한다.

고정된 `최근 N시간` 범위는 두지 않는다. 유효한 SQLite 상태 DB가 있으면 마지막 실행
상태와 재시작 시점의 파일 목록을 비교하여, 현재 존재하지만 DB에 기록되지 않은 Markdown
문서를 모두 보완 대상으로 삼는다. 즉 시간 추정이 아니라 상태 차이를 기준으로 하므로 긴
점검이나 장애에도 문서를 놓치지 않는다.

- 최초 설치 또는 유효한 상태 DB가 없는 재구축은 현재 파일 전체를 baseline으로만 등록하고
  알림을 보내지 않는다. DB 유실을 장기 장애로 오인하여 기존 문서 알림이 쏟아지는 것을
  막기 위한 fail-safe 정책이다.
- 장시간 중단 뒤 보완 대상이 많아도 누락시키는 시간 제한 대신 기존 worker와 rate limit
  처리를 통해 순차 전송한다.
- 서비스 중단 중 생성되었다가 재시작 전에 삭제된 파일은 재시작 시 파일 목록에 없으므로
  이 방식으로 복구할 수 없다.

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

실제 운영에서는 자동화 신원과 권한을 개인 계정에서 분리한 전용 Bot token을 Bearer
인증으로 사용한다. PAT는 운영 자격 증명으로 사용하지 않는다.

- Bearer token으로 인증과 채널을 조회한다.
- `POST /api/v4/posts`로 메시지를 작성한다.
- 서비스 시작 시 `GET /api/v4/users/me`로 자격 증명을 검증한다.
- Incoming Webhook은 고정 채널 게시에는 단순하지만 API 조회용 별도 자격 증명이
  필요하므로 현재 구현 범위에서 제외한다.

## 11. Docker Compose 오프라인 운영 요구사항

- notifier와 LiveSync Bridge를 인터넷 가능한 개발 PC에서 모두 빌드한다.
- 운영 빌드는 깨끗한 Git 작업 트리와 고정된 source revision을 사용한다.
- Python base image는 digest, Python 의존성은 버전과 wheel hash, Bridge는 commit 전체 SHA로
  고정한다.
- 두 이미지를 `docker image save`로 하나의 `images.tar`에 담고 전송 중 손상 확인용
  SHA-256 checksum과 build metadata를 함께 반입한다. checksum은 출처를 증명하는
  전자서명을 대신하지 않는다.
- 운영 서버에서는 `docker build`, `docker pull`, `pip install`과 Deno 패키지 다운로드를
  실행하지 않는다.
- `compose.example.yaml`을 운영 서버에서 `compose.yaml`로 복사해 실제 경로를 작성한다.
- Compose에는 `build:`를 두지 않고 `pull_policy: never` 및 `--pull never --no-build`를
  사용한다.
- 두 컨테이너는 하나의 Compose 프로젝트로 운영하며 `restart: unless-stopped`를 적용한다.
- Bridge health가 정상이어야 notifier를 시작할 수 있게 하되, 최초 원격 문서 반영 완료는
  운영자가 별도 readiness marker로 승인한다.
- notifier는 marker가 생길 때까지 대기하고, 승인 후 처음 보는 현재 문서를 baseline으로
  등록한 다음 watcher를 시작한다.
- 설정 파일은 read-only, vault는 Bridge에 read-write/notifier에 read-only, SQLite와 Bridge
  상태는 각각 별도의 영속 bind mount로 제공한다.
- 컨테이너는 non-root, read-only root filesystem, `cap_drop: ALL`,
  `no-new-privileges`로 실행하고 호스트 SELinux를 유지한다.
- 로그는 stdout/stderr로 출력하여 Docker logging driver가 수집한다. 운영 daemon이
  `journald` driver를 사용하면 journald에서 조회한다.
- SIGTERM을 받아 watcher, HTTP session과 DB를 정상 종료하고 30초 grace period를 둔다.
- `/etc/localtime`을 read-only mount한다. 현재 내부 HTTP 배포에는 CA bundle을 mount하지 않는다.
- 이전 이미지 번들 및 이미지 태그 파일을 최소 한 세대 보관하여 registry 없이 rollback한다.

예상 파일:

```text
Dockerfile
compose.example.yaml
scripts/build-offline-bundle.sh
deploy/notifier/entrypoint.sh
deploy/livesync-bridge/Dockerfile
deploy/livesync-bridge/config.example.json
```

### 최초 초기 동기화 승인

Compose `depends_on`과 Bridge health는 프로세스 및 peer 상태를 확인하지만 CouchDB의 기존
문서가 파일시스템에 모두 반영되었다는 업무적 완료 시점을 보장하지 않는다. 빈 mirror에서
notifier가 먼저 baseline을 만들면 뒤이어 내려오는 모든 기존 문서가 신규 문서로 처리될 수
있다.

따라서 최초 배포에서는 Bridge가 healthy가 된 뒤 로그, 대상 vault의 파일 수 및 일정 시간
변경이 안정적인지를 운영자가 확인하고 다음 marker를 만든다.

```text
/data/obsidian-mattermost-notifier/state/notifier/bridge-initial-sync.complete
```

marker는 일반 재부팅과 이미지 갱신에는 유지한다. mirror 또는 notifier DB를 처음부터
재구축할 때만 notifier를 먼저 중지하고 marker 및 baseline 재생성 절차를 수행한다.

## 12. 보안 요구사항

- 실제 PAT, Webhook URL, CouchDB 비밀번호, E2EE passphrase를 저장소에 커밋하지 않음
- 예시 설정에는 `CHANGE_ME`만 사용
- `/data/obsidian-mattermost-notifier/config/notifier/config.yaml`은 `devsvr`와 notifier
  컨테이너만 읽도록 제한
- Mattermost `verify_ssl` 기본값은 `true`로 유지하되 현재 HTTP 연결에는 적용하지 않는다.
- HTTP에서는 Mattermost token과 CouchDB 인증 정보가 전송 구간에서 암호화되지 않으므로
  방화벽으로 해당 포트 접근을 필요한 서버 사이에만 제한한다.
- 로그에 token, webhook URL, CouchDB 암호, 문서 본문을 남기지 않음
- 알림 본문에는 제목과 경로만 포함하고 실제 문서 본문은 전송하지 않음
- LiveSync Bridge 암호 설정은 notifier 설정과 분리

### LiveSync E2EE와 path obfuscation의 의미

두 설정은 CouchDB에 저장되는 원격 데이터에서 무엇을 숨길지 정한다. 네트워크 전송 구간을
보호하는 TLS와는 별개의 계층이며, 현재 HTTP 배포에서는 전송 구간이 암호화되지 않는다.

공식 설정 설명: <https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/settings.md#3-privacy--encryption>

| 설정 | 보호하는 것 | 설정하지 않았을 때 |
| --- | --- | --- |
| E2EE (end-to-end encryption) | 문서 본문과 동기화 데이터를 클라이언트에서 암호화하여 CouchDB 관리자나 DB 백업만으로 내용을 읽지 못하게 함 | CouchDB 접근 권한을 가진 주체가 저장된 내용을 볼 수 있음 |
| path obfuscation | CouchDB 문서 식별자에 드러날 수 있는 폴더명과 파일 경로를 알아보기 어려운 값으로 변환 | E2EE를 켜도 `인사/평가결과.md` 같은 경로 정보가 별도로 노출될 수 있음 |

예를 들어 E2EE만 사용하면 문서 본문은 숨겨져도 파일명과 폴더명에서 프로젝트명이나
고객명을 추측할 수 있다. path obfuscation까지 사용하면 이 메타데이터 노출도 줄인다.
path obfuscation은 파일 내용 암호화를 대신하지 않으므로 기밀성이 필요하면 E2EE와 함께
사용한다.

LiveSync Bridge는 암호화된 CouchDB 데이터를 일반 파일로 물질화해야 하므로 다음 값이
필요하다.

Bridge 설정 설명: <https://github.com/vrtmrz/livesync-bridge#configuration>

- `passphrase`: 해당 보관함의 LiveSync E2EE passphrase. E2EE를 사용하지 않을 때만 빈 값.
- `obfuscatePassphrase`: path obfuscation에 사용한 passphrase. 사용하지 않을 때는 빈 값이며
  E2EE passphrase와 다른 값일 수도 있다.

모든 Obsidian 클라이언트와 Bridge가 해당 보관함에 대해 동일한 설정과 passphrase를
사용해야 한다. Bridge에 passphrase를 제공하면 CouchDB 및 DB 백업의 노출은 줄일 수
있지만, Bridge 프로세스와 물질화된
`/data/obsidian-mattermost-notifier/vaults/...` 파일은 평문을 읽을 수
있다. 따라서 서버 침해까지 막아 주는 기능은 아니며 미러 디렉터리 권한과 서버 보안은
계속 필요하다.

신규 보관함에는 E2EE와 path obfuscation을 모두 활성화하는 것을 권장한다. 이미 운영 중인
보관함은 설정을 즉시 바꾸지 말고 먼저 현재 모든 클라이언트의 설정을 확인하고 백업한 뒤,
LiveSync가 요구하는 DB rebuild 또는 경로 변환 절차를 별도 점검한다. passphrase를 잃으면
원격 데이터를 복호화할 수 없으므로 비밀번호 관리자 등 별도 안전한 저장소에도 복구본을
보관한다.

### Bridge 비밀 배포 방식

- 저장소의 `compose.example.yaml`과 예시 설정에는 실제 비밀값을 넣지 않는다.
- 운영 서버의 `/data/obsidian-mattermost-notifier/config/livesync-bridge/config.json`에 CouchDB 계정,
  `passphrase`, `obfuscatePassphrase`를 저장하고 컨테이너의
  `/run/secrets/livesync-bridge-config.json`에 read-only bind mount하고 `LSB_CONFIG`로
  해당 경로를 지정한다.
- 호스트 파일은 `devsvr` 및 Bridge 컨테이너 GID만 읽을 수 있게 소유권을 지정하고 `0640`
  이하로 제한한다. 컨테이너의 실행 UID/GID가 읽을 수 있는지는 배포 시 확인한다.
- compose 출력, journald, 셸 명령행에 비밀값을 직접 넣지 않는다. 비밀 파일과 백업의 접근
  권한 및 교체 절차도 함께 관리한다.
- Mattermost Bot token은 운영 서버의
  `/data/obsidian-mattermost-notifier/config/notifier/config.yaml`에
  별도로 두어 Bridge가 읽지 못하게 한다.

## 13. 추천 프로젝트 구조

```text
obsidian-mattermost-notifier/
├── README.md
├── docs/
│   ├── HANDOFF.md
│   └── PHASE2_PROMPT.md
├── pyproject.toml
├── Dockerfile
├── .dockerignore
├── compose.example.yaml
├── requirements.build.lock
├── requirements.runtime.lock
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
│   ├── notifier/
│   │   └── entrypoint.sh
│   └── livesync-bridge/
│       ├── Dockerfile
│       └── config.example.json
├── scripts/
│   └── build-offline-bundle.sh
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

- [x] non-root notifier Docker image와 고정/hash 검증된 Python 의존성
- [x] 고정 commit의 LiveSync Bridge를 함께 빌드하는 오프라인 bundle script
- [x] Bridge와 notifier를 묶은 `compose.example.yaml`
- [x] 초기 동기화 운영자 승인 marker
- [x] read-only 설정/vault, 영속 상태 및 SELinux mount 정책
- [x] 이미지 save/load, checksum, update 및 rollback 문서
- [ ] 운영 Rocky Linux에서 Compose 기동, SELinux 볼륨 접근 및 내부 HTTP 연결 검증
- [ ] 재부팅/네트워크 장애/보관함별 장애/이전 이미지 rollback 테스트

### Phase 4: 운영 안정화

- [x] Bridge heartbeat와 notifier process/marker container health check
- [ ] 필요 시 metrics 추가
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
- 운영 서버가 외부 registry나 패키지 저장소에 접근하지 않고 두 이미지를 기동할 수 있다.
- 최초 Bridge 동기화 승인 전에는 notifier가 baseline이나 알림 전송을 시작하지 않는다.

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

## 17. 운영 결정 및 확인할 사항

확정:

- 실제 운영 Mattermost 인증은 전용 Bot token을 사용한다.
- notifier와 LiveSync Bridge는 개발 PC에서 이미지로 빌드하고 하나의 `compose.yaml`로
  오프라인 Rocky Linux 서버에서 운영한다.
- 저장소에는 `compose.example.yaml`만 제공하며 운영자는 이를 `compose.yaml`로 복사하여
  운영 경로와 이미지 버전을 작성한다.
- 최초 Bridge 동기화 완료는 자동 health만으로 판단하지 않고 운영자 승인 marker를 사용한다.
- 재시작 후 보완 전송은 고정 시간 범위 없이 유효한 SQLite DB와 현재 파일 목록의 차이를
  기준으로 한다. 최초 설치 또는 DB 유실 시에는 전체 파일을 baseline 처리한다.

배포 전에 확인:

- 현재 보관함에서 E2EE와 path obfuscation을 이미 사용 중인지 모든 클라이언트 설정을
  확인한다. 신규 보관함에는 둘 다 활성화를 권장하며, 기존 보관함의 변경은 백업과 공식
  변환 절차 확인 후 수행한다.

### Phase 1에서 확정한 정책

- 삭제 이벤트가 관측된 뒤 동일 상대 경로가 재생성되면 새 generation으로 기록하고 다시 알린다.
- 메시지 시각은 timezone 정보가 있는 이벤트 최초 감지 시각을 사용하며 `감지`로 표시한다.
- `channel_id`가 있으면 `team_name + channel_name` 조회보다 우선한다.
- 컨테이너 내부 기본 설정 경로는 `/etc/obsidian-mattermost-notifier/config.yaml`이며,
  호스트의 `/data/obsidian-mattermost-notifier/config/notifier/config.yaml`을 여기에
  read-only로 마운트한다.

### Phase 2에서 확정한 정책

- Incoming Webhook은 제외하고 PAT 또는 Bot token의 Bearer 인증을 사용한다.
- 서비스 시작 시 `/api/v4/users/me`로 인증을 검증한다.
- 게시 요청은 watchdog 스레드가 아닌 전용 worker에서 처리한다.
- 재시도 가능 오류는 408, 429, 5xx, timeout 및 연결 오류이다.
- 재시도 불가능한 4xx는 자동 재시도를 중단하고 설정 수정 후 재시작 시 재개한다.
- `Retry-After`, capped exponential backoff 및 jitter를 적용하고 상태를 SQLite에 보존한다.
- 실제 메시지는 `--send-test VAULT_NAME`을 명시한 경우에만 smoke test로 전송한다.
