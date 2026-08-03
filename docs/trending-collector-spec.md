# 인기급상승(Trending) 수집기 스펙

> 목적: ① 시계열 데이터 확보 시작(소급 불가) ② 분석 대상 채널 후보 풀 자동 축적
> 이 문서는 클로드 코드에 그대로 넘기는 작업 지시서입니다.

---

## 0. 설계 원칙 (이걸 어기면 나중에 데이터를 버리게 됩니다)

1. **원본 응답을 반드시 보관한다.** API 응답 JSON을 그대로 파일로 저장한 뒤, DB는 거기서 파생시킵니다. 스키마 설계가 틀렸을 때 재구축이 가능해집니다.
2. **관측 기록은 절대 UPDATE 하지 않는다.** 통계 스냅샷은 append-only.
3. **시각은 DB에 UTC로 저장하고, KST 변환은 분석 단계에서 한다.** `publishedAt`은 UTC입니다. "업로드 시간대별 성과" 분석에서 이걸 놓치면 결론이 9시간 밀립니다.
4. **누적 카운터를 저장하지 않고 관측 테이블에서 계산한다.** (하루 3회 실행 시 카운터가 3배로 부풀는 버그 방지)
5. **채널 수집기와 테이블을 공유한다.** 트렌딩 수집기와 채널 수집기가 각자 다른 테이블에 쌓으면 두 배 일이 됩니다.

---

## 1. 수집 대상 및 호출 구조

### 전체 인기급상승
```
GET videos.list
  part=snippet,contentDetails,statistics
  chart=mostPopular
  regionCode=KR
  maxResults=50
  (nextPageToken으로 페이지네이션)
```

### 카테고리별 인기급상승
위 호출에 `videoCategoryId`를 추가합니다. 카테고리 목록은 하드코딩하지 말고
`videoCategories.list?regionCode=KR`로 받아서 캐싱하세요.

주요 카테고리(참고용, 실제 값은 위 호출로 확인):

| ID | 카테고리 |
|---|---|
| 10 | 음악 |
| 17 | 스포츠 |
| 20 | 게임 |
| 22 | 인물/블로그 |
| 23 | 코미디 |
| 24 | 엔터테인먼트 |
| 25 | 뉴스/정치 |
| 26 | 노하우/스타일 |

**주의:** 모든 카테고리가 mostPopular 차트를 지원하지 않습니다. 빈 응답이나 400 에러가
정상적으로 발생하므로, 실패한 카테고리는 로그만 남기고 다음으로 넘어가야 합니다.
전체 작업을 중단시키면 안 됩니다.

---

## 2. 실행 주기

- **최소 1일 1회.** 이것만으로도 발견 목적은 충족됩니다.
- **권장 1일 3회** (KST 09시 / 15시 / 21시). 순위 변동과 체류 시간까지 분석 가능해집니다.
- GitHub Actions cron은 **UTC 기준**이고, 지정 시각보다 수십 분 지연되는 일이 흔합니다.
  → 정확한 시각에 의존하는 로직을 만들지 말고, **실제 실행 시각을 `collected_at`에 기록**하세요.

---

## 3. 스토리지 레이아웃

### 원본 아카이브 (source of truth)
```
data/raw/trending/YYYY/MM/DD/{run_id}_{category_id|all}_{page}.json.gz
```

### DB 스키마

```sql
-- 실행 이력
CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,   -- ULID 또는 UUID
  job_name      TEXT NOT NULL,      -- 'trending'
  started_at    TEXT NOT NULL,      -- UTC ISO8601
  finished_at   TEXT,
  status        TEXT,               -- running | success | partial | failed
  quota_used    INTEGER,
  items_seen    INTEGER,
  error_message TEXT
);

-- 영상 고정 정보 (변하지 않는 값) — 채널 수집기와 공유
CREATE TABLE videos (
  video_id       TEXT PRIMARY KEY,
  channel_id     TEXT NOT NULL,
  title          TEXT,
  published_at   TEXT,             -- UTC
  duration_sec   INTEGER,
  category_id    TEXT,
  tags           TEXT,             -- JSON 배열 문자열
  thumbnail_url  TEXT,
  first_seen_at  TEXT NOT NULL,
  last_meta_at   TEXT              -- 제목 변경 추적용
);

-- 제목/썸네일 변경 이력 (선택이지만 강력한 분석 소재)
CREATE TABLE video_meta_history (
  video_id     TEXT NOT NULL,
  observed_at  TEXT NOT NULL,
  title        TEXT,
  thumbnail_url TEXT,
  PRIMARY KEY (video_id, observed_at)
);

-- 통계 관측 (append-only) — 채널 수집기와 공유
CREATE TABLE video_stats (
  video_id     TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  view_count   INTEGER,
  like_count   INTEGER,
  comment_count INTEGER,
  PRIMARY KEY (video_id, collected_at)
);

-- 트렌딩 순위 관측 (append-only)
CREATE TABLE trending_rank (
  collected_at TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  region       TEXT NOT NULL DEFAULT 'KR',
  category_id  TEXT NOT NULL DEFAULT 'all',
  rank         INTEGER NOT NULL,
  video_id     TEXT NOT NULL,
  PRIMARY KEY (collected_at, region, category_id, rank)
);

-- 채널 후보 풀
CREATE TABLE channel_pool (
  channel_id      TEXT PRIMARY KEY,
  channel_title   TEXT,
  first_seen_at   TEXT NOT NULL,
  source          TEXT,            -- trending | ranking_site | manual
  screened_at     TEXT,
  passed_filter   INTEGER,         -- 0/1/NULL
  group_type      TEXT,            -- 제작사 | 개인 | 플랫폼공식 | 브랜드 (수동 입력)
  decision        TEXT,            -- include | exclude | pending
  decision_reason TEXT
);
```

**`times_in_trending`은 컬럼으로 두지 말고 뷰로 계산:**

```sql
CREATE VIEW channel_trending_days AS
SELECT v.channel_id,
       COUNT(DISTINCT substr(t.collected_at, 1, 10)) AS days_in_trending,
       COUNT(DISTINCT t.video_id)                    AS videos_in_trending,
       MIN(t.collected_at)                           AS first_trending_at
FROM trending_rank t
JOIN videos v ON v.video_id = t.video_id
GROUP BY v.channel_id;
```

---

## 4. 쇼츠 플래그

`contentDetails.duration`(ISO8601, 예: `PT3M42S`)을 초로 파싱해 `duration_sec`에 저장합니다.

```
is_short_candidate = duration_sec <= 180
```

- 단정하지 말고 `_candidate`로 명명하세요. API로 세로 비율을 알 수 없어 길이 기반 추정입니다.
- **분석 시 롱폼/쇼츠를 반드시 분리하세요.** 섞으면 조회수 분포가 뒤섞여 모든 비교가 무의미해집니다.

---

## 5. 멱등성 · 실패 처리

- **재실행 안전성:** `video_stats` PK가 `(video_id, collected_at)`이므로 같은 run 내 중복은 자동 방지. `videos`는 UPSERT하되 `first_seen_at`은 덮어쓰지 않을 것.
- **부분 실패 허용:** 카테고리 하나가 실패해도 나머지는 계속 수집. 최종 상태를 `partial`로 기록.
- **조용한 실패 금지:** 수집 0건이면 exit code를 0이 아닌 값으로 반환해 Actions가 빨간불이 되게 하세요. 매일 무인 실행되는 코드에서 가장 위험한 건 아무 일도 안 일어난 걸 모르는 것입니다.
- **재시도:** 5xx / rate limit은 지수 백오프로 3회. 4xx는 즉시 로그 후 스킵.
- **쿼터 소진(403 quotaExceeded):** 즉시 중단하고 `runs.status='failed'`, 다음 실행에서 재개.

---

## 6. 쿼터

`videos.list`는 호출당 비용이 낮은 편이라, 트렌딩 수집은 하루 수십 유닛 수준으로 끝납니다.
일일 기본 할당량(10,000) 대비 여유가 크므로 **남는 쿼터를 채널 추적에 쓰는 게 맞습니다.**

단, 정확한 유닛 값은 반드시 공식 문서로 확인하고 `quota.py`에 계산 함수로 박아두세요.

```python
def estimate_daily_quota(n_categories: int, runs_per_day: int,
                         avg_pages: int, unit_cost: int) -> int:
    ...
```

---

## 7. 프로젝트 구조

```
media-insight/
├── CLAUDE.md
├── config/
│   ├── channels.csv           # 확정 채널 (아직 비어 있어도 됨)
│   └── settings.toml
├── src/
│   ├── youtube_client.py      # 인증, 재시도, 쿼터 카운팅
│   ├── quota.py
│   ├── collect_trending.py    # 이 문서의 대상
│   ├── collect_channels.py    # 다음 단계
│   ├── parsers.py             # ISO8601 duration, 타임존 변환
│   └── db.py                  # 스키마 마이그레이션, UPSERT
├── data/
│   ├── raw/
│   └── media.db
├── tests/
│   ├── test_parsers.py        # duration 파싱, KST 변환
│   └── test_quota.py
└── .github/workflows/collect.yml
```

---

## 8. 테스트 (반드시 요청할 것)

무인 실행 코드이므로 아래는 테스트가 있어야 합니다.

- `PT1H2M3S`, `PT45S`, `PT10M` 등 duration 파싱 엣지 케이스
- UTC → KST 변환 (자정 넘김 케이스 포함)
- 같은 run 재실행 시 중복 행이 생기지 않는지
- 빈 응답 / 카테고리 미지원 응답 처리
- 쿼터 계산 함수

---

## 9. GitHub Actions

- API 키는 `secrets.YOUTUBE_API_KEY`
- cron은 UTC로 작성 (KST 09시 = UTC 00시)
- `concurrency` 그룹으로 중복 실행 방지
- 산출물 반영 방식 택 1:
  - **A.** SQLite를 레포에 커밋 — 간단하지만 바이너리 diff로 레포가 비대해짐
  - **B.** Postgres 무료 티어(Neon/Supabase)에 적재 — 권장
  - 원본 JSONL은 어느 쪽이든 별도 보관(레포 커밋 또는 오브젝트 스토리지)

---

## 10. 착수 전 확인 목록

공식 문서에서 직접 확인하고 이 문서를 갱신하세요. 제 기억이 아니라 문서가 기준입니다.

- [ ] `videos.list` 쿼터 유닛 비용
- [ ] mostPopular 차트가 반환하는 최대 항목 수 및 페이지네이션 동작
- [ ] `regionCode=KR`에서 mostPopular를 지원하는 카테고리 ID 목록
- [ ] 일일 쿼터 할당량 (프로젝트별로 다를 수 있음)
- [ ] 쇼츠 최대 길이 기준 현행값

---

## 11. 완료 정의 (Definition of Done)

1. 로컬에서 `python -m src.collect_trending` 1회 실행 성공
2. `data/raw/`에 gzip 원본 생성 확인
3. `trending_rank`, `videos`, `video_stats`, `channel_pool`에 행 적재 확인
4. 동일 명령 재실행 시 중복 행 0건
5. GitHub Actions에서 스케줄 실행 성공(수동 dispatch로 먼저 검증)
6. 이틀치 데이터로 `channel_trending_days` 뷰 조회 성공

6번까지 되면 후보 풀이 자동으로 쌓이기 시작합니다. 그 시점부터 채널 목록 확정 작업으로 넘어갑니다.
