# Obsidian Mattermost Notifier

Rocky Linux에서 Self-hosted LiveSync로 동기화된 Obsidian 보관함을 감시하고, 새 Markdown 문서가 생기면 지정된 Mattermost 채널에 알리는 서버 서비스입니다.

Phase 1 로컬 코어와 Phase 2 Mattermost 연결이 구현되어 있습니다. 다중 보관함 감시, SQLite baseline 및 중복 방지, Bearer 인증 검증, 채널 조회, 비동기 게시와 지속 재시도, rate limit 처리, 명시적 smoke test를 포함합니다. Phase 3은 외부 인터넷을 사용할 수 없는 Rocky Linux 서버를 위한 Docker Compose 배포입니다.

## 운영 경로

운영 서버의 영속 파일은 모두 `/data/obsidian-mattermost-notifier` 아래에 둡니다. 이 디렉터리는
호스트 운영 계정 `devsvr`가 관리하고, 컨테이너가 쓰는 상태 및 vault 디렉터리만 컨테이너의
숫자 UID/GID에 맞춰 권한을 분리합니다.

```text
/data/obsidian-mattermost-notifier/
├── compose.yaml
├── .env
├── config/
│   ├── notifier/config.yaml
│   └── livesync-bridge/config.json
├── state/
│   ├── notifier/notifier.db
│   └── livesync-bridge/
└── vaults/
    ├── example_vault/
    └── another_vault/
```

Compose bind mount에서 콜론 왼쪽은 위의 **운영 서버 경로**, 오른쪽은 이미지가 사용하는
**컨테이너 내부 경로**입니다. `/etc`, `/var/lib`, `/srv`, `/app`처럼 보이는 오른쪽 경로는
운영 서버에 별도로 만들지 않습니다.

| 용도 | 운영 서버 경로 | 컨테이너 내부 경로 |
| --- | --- | --- |
| notifier 설정 | `/data/obsidian-mattermost-notifier/config/notifier/config.yaml` | `/etc/obsidian-mattermost-notifier/config.yaml` |
| Bridge 설정 | `/data/obsidian-mattermost-notifier/config/livesync-bridge/config.json` | `/run/secrets/livesync-bridge-config.json` |
| vault 상위 디렉터리 | `/data/obsidian-mattermost-notifier/vaults` | Bridge: `/app/data`, notifier: `/srv/obsidian/vaults` |
| notifier 상태 | `/data/obsidian-mattermost-notifier/state/notifier` | `/var/lib/obsidian-mattermost-notifier` |
| Bridge 상태 | `/data/obsidian-mattermost-notifier/state/livesync-bridge` | `/var/lib/livesync-bridge` |

Obsidian URI의 `vault` 값은 서버 디렉터리명이 아니라 각 사용자 PC의 Obsidian에 등록된 보관함 이름이어야 합니다. 예시에서는 서버 경로와 클라이언트 보관함 이름을 `example_vault`로 통일합니다.

## 개발 PC에서 직접 실행

이 절차는 개발과 테스트용이다. 운영 서버에서는 Python 패키지를 직접 설치하지 않고 아래의
오프라인 Docker 배포 절차를 사용한다. Python 3.11 이상이 필요하다.

```bash
cd /path/to/obsidian-mattermost-notifier
python3 -m venv .venv
.venv/bin/pip install .
cp config.example.yaml config.yaml
```

`config.yaml`의 Mattermost URL, 토큰, 팀 및 채널을 실제 값으로 바꾼 뒤 실행합니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config ./config.yaml
```

로그는 stdout/stderr로 출력합니다. `SIGTERM`과 `SIGINT`를 받으면 감시기, HTTP 세션,
SQLite 연결을 순서대로 닫습니다.

### Mattermost 연결 검증

인증과 모든 활성 보관함의 채널 조회만 검증하며 메시지는 보내지 않습니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config ./config.yaml \
  --check-mattermost
```

실제 채널 smoke test는 보관함 이름을 명시했을 때만 테스트 메시지 한 건을 보냅니다. 이
명령은 자동 테스트에서 실행되지 않습니다.

```bash
.venv/bin/obsidian-mattermost-notifier \
  --config ./config.yaml \
  --send-test example_vault
```

## 오프라인 Docker 배포

운영 서버는 외부 인터넷에 접근하지 못하지만 내부 CouchDB, Mattermost 및 DNS에는 접근할 수
있다는 전제다. 현재 운영망에서는 Mattermost와 CouchDB에 내부 HTTP로 연결한다. notifier와
LiveSync Bridge 이미지는 인터넷이 가능한 개발 PC에서 함께 빌드하고 하나의 Compose
프로젝트로 운영한다. 운영 서버에서는 `docker build`, `docker pull`, `pip install` 또는
Deno 패키지 다운로드를 실행하지 않는다.

HTTP에서는 Mattermost Bot token과 CouchDB 인증 정보가 전송 구간에서 암호화되지 않는다.
따라서 이 구성은 신뢰할 수 있는 격리망으로 범위를 제한하고, 방화벽에서 Mattermost와
CouchDB 접근을 필요한 서버 사이에만 허용한다. 망 신뢰 조건이 바뀌면 내부 CA 또는
TLS reverse proxy를 도입한 뒤 HTTPS로 전환한다.

### 사전 조건

- 개발 PC: Git, Docker Engine 또는 Docker Desktop, Buildx, Bash, Python 3.11 이상
- 운영 서버: 사전에 오프라인 설치한 Docker Engine과 Compose plugin
- 기본 지원 대상: `linux/amd64`. 다른 아키텍처는 Python wheel hash lock을 별도로 갱신해야 한다.
- 운영 서버에서 내부 CouchDB와 Mattermost의 URL, DNS 및 포트에 접근할 수 있어야 한다.

### 1. 개발 PC에서 이미지와 반입 번들 생성

운영 빌드는 추적 가능한 소스만 사용하도록 깨끗한 Git 작업 트리에서 실행한다.

```bash
./scripts/build-offline-bundle.sh
```

스크립트는 다음 작업을 수행한다.

- digest로 고정한 Python base image와 hash lock된 Python 의존성으로 notifier 이미지 빌드
- 고정된 LiveSync Bridge commit을 clone하여 Bridge 이미지 빌드
- 두 이미지를 하나의 `images.tar`로 저장
- Compose 예시, 설정 예시, 이미지 태그, source revision 및 이미지 inspect 결과 포함
- 이동식 매체 반입 중 파일 손상을 확인하기 위한 `SHA256SUMS` 생성

결과는 기본적으로 다음 디렉터리에 생성된다.

```text
dist/offline-bundle-<version>-<git-sha>/
├── images.tar
├── compose.example.yaml
├── image-versions.env
├── notifier-config.example.yaml
├── livesync-bridge-config.example.json
├── BUILD-METADATA.txt
├── image-inspect.json
├── SHA256SUMS
└── README.md
```

이미 존재하는 출력 디렉터리는 덮어쓰지 않는다. 검증 목적의 미커밋 소스 빌드만
`ALLOW_DIRTY=1`로 허용하며 이 이미지는 운영 배포에 사용하지 않는다. Bridge 버전을 바꿀
때는 검증한 commit 전체 SHA를 명시한다.

```bash
LIVESYNC_BRIDGE_REF=<verified-full-commit-sha> \
BUNDLE_DIR=/path/to/new-bundle \
./scripts/build-offline-bundle.sh
```

### 2. 번들을 운영 서버로 반입하고 검증

번들 생성이 끝났다면 개발 PC에서 할 작업은 완료된 것이다. 생성된
`offline-bundle-<version>-<git-sha>` 디렉터리 전체를 승인된 이동식 매체에 복사한다.

이 절부터는 **Rocky Linux 운영 서버에서 실행한다.** 아래 `/path/to/...`는 이동식 매체에
있는 실제 번들 경로로 바꾼다. 번들을 `/data/obsidian-mattermost-notifier`에 복사한 직후,
어떤 파일도 수정하기 전에 checksum을 검증하고 이미지를 적재한다.

```bash
sudo install -d -o devsvr -g devsvr -m 0755 \
  /data/obsidian-mattermost-notifier
cp -a \
  /path/to/offline-bundle-<version>-<git-sha>/. \
  /data/obsidian-mattermost-notifier/
cd /data/obsidian-mattermost-notifier
sha256sum -c SHA256SUMS
docker image load -i images.tar
```

배포 번들과 Compose 파일은 `devsvr:devsvr` 소유로 유지한다. `root:root`로 재귀 변경하지
않는다. 서비스 데이터 전체를 `devsvr` 소유로 만드는 대신, 아래 단계에서 컨테이너가 실제로
쓰는 디렉터리만 해당 숫자 UID/GID로 설정한다.

`SHA256SUMS` 검증이 하나라도 실패하면 이미지 적재와 배포를 중단하고 번들을 다시 반입한다.
이 값은 반입 과정의 우발적인 파일 손상을 확인하기 위한 것이며, 번들의 출처나 악의적인
위변조까지 증명하는 서명은 아니다. 그런 검증이 필요하면 checksum 파일을 별도 신뢰 채널로
전달하거나 전자서명을 추가한다.

### 3. 운영 서버 디렉터리 준비

Compose 예시에 사용된 호스트 디렉터리를 만들고 컨테이너가 접근할 수 있는 소유권과 권한을
설정한다.

```bash
sudo install -d -o devsvr -g 10001 -m 2750 \
  /data/obsidian-mattermost-notifier/config/notifier
sudo install -d -o devsvr -g 1993 -m 2750 \
  /data/obsidian-mattermost-notifier/config/livesync-bridge
sudo install -d -o 1993 -g 1993 -m 0750 \
  /data/obsidian-mattermost-notifier/state/livesync-bridge
sudo install -d -o 10001 -g 10001 -m 0750 \
  /data/obsidian-mattermost-notifier/state/notifier
sudo install -d -o 1993 -g 20001 -m 2770 \
  /data/obsidian-mattermost-notifier/vaults
```

- notifier 컨테이너 UID/GID는 `10001:10001`이다.
- 고정한 Bridge image의 `deno` UID/GID는 `1993:1993`이다.
- `20001`은 Bridge와 notifier가 공유하는 vault 읽기 그룹이다. Compose가 두 컨테이너에
  supplementary group으로 추가한다.
- 호스트 배포 계정의 UID/GID는 위 컨테이너 UID/GID와 일치할 필요가 없다. 호스트 디렉터리는
  `sudo`로 준비하고 실제 파일 접근은 컨테이너의 숫자 UID/GID 및 Compose의 `group_add`로
  제어한다.
- 호스트 배포 계정은 기본적으로 `20001` 그룹에 넣지 않는다. 초기 동기화 확인처럼 호스트에서
  vault를 직접 조회해야 할 때는 `sudo`를 사용하여 서버 미러의 우발적인 편집을 방지한다.
- 설정 디렉터리는 setgid를 적용해 편집 후 교체된 파일도 컨테이너용 그룹을 상속하게 한다.
  운영 설정 파일은 `devsvr`와 해당 컨테이너 GID만 읽도록 `0640` 이하로 제한한다.
- Rocky Linux SELinux를 비활성화하지 않는다. Compose의 `z`/`Z` mount label을 유지한다.

### 4. 운영 설정 작성과 서비스 기동

번들 디렉터리에서 Compose 파일과 이미지 버전 파일을 만들고, 설정 예시를 프로젝트의
`config` 아래 운영 설정으로 복사한다.

```bash
cd /data/obsidian-mattermost-notifier
cp compose.example.yaml compose.yaml
cp image-versions.env .env
cp notifier-config.example.yaml config/notifier/config.yaml
cp livesync-bridge-config.example.json config/livesync-bridge/config.json
sudo chown devsvr:10001 config/notifier/config.yaml
sudo chown devsvr:1993 config/livesync-bridge/config.json
chmod 0640 \
  config/notifier/config.yaml \
  config/livesync-bridge/config.json
```

두 설정 파일을 편집한다. 실제 Bot token, CouchDB 암호, E2EE passphrase와 path
obfuscation passphrase는 이미지, Git 저장소 및 `compose.yaml`에 넣지 않는다.

```bash
vi /data/obsidian-mattermost-notifier/config/notifier/config.yaml
vi /data/obsidian-mattermost-notifier/config/livesync-bridge/config.json
```

다음 값을 빠짐없이 운영값으로 바꾼다.

- notifier 설정: `mattermost.url`, `mattermost.token`, 각 vault의 `vault_path`,
  `vault_name`, `team_name`/`channel_name` 또는 `channel_id`
- Bridge 설정: CouchDB `url`, `database`, `username`, `password`와 현재 Obsidian
  LiveSync 보관함에 설정된 `passphrase`, `obfuscatePassphrase`
- Bridge storage peer의 `baseDir`: Compose 컨테이너 내부 경로인
  `data/<vault-directory>/`; notifier의 `vault_path`는 이에 대응하는
  `/srv/obsidian/vaults/<vault-directory>`이다. 두 값 모두 컨테이너 내부 경로이며, 호스트에서는
  `/data/obsidian-mattermost-notifier/vaults/<vault-directory>`에 저장된다.

#### LiveSync Bridge 설정

vault 하나에는 같은 `group`을 사용하는 `couchdb` peer와 `storage` peer가 한 쌍 필요하다.
다음 예시는 CouchDB의 `example_vault` 데이터베이스를 서버의 `example_vault` 디렉터리로
물질화한다.

```json
{
  "peers": [
    {
      "type": "couchdb",
      "name": "example-vault-remote",
      "group": "example-vault",
      "database": "example_vault",
      "username": "CHANGE_ME",
      "password": "CHANGE_ME",
      "url": "http://couchdb.internal.example:5984",
      "passphrase": "CHANGE_ME",
      "obfuscatePassphrase": "CHANGE_ME",
      "baseDir": "",
      "useRemoteTweaks": true
    },
    {
      "type": "storage",
      "name": "example-vault-storage",
      "group": "example-vault",
      "baseDir": "data/example_vault/",
      "scanOfflineChanges": false,
      "useChokidar": false
    }
  ]
}
```

| 항목 | 설명 |
| --- | --- |
| `peers` | Bridge가 연결할 CouchDB 및 파일 저장소 목록이다. vault가 여러 개면 peer 쌍을 반복해서 추가한다. |
| `type` | `couchdb`는 원격 LiveSync DB, `storage`는 서버 파일시스템을 뜻한다. |
| `name` | 설정 파일 안에서 peer를 구분하는 고유 이름이다. CouchDB DB명이나 Obsidian vault 이름과 같을 필요는 없다. |
| `group` | 같은 vault에 속한 peer를 연결한다. 한 쌍에는 같은 값을 사용하고 서로 다른 vault에는 다른 값을 사용한다. |
| `database` | Obsidian Self-hosted LiveSync에 설정된 실제 CouchDB DB명이다. Fauxton의 `_users`, `_replicator` 같은 시스템 DB는 지정하지 않는다. |
| `username`, `password` | 해당 DB에 접근할 CouchDB 자격 증명이다. Mattermost 토큰과는 별개다. |
| `url` | CouchDB 기본 주소만 지정한다. Fauxton 관리 화면 경로인 `/_utils/#/_all_dbs`는 포함하지 않는다. |
| `passphrase` | 해당 vault의 LiveSync E2EE passphrase다. E2EE를 사용하지 않을 때만 빈 문자열로 둔다. |
| `obfuscatePassphrase` | 해당 vault의 path obfuscation passphrase다. 기능을 사용하지 않을 때만 빈 문자열로 둔다. |
| CouchDB `baseDir` | 원격 vault 안의 일부 하위 경로만 공유할 때 사용한다. 빈 문자열은 vault 전체를 뜻한다. |
| `useRemoteTweaks` | 원격 DB에 저장된 LiveSync 조정값을 적용하고 설정 호환성을 확인한다. |
| storage `baseDir` | Bridge 컨테이너의 파일 저장 경로다. Compose 구성에서는 반드시 `data/<vault-directory>/` 아래를 사용한다. |
| `scanOfflineChanges` | 시작할 때 storage에 이미 있던 파일을 원격 변경으로 스캔할지 정한다. 최초 CouchDB → 빈 mirror 구성에서는 `false`를 유지한다. |
| `useChokidar` | 기본 Deno 파일 감시 대신 Chokidar를 사용할지 정한다. Linux에서 기본 감시에 문제가 없으면 `false`를 유지한다. |

같은 `group`에 속한 모든 peer 사이에서 변경이 전달되므로, 서로 다른 vault에 같은 `group`을
재사용하면 데이터가 섞일 수 있다. 다중 vault는 다음과 같이 각 쌍을 분리한다.

```text
CouchDB example_vault  <─ group: example-vault ─>  data/example_vault/
CouchDB team_docs      <─ group: team-docs     ─>  data/team_docs/
```

#### notifier 설정

다음 예시는 Mattermost의 `engineering` 팀과 `dev-docs` 채널에 `example_vault`의 새 문서
알림을 보낸다. Mattermost 채널 URL이
`http://mattermost.internal.example:8065/engineering/channels/dev-docs`라면 URL의 두 경로
조각을 각각 `team_name`과 `channel_name`으로 사용한다.

```yaml
mattermost:
  url: "http://mattermost.internal.example:8065"
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
    team_name: "engineering"
    channel_name: "dev-docs"
    ignore_folders:
      - ".obsidian"
      - ".trash"
      - "images"
      - "templates"
    settle_seconds: 2

state:
  database_path: "/var/lib/obsidian-mattermost-notifier/notifier.db"

logging:
  level: "INFO"
```

| 항목 | 설명 |
| --- | --- |
| `mattermost.url` | Mattermost 기본 주소다. 팀 및 채널 경로는 포함하지 않는다. |
| `mattermost.token` | 알림 전송 전용 Bot token을 사용한다. 설정 파일 밖에 노출하거나 Git에 커밋하지 않는다. |
| `verify_ssl` | HTTPS 인증서 검증 여부다. HTTP 연결에는 적용되지 않으며 향후 HTTPS 전환을 위해 `true`를 유지한다. |
| `request_timeout_seconds` | Mattermost HTTP 요청 한 번의 제한 시간이다. |
| `immediate_retry_attempts` | 일시적 오류가 발생했을 때 즉시 재시도할 최대 횟수다. |
| `retry_base_seconds` | 지수 backoff의 최초 기준 지연 시간이다. |
| `retry_max_seconds` | 지속 재시도 사이의 최대 지연 시간이다. |
| `retry_jitter_ratio` | 여러 요청이 동시에 재시도되는 현상을 줄이기 위해 지연 시간에 적용할 무작위 비율이다. |
| `enabled` | `false`이면 해당 vault의 경로를 검사하거나 감시하지 않는다. |
| `vault_path` | notifier **컨테이너 내부** vault 경로다. 호스트 `/data/.../vaults` 경로를 직접 쓰지 않는다. |
| `vault_name` | 각 사용자 PC의 Obsidian에 등록된 vault 이름이다. Obsidian URI 생성과 중복 식별에 사용한다. |
| `team_name` | Mattermost 팀 표시명이 아니라 팀 URL에 나타나는 이름이다. |
| `channel_name` | Mattermost 채널 표시명이 아니라 `/channels/` 다음에 나타나는 채널 URL 이름이다. |
| `channel_id` | 선택 항목이다. 지정하면 `team_name`과 `channel_name` 조회를 생략하고 이 값을 우선한다. |
| `ignore_folders` | vault 루트 기준 상대 폴더 경로다. 예를 들어 `images`는 루트의 `images/`를, `archive/private`는 해당 중첩 폴더를 제외한다. |
| `settle_seconds` | 파일 크기와 수정 시각이 이 시간 동안 안정된 뒤 새 문서로 처리한다. |
| `state.database_path` | notifier 컨테이너 내부 SQLite 경로다. Compose가 호스트 `state/notifier` 디렉터리를 이 위치에 연결한다. |
| `logging.level` | 로그 수준이다. 운영 기본값은 `INFO`다. |

notifier는 `.md` 파일만 처리한다. 따라서 `문서목록.base` 같은 `.base` 파일이나 이미지 파일은
`ignore_folders`에 추가하지 않아도 자동으로 제외된다. `images`처럼 폴더 전체를 제외하려는
항목만 지정한다.

현재 운영 환경에서는 notifier의 `mattermost.url`에 `http://<Mattermost-IP>`, Bridge의
CouchDB `url`에 `http://<CouchDB-IP>` 형식을 사용한다. CouchDB URL에는 Fauxton 관리
화면 경로인 `/_utils/#/_all_dbs`를 넣지 않는다. `verify_ssl`은 HTTP에서는 사용되지 않지만,
향후 HTTPS 전환 시 안전한 기본값을 유지하도록 `true`로 둔다. Mattermost가 notifier와 같은
호스트에서 실행되어도 컨테이너 안의 `localhost`는 notifier 컨테이너 자신을 가리키므로,
호스트에 공개된 Mattermost 포트의 실제 서버 IP 또는 내부 DNS 이름을 사용한다.

서비스 기동 전에 운영 서버에서 HTTP 포트 접근을 확인한다. Mattermost는 `200`, CouchDB는
인증 설정에 따라 `200`, `401` 또는 `403`이면 HTTP 서버까지 연결된 것이다.

```bash
curl -v --connect-timeout 5 \
  http://<Mattermost-IP>/api/v4/system/ping
curl -v --connect-timeout 5 \
  http://<CouchDB-IP>/
```

기본 호스트 경로를 그대로 사용한다면 `compose.yaml`은 수정할 필요가 없다. 다른 경로를
사용할 때만 두 설정 파일과 `compose.yaml`의 bind mount 경로를 함께 변경한다. 설정이 끝나면
Compose 구성을 검사하고 서비스를 기동한다.

```bash
cd /data/obsidian-mattermost-notifier
docker compose config --quiet
docker compose up -d --pull never --no-build
```

현재 사용자에게 Docker socket 권한이 없다면 위의 `docker` 명령에는 `sudo`를 붙인다.

Compose는 `pull_policy: never`를 사용한다. 필요한 이미지가 로컬에 없으면 실패하며 외부
registry에서 자동으로 가져오지 않는다.

### 5. 최초 동기화 승인 marker

최초 기동에서 notifier는 다음 marker가 생길 때까지 실제 애플리케이션을 시작하지 않는다.

```text
/data/obsidian-mattermost-notifier/state/notifier/bridge-initial-sync.complete
```

이는 빈 mirror를 먼저 baseline 처리한 뒤 Bridge가 내려받는 모든 기존 문서를 신규 문서로
오인하는 일을 방지한다. 다음 순서로 한 번만 승인한다.

1. `docker compose ps`에서 Bridge가 healthy인지 확인한다.
2. `docker compose logs -f livesync-bridge`와
   `/data/obsidian-mattermost-notifier/vaults`의 파일을 확인한다.
3. 모든 대상 vault의 초기 문서 반영이 완료되고 일정 시간 파일 수와 변경이 안정적인지 확인한다.
4. 아래 명령으로 marker를 생성한다.

```bash
sudo install -o 10001 -g 10001 -m 0640 /dev/null \
  /data/obsidian-mattermost-notifier/state/notifier/bridge-initial-sync.complete
docker compose logs -f obsidian-mattermost-notifier
```

marker는 일반적인 재부팅, 컨테이너 재생성 및 이미지 갱신 때 유지한다. mirror 또는 notifier
DB를 처음부터 재구축할 때는 notifier를 먼저 중지하고 marker 및 baseline 재생성 절차를
별도로 수행해야 한다.

### 6. 연결 검증

notifier가 시작된 뒤 다음 명령으로 인증과 채널 조회를 확인한다.

```bash
docker compose exec obsidian-mattermost-notifier \
  obsidian-mattermost-notifier \
  --config /etc/obsidian-mattermost-notifier/config.yaml \
  --check-mattermost
```

실제 테스트 메시지는 명시적으로 요청한 경우에만 보낸다.

```bash
docker compose exec obsidian-mattermost-notifier \
  obsidian-mattermost-notifier \
  --config /etc/obsidian-mattermost-notifier/config.yaml \
  --send-test example_vault
```

### 7. 갱신과 rollback

새 번들은 기존 번들과 다른 디렉터리에 보관하고 checksum 검증 후 이미지를 추가로 load한다.
`.env`의 두 이미지 태그를 새 버전으로 바꾸고 다음 명령으로 재생성한다.

```bash
docker compose up -d --pull never --no-build
```

문제가 있으면 `.env`를 보존한 이전 이미지 태그로 되돌리고 같은 명령을 실행한다. rollback을
위해 최소 한 세대의 이전 `images.tar`, `.env`, `BUILD-METADATA.txt`를 유지한다. vault,
notifier SQLite DB 및 Bridge 상태 디렉터리는 이미지와 독립적으로 백업한다.

Docker 이미지 반입에는 `docker image save`/`docker image load`를 사용한다. Compose에서
registry 접근을 금지하는 방식은 Docker의 `pull_policy: never` 동작을 따른다.

- <https://docs.docker.com/reference/cli/docker/image/save/>
- <https://docs.docker.com/reference/cli/docker/image/load/>
- <https://docs.docker.com/reference/compose-file/services/#pull_policy>

## 설정 정책

설정 형식은 [config.example.yaml](config.example.yaml)을 참고합니다.

- `obsidian_notifications`에 두 개 이상의 보관함을 지정할 수 있습니다.
- `enabled: false`인 항목은 경로 존재 여부를 검사하거나 감시하지 않습니다.
- 활성 보관함의 `vault_name`과 정규화된 `vault_path`는 중복될 수 없습니다.
- `channel_id`가 있으면 이를 우선 사용하고 채널 조회를 생략합니다.
- `channel_id`가 없으면 `team_name`과 `channel_name`이 모두 필요합니다.
- 상태 DB는 활성 보관함 경로 밖의 절대 경로여야 합니다.
- `verify_ssl`은 HTTPS 연결에서만 적용되며 기본값은 `true`입니다. 현재 내부 HTTP 배포에서는
  사용되지 않습니다.
- 구현은 PAT 또는 Bot token의 Bearer 인증을 지원하지만 실제 운영의 `mattermost.token`에는
  전용 Bot token을 사용합니다.
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

Debian/Ubuntu 계열 개발 PC에서 `ensurepip is not available` 오류가 나면 먼저 배포판의
`python3-venv` 패키지가 필요합니다.

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
