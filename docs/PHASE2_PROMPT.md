# Phase 2 개발 프롬프트

아래 내용을 새 Codex 세션의 첫 요청으로 사용한다.

```text
docs/HANDOFF.md 전체와 README.md, 현재 소스 및 테스트를 먼저 읽고 Phase 2 Mattermost 연결을 구현해줘.

운영 경로는 다음과 같다.

- 서버 vault 경로: /srv/obsidian/vaults/example_vault
- Obsidian vault 이름: example_vault
- 애플리케이션 설치 루트: /opt/obsidian-mattermost-notifier
- 운영 설정: /etc/obsidian-mattermost-notifier/config.yaml
- SQLite 상태 DB: /var/lib/obsidian-mattermost-notifier/notifier.db

기존 mattermost-windows-toast 프로젝트는 수정하지 말고 읽기 전용 참고만 해. 실제 token, webhook URL, 비밀번호나 문서 본문은 저장소 또는 로그에 남기지 마.

Phase 1에는 다중 vault 설정 검증, SQLite baseline/pending/sent 상태, 생성·move 감시, 재시작 중 생성 문서 복구, 제목/Obsidian URI 생성, Mattermost REST 클라이언트 골격과 mock 테스트가 구현되어 있다. 먼저 기존 구현과 테스트를 확인하고 호환성을 유지해.

Phase 2 범위:

1. 현재 mattermost.token을 PAT 또는 Bot token의 Bearer 인증으로 사용한다. Incoming Webhook 지원은 별도 결정 전까지 범위에서 제외한다.
2. 서비스 시작 시 GET /api/v4/users/me로 인증을 검증하고, 각 활성 vault의 channel_id를 확정한다. channel_id가 설정되어 있으면 우선하고, 없으면 team_name + channel_name으로 조회한다.
3. HTTP timeout, 연결 오류, 408, 429, 5xx를 재시도 가능 오류로 분류한다. 429의 Retry-After를 존중하고 capped exponential backoff와 jitter를 적용한다. 인증/권한/잘못된 요청 등 재시도 불가능한 4xx는 명확하게 보고한다.
4. 게시 실패 문서는 SQLite에서 pending을 유지한다. 제한된 횟수의 즉시 재시도가 끝난 뒤에도 서비스 재시작 없이 백그라운드 재시도되어야 한다. 이를 위해 필요한 attempt_count, next_retry_at 등의 상태와 기존 Phase 1 DB를 위한 안전한 schema migration을 구현한다.
5. 한 vault의 채널 조회, 게시 또는 재시도 실패가 다른 vault의 감시와 전송을 막지 않게 한다. 느린 Mattermost 요청이 watchdog 이벤트 스레드를 장시간 점유하지 않도록 전송 작업을 분리한다.
6. 성공 응답에서 Mattermost post ID를 저장한 뒤에만 sent로 전환한다. 이전 generation의 늦은 응답이 삭제 후 재생성된 새 generation을 완료 처리하지 못하게 한다.
7. 시작 시 남은 pending 문서를 재개하고 SIGTERM 시 watcher, retry worker, HTTP session, SQLite를 순서대로 정상 종료한다.
8. requests를 실제 네트워크 대신 mock/fake session으로 검증하는 단위 테스트를 작성한다. 인증 성공/실패, 이름 기반 채널 조회, channel_id 우선, 정상 게시, timeout, 429 Retry-After, 5xx backoff, 비재시도 4xx, 재시작 없는 복구, 다중 vault 장애 격리, SIGTERM 관련 동작을 포함한다.
9. 실제 테스트 채널에 정확히 한 건을 보내는 명시적 smoke-test 명령을 제공한다. 기본 테스트나 서비스 시작만으로 메시지가 전송되면 안 되며, 사용자가 명시적인 옵션을 줬을 때만 실행되어야 한다. 실제 자격 증명이 없으면 mock 검증까지만 완료하고 실채널 검증 절차와 미검증 상태를 보고한다.
10. config.example.yaml, README.md, docs/HANDOFF.md를 구현과 일치하도록 갱신한다. token이나 서버 응답 본문은 오류 메시지에 포함하지 않는다.

Phase 3의 systemd unit, 서비스 계정 생성, LiveSync Bridge 배포는 이번 범위에 포함하지 않는다.

완료 전 다음을 실행하고 결과를 보고해줘.

- .venv/bin/pytest -q
- .venv/bin/ruff check src tests
- .venv/bin/ruff format --check src tests
- .venv/bin/python -m compileall -q src tests
- git diff --check
- 별도 mattermost-windows-toast 참고 프로젝트를 사용했다면 Git 상태가 깨끗한지 확인

기존 사용자 변경을 보존하고 실제 비밀값이 포함되지 않았는지 확인한 뒤, 구현 결과와 남은 실환경 검증 항목을 요약해줘.
```
