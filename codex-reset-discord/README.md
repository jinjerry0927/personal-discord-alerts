# 개인 Discord용 Tibo 원문·Codex 리셋 알림

Python 3.11 표준 라이브러리만 사용합니다. AI 모델을 호출하지 않으므로 AI API 요금은 없습니다.

## 두 채널

| 채널 예시 | 받는 내용 | GitHub Actions Secret |
|---|---|---|
| codex-알림 | 리셋 예정 / 리셋 적용 / 리셋권 지급 안내 | DISCORD_WEBHOOK_URL |
| tibo-원문 | API 피드에 수집된 Tibo 글 전체, 개인적인 글 포함 | TIBO_FEED_WEBHOOK_URL |

원문 채널은 단어로 필터링하거나 리셋 여부를 판단하지 않습니다. API가 제공하는 영어 텍스트와 원문 링크를 전달합니다. 줄바꿈과 문구를 유지하며 멘션 알림은 차단합니다. 긴 글은 일부만 표시한다고 밝히고 전체 글 링크를 제공합니다. 이미지·영상은 X 링크에서 확인합니다.

**X의 모든 게시물을 직접 구독하는 구조가 아닙니다.** codex-reset.com이 수집하지 않은 글, 피드에서 빠진 글, API에 없는 원문은 전달할 수 없습니다. 처음 연결하면 현재 피드를 기준점으로 저장하고 과거 글을 보내지 않습니다. 이후 새로 나타난 24시간 이내의 글부터 전달합니다.

원문 Secret을 추가하지 않으면 원문 채널만 비활성화되고 기존 리셋 채널은 계속 작동합니다.

## 리셋 판단과 시간 표시

- 예정: 명시적인 전체/사용량 리셋 예고를 찾습니다. 짧은 “Landing within the next 2 hours”는 API의 명시적인 답글 연결을 따라 같은 Tibo 대화에 리셋 맥락이 있을 때만 예고로 분류합니다.
- 적용 · Tibo 확인: 명시적인 적용 확인 문구 또는 같은 리셋 대화의 “Done and dusted” 같은 확인 답글을 구분합니다.
- 적용 · 추적 서비스 관측: 제공자의 문서화된 적용 신호를 따르되, 운영자 관측/확인 대기라고 표시된 항목은 Tibo 직접 확인과 구분합니다.
- 리셋권: banked reset의 지급 안내·진행·확인 상태를 일반 리셋과 구분합니다.

API 제공자도 분류하지만 최종 Discord 문구와 추가 판단은 우리 코드의 고정 규칙입니다. AI가 영어 의미를 완전히 이해하는 구조는 아닙니다. 농담·중의적인 표현·누락된 답글 맥락은 여전히 오탐/누락 원인이 될 수 있습니다. 애매한 글은 원문 채널에서 직접 확인할 수 있습니다.

같은 대화에서 적용이 이미 확인된 경우 지난 예고를 새로 보내지 않습니다. 원글과 답글은 API에 명시된 연결만 사용하며 게시 시간이 비슷하다는 이유로 묶지 않습니다.

각 알림은 가능한 경우 다음을 표시합니다.

- X 게시 시각: API가 제공한 원문 게시 시각
- 우리 프로그램 최초 감지: 이 버전이 해당 ID를 처음 읽은 시각
- 게시 → 최초 감지: API 수집·공개 지연과 다음 예약 실행 대기를 합친 차이
- 이번 전송 요청 시각: Discord에 요청하기 직전의 실행 시각
- API 피드 갱신 시각: API의 fetched_at이며 해당 글의 최초 수집 시각과는 다름
- 추적 서비스 기록 시각: 원문 게시 시각과 구분되는 적용/관측 기록 시각

API가 원문 시각을 제공하지 않으면 추정하지 않고 미제공으로 표시합니다. 예고에 “within the next 2 hours”처럼 명시된 상대 시간이 있으면 **원문 게시 시각**에 더한 기한을 표시합니다. 개인 계정에 실제 적용되는 시각을 보장하지 않습니다.

## 기존 저장소에서 원문 채널 연결

1. 개인 Discord 서버에 텍스트 채널 `tibo-원문`을 만듭니다.
2. 해당 채널용 새 웹훅을 만들고 URL을 복사합니다.
3. 저장소 Settings → Secrets and variables → Actions → New repository secret에서 이름을 `TIBO_FEED_WEBHOOK_URL`로 지정하고 URL을 저장합니다.
4. Actions → Codex Reset Alerts → Run workflow → `test-feed`로 원문 채널에 테스트 메시지 1건이 오는지 확인합니다.
5. 다음 `send` 실행에서는 원문 채널 기준점만 저장합니다. 그 이후 새 글부터 전송합니다.

웹훅은 GitHub Secret에 직접 입력하며 코드·채팅·스크린샷에 넣지 않습니다. 기존 `DISCORD_WEBHOOK_URL`은 그대로 둡니다. 두 Secret에는 서로 다른 채널의 웹훅을 사용합니다.

## 실행과 상태

현재 저장소 구조는 다음과 같습니다.

- `.github/workflows/codex-alerts.yml`
- `codex-reset-discord/monitor.py`
- `codex-reset-discord/test_monitor.py`, `test_updates.py`
- `codex-reset-discord/state/`

cron-job.org가 10분마다 GitHub의 `workflow_dispatch`를 `mode=send`로 호출합니다. GitHub 자체 schedule은 중복 예약 방지를 위해 제거했습니다. workflow 전체를 비활성화하면 외부 예약도 실행되지 않습니다.

매번 timeline/feed를 조회하고 새 알림만 보냅니다. 원문 채널과 리셋 채널은 성공 기록을 따로 저장합니다. 한 채널의 전송 실패가 있어도 같은 실행에서 다른 채널의 전송을 시도합니다.

- `state/state.json`: 리셋 알림의 성공 기록
- `state/feed-state.json`: 원문 채널의 성공 기록
- `state/observations.json`: 게시물 ID별 최초 감지 시각. 본문과 비밀값은 없음
- `state/cooldown.json`: HTTP 429 재시도 대기

새 글이나 상태 변화가 없으면 상태 파일을 계속 커밋하지 않습니다. 각 전송은 Discord가 메시지 ID로 성공을 확인한 뒤 기록합니다. 상태 커밋 실패나 응답 유실 때 드물게 중복이 발생할 수 있습니다.

기존 버전에서 처음 업데이트하면 새 분류 규칙의 기준점을 한 번 저장하므로 과거 공지가 재전송되지 않습니다. 업데이트 전 최초 감지 시각은 복원하지 않습니다.

API 응답이 오래됐거나 형식이 달라지면 전송을 중단하고 실행을 실패로 표시합니다. 제공자의 Retry-After를 따릅니다. 10분 주기여도 실제 전달 시간은 API 반영과 GitHub 시작 지연에 따라 달라집니다.

## 검증

`codex-reset-discord` 폴더에서 실행합니다.

```powershell
python -m unittest discover -v
python -m py_compile monitor.py test_monitor.py test_updates.py
python monitor.py --demo
python monitor.py
```

기본 실행은 전송/상태 저장 없는 미리보기입니다. `--send`만 실제 게시하며, `--test-webhook`과 `--test-feed-webhook`은 각 채널에 가짜 연결 테스트 1건만 보냅니다. 오프라인 데이터와 데모는 실제 전송할 수 없습니다.

## 출처

- [무료 API와 데이터 사용 조건](https://codex-reset.com/developers)
- [Discord 웹훅](https://docs.discord.com/developers/resources/webhook)
- [X 공식 API 가격](https://docs.x.com/x-api/getting-started/pricing)

Data: [codex-reset.com](https://codex-reset.com/). GitHub 실행 시 저장소 URL을 포함한 User-Agent로 식별합니다.
