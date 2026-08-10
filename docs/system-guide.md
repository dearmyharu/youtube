# media-insight 시스템 가이드

이 문서는 두 부분입니다. **1부**는 지금 돌아가고 있는 걸 어디서 확인하는지 실전 가이드,
**2부**는 코드가 왜 이렇게 짜여 있는지 공부용 설명입니다. 코드 자체는 이 문서가 아니라
`src/` 안의 실제 파일이 원본이니, 문서와 코드가 다르면 코드가 맞습니다.

---

## 1부. 어디서 뭘 보는지

### 전체 그림

```
YouTube Data API v3
      │
      ▼
GitHub Actions (매일 자동 실행, 노트북 꺼도 됨)
  ├─ trending      하루 3회 — 인기급상승 수집 + 새 채널 발견
  ├─ channel-backfill  하루 4회 — 채널별 과거 영상 최대 500개
  └─ channel-incremental 하루 1회 — 신규 영상 감지 + 7일 성장추이 + 썸네일
      │
      ├──────────────► Supabase Postgres (표/숫자 데이터)
      └──────────────► Supabase Storage  (썸네일 이미지, 새 영상만)
```

로컬 컴퓨터는 코드를 push하는 용도로만 쓰이고, 실제 수집은 전부 GitHub 서버에서 돕니다.

### GitHub — 코드와 자동화

- **저장소**: https://github.com/dearmyharu/youtube
- **Actions 탭**: 지금까지 실행된 모든 작업 목록. 초록 체크 = 성공, 빨간 X = 실패.
  클릭하면 단계별 로그(파이썬 에러 메시지까지) 볼 수 있음.
  - 수동으로 지금 당장 돌리고 싶으면: Actions → 왼쪽 `collect` → **Run workflow**
- **Settings → Secrets and variables → Actions**: API 키 저장소. 여기 등록된 값:
  - `YOUTUBE_API_KEY` — YouTube Data API 키
  - `DATABASE_URL` — Supabase Postgres 접속 문자열
  - `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` — 썸네일 업로드용

### Supabase — 실제 데이터가 쌓이는 곳

프로젝트: `ebesyvgtodrprbcpotyg` (supabase.com 로그인 후 접속)

**Table Editor** (왼쪽 메뉴) — 엑셀처럼 표로 보는 화면. 테이블 8개 + 뷰 2개:

| 테이블/뷰 | 뭐가 들어있나 | 지금 행 수 |
|---|---|---|
| `videos` | 영상 메타데이터(제목, 채널, 길이, 업로드일, 썸네일 경로) | 314,326 |
| `video_stats` | 영상별 조회수/좋아요/댓글 — **관측할 때마다 새 행 추가**(덮어쓰지 않음) | 417,034 |
| `trending_rank` | 인기급상승 순위 기록 | 6,532 |
| `channel_pool` | 발견된 채널 전체 + 승인 상태(`decision`)와 우선순위(`tier`) | 1,493 (승인 1,428 / 제외 65) |
| `channel_stats` | 채널별 구독자/조회수 스냅샷 — 매일 쌓여서 성장 추이가 됨 | 4,076 |
| `channel_collection_state` | 채널별 백필 진행 상태(끝났는지, 실패했는지) | 백필 완료 860개 |
| `runs` | 모든 실행 기록(성공/실패, 쿼터 사용량) | 49건 |
| **뷰** `channel_trending_days` | 채널별 트렌딩 노출 횟수 계산 |
| **뷰** `thumbnail_export` | 채널명+제목+썸네일 링크를 한 표로 — **여기서 Export CSV 누르면 됨** |

**SQL Editor** — 직접 쿼리 짜서 보고 싶을 때. 예:
```sql
-- 채널별 최근 구독자 수 상위 20
SELECT cp.channel_title, cs.subscriber_count
FROM channel_stats cs
JOIN channel_pool cp ON cp.channel_id = cs.channel_id
ORDER BY cs.collected_at DESC, cs.subscriber_count DESC
LIMIT 20;
```

**Storage** — 실제 썸네일 이미지 파일(`thumbnails` 버킷). `videos.thumbnail_path`가
가리키는 파일이 여기 있음. 이미지 자체는 DB가 아니라 여기 저장됨.

**Project Settings → Database / API Keys** — 연결 문자열, 서비스 키 확인/재발급하는 곳.

---

## 2부. 코드는 어떻게 짜여 있나

### 설계 원칙 (모든 코드가 따르는 규칙)

1. **원본 API 응답을 항상 gzip으로 보관한다** (`data/raw/...`, GitHub Actions에서는
   아티팩트로 90일 보관). DB는 여기서 파생된 값.
2. **관측 테이블은 UPDATE 안 함, append만 한다.** `video_stats`, `trending_rank`,
   `channel_stats`가 그렇다 — 오늘 조회수를 어제 걸로 덮어쓰면 시계열이 사라지니까.
3. **DB엔 항상 UTC로 저장, KST 변환은 분석 단계에서만.** (`parsers.utc_to_kst`)
4. **누적 카운터를 저장하지 않고 관측 테이블에서 계산한다.**
5. **새로 발견된 채널은 자동 승인**(`decision='include'`), 단 뉴스/정치 카테고리는 자동 제외.
   사람이 나중에 언제든 `exclude`로 바꿀 수 있고, 그 결정은 다시 덮어써지지 않음.
6. **백필(과거 데이터 채우기)과 증분(매일 신규 추적)은 별개 모드**이며 진행 상태를
   DB(`channel_collection_state`)에 남겨서 중간에 끊겨도 이어서 할 수 있다.

### 파일 구조

```
src/
├── parsers.py            ISO8601 길이 파싱, UTC↔KST 변환
├── quota.py               쿼터 계산(하루 예산, 채널당 비용 추정)
├── db.py                  Postgres 스키마 + 모든 INSERT/UPDATE 함수
├── youtube_client.py      YouTube API 호출 (재시도, 배치, search)
├── storage_client.py      Supabase Storage 업로드 (썸네일 이미지)
├── collect_trending.py    인기급상승 수집 (하루 3회)
├── collect_channels.py    채널별 백필/증분 수집 (하루 4+1회)
├── discover_channels.py   검색 API로 크리에이터 채널 대량 발견 (일회성)
├── analyze_trends.py      제목 키워드 + 업로드 형식 트렌드 분석
└── analysis/
    ├── keywords.py         한국어 제목 토큰화, 트렌드 감지
    └── format_trends.py    길이/업로드시간/쇼츠비율 집계

tests/    각 모듈 대응 테스트 (80개, DB 관련 23개는 실제 Postgres 필요해서 평소엔 skip)
docs/     설계 스펙 문서 (trending/channel 수집기, 이 가이드)
config/   settings.toml(모든 파라미터), channels.csv
.github/workflows/collect.yml   스케줄 정의
```

### 모듈별 설명

**`parsers.py`** — 제일 작고 제일 자주 쓰이는 모듈. YouTube가 영상 길이를 `PT3M42S`
같은 ISO8601 형식으로 주는데 이걸 초 단위 정수로 바꾸는 `parse_iso8601_duration`,
그리고 UTC ↔ KST 변환(`utc_to_kst`). KST는 서머타임이 없어서 그냥 +9시간 고정 오프셋으로
처리 — `zoneinfo`/`tzdata` 같은 무거운 걸 안 써도 됨.

**`quota.py`** — YouTube API는 하루 10,000 유닛까지 무료인데, 호출마다 비용이 다름
(목록 조회는 1유닛, 검색은 100유닛). 여기 함수들이 "오늘 이만큼 써도 되나"를 계산함.
채널이 수백~수천 개로 늘어나면서 **하루 4번 도는 백필끼리 서로 얼마나 썼는지 알아야** 하는
문제가 생겼는데, 이건 `db.get_quota_used_today`(오늘 날짜 `runs` 테이블 합계 조회)로 해결.

**`db.py`** — 스키마 정의(`SCHEMA` 문자열)와 모든 DB 접근 함수. 원래 SQLite로 시작했다가
Supabase Postgres로 전면 전환하면서 다시 씀. 눈여겨볼 부분 두 개:
- `upsert_video`가 `RETURNING (xmax = 0) AS inserted`를 씀 — Postgres에만 있는 트릭으로,
  "이번에 진짜 새로 추가된 행인지 아니면 기존 행을 업데이트한 건지"를 한 번의 쿼리로 알아냄.
  이게 썸네일을 "새 영상만" 저장하게 만드는 핵심 장치.
- `get_backfill_queue`가 `tier`(core 우선) → `first_seen_at`(먼저 발견된 순) 순으로 정렬.

**`youtube_client.py`** — API 호출을 감싸는 얇은 래퍼. 5xx/레이트리밋은 지수 백오프로
재시도, 403 쿼터초과는 즉시 예외를 던져서 위에서 전체 작업을 중단시킴. `chunked()`로
영상/채널 ID를 50개씩 배치 처리(그래야 유닛 비용이 안 늘어남).

**`storage_client.py`** — Supabase Storage REST API를 직접 호출하는 최소 클라이언트
(SDK 없이 `requests`만 사용, `youtube_client.py`와 같은 스타일). `save_thumbnail`이
다운로드→업로드를 메모리에서 바로 처리해서 로컬 디스크를 안 거침.

**`collect_trending.py`** — `videos.list(chart=mostPopular)`를 전체 + 카테고리별로
돌면서 영상/통계/순위를 기록하고, 등장하는 채널을 `channel_pool`에 자동 등록. 뉴스/정치
카테고리(25번)는 영상은 그대로 수집하되 채널은 `exclude`로 등록.

**`collect_channels.py`** — 이 프로젝트에서 제일 복잡한 파일. 두 모드:
- `--mode backfill`: 채널당 업로드 재생목록을 최대 10페이지(500개) 넘기면서 과거 영상을
  긁음. 하루 목표 500채널을 4번 실행에 나눠 처리(`daily_channel_target / runs_per_day`).
  **썸네일은 절대 저장 안 함** — 거의 다 "우리 DB엔 처음 보는 영상"이라 여기서 저장하면
  기존 31만개 백로그까지 전부 받게 됨.
- `--mode incremental`: 재생목록 최신순으로 페이지를 넘기다 "이미 아는 영상만 남았고 +
  2일 이상 지났다"는 조건이 되면 멈춤(신규 영상 탐지). 여기에 최근 7일 내 영상들의
  조회수도 같이 갱신. **썸네일은 여기서만 저장** — `upsert_video`가 "새 영상"이라고
  확인해준 것만.

**`discover_channels.py`** — 트렌딩만으로는 크리에이터 채널이 너무 천천히 모여서,
`search.list(order=viewCount)`로 장르 키워드 30개("브이로그", "먹방", "게임" 등)를
조회수순으로 검색해 한 번에 800개 넘는 채널을 확보한 일회성 스크립트. 검색은 호출당
100유닛이라 비싸지만, 30번 돌려도 3,000유닛 정도라 하루 예산 안에서 충분함.

**`analysis/keywords.py` + `format_trends.py`, `analyze_trends.py`** — 제목/업로드
패턴 트렌드 분석. 한국어 형태소 분석기(KoNLPy 등)는 Java가 필요해서, Java 없이 돌아가는
`soynlp`(통계 기반 단어 추출)를 씀. 스톱워드 제거, "ㅋㅋㅋ" 같은 반응 표현/달력 라벨
필터링, **채널마다 반복되는 고정 문구(채널명 등) 자동 감지 후 제거**, 그리고 "지난 4개월
평균 대비 이번 달"처럼 베이스라인과 비교하는 트렌드 판정까지 포함.

---

## 지금 상태 스냅샷 (2026-08-10 기준)

- 영상 314,326개, 채널 1,428개 추적 중(뉴스 65개 제외)
- 백필 완료 860개 채널, 나머지는 자동으로 이어서 진행 중
- 썸네일은 새로 발견되는 영상부터 저장 시작(현재 46개)

## 알아두면 좋은 것 / 다음에 볼 것

- `channel_backfill` 실행 3건이 `status='running'`으로 멈춰있음 — 정상 종료 안 된
  케이스로 보이며 원인 파악 필요(다음 대화에서 짚기로 함).
- `config/channels.csv`는 아직 안 쓰이고 있음(수동 채널 리스트용으로 만들어뒀던 것).
