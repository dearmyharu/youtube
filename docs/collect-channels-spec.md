# 채널 수집기(`collect_channels.py`) 스펙

> [trending-collector-spec.md](./trending-collector-spec.md)의 설계 원칙(원본 보관, append-only,
> UTC 저장, `videos`/`video_stats` 테이블 공유)을 그대로 승계합니다. 여기에 원칙 하나를 추가합니다.

---

## 0. 원칙 (추가분)

6. **백필과 증분은 서로 다른 실행 모드다.** 한 스크립트에서 `--mode backfill` / `--mode incremental`로
   분기하되, 상태(어디까지 긁었는지)는 반드시 DB(`channel_collection_state`)에 남겨서 재실행 시
   이어받을 수 있게 한다.

---

## 1. 확정된 파라미터

| 항목 | 값 |
|---|---|
| 채널당 백필 개수 | 최근 500개 (또는 재생목록 끝, 먼저 도달하는 쪽) |
| 성장곡선 추적 기간 | 업로드 후 7일간 매일 |
| 채널 풀 규모 | core(비교 분석 핵심 채널, 소수) + panel(최대 1,000~2,000개) |
| 백필 우선순위 | `channel_pool.tier`: `core`를 무조건 먼저 전부 처리, 남는 쿼터로 `panel`을 FIFO(`first_seen_at`) |

---

## 2. API 호출 구조

채널 하나당 3종 호출을 조합합니다.

```
① channels.list       part=snippet,contentDetails,statistics   id=<최대50개 배치>
   → uploads 재생목록 ID + 구독자수/조회수/영상수 스냅샷

② playlistItems.list  part=contentDetails  playlistId=<uploads>  maxResults=50
   → 영상 ID 목록 (최신순, 단 premiere 등 예외로 100% 정렬 보장은 아님)

③ videos.list         part=snippet,contentDetails,statistics   id=<최대50개 배치>
   → 실제 통계/메타 (트렌딩 수집기와 동일 로직 재사용)
```

①③은 ID를 콤마로 최대 50개씩 배치할 수 있어 채널 수가 늘어도 비용이 선형으로 크게 늘지 않습니다.
②는 채널당 1콜이라 배치가 안 되고, 이게 스케일의 지배 비용입니다.

---

## 3. 백필 모드 (`--mode backfill`)

- 대상: `channel_pool.decision='include'` AND
  (`channel_collection_state` 없음 OR `backfill_status` IN (`NULL`, `pending`, `failed`))
- 정렬: `tier='core'` 우선 전부 처리 → 남는 일일 쿼터 예산으로 `tier='panel'`을 `first_seen_at` FIFO
- 채널별로 ②를 반복 호출하며 최대 500개 또는 재생목록 끝(다음 페이지 없음) 중 먼저 도달하는 쪽에서 멈춤
  → 채널당 최대 10페이지
- 모은 video_id를 ③으로 배치 조회 → `videos`, `video_stats` upsert/insert, `channel_stats`에 스냅샷 1행
- 완료 시 `channel_collection_state`에 `backfill_status='done'`, `backfill_completed_at`,
  `oldest_video_published_at` 기록
- 실패한 채널은 `backfill_status='failed'`로 남기고 다음 실행에서 재시도 대상에 포함(전체 배치 중단 금지)
- **일일 쿼터 예산 스케줄러:** 하루 쿼터를 ① 트렌딩 수집기 예약분 → ② 이미 백필 끝난 채널의 증분 추적
  예약분 → ③ 남는 쿼터로 backfill 대상 채널 처리, 순서로 배분(`quota.allocate_daily_budget`).
  1,000~2,000개 채널이면 며칠에 걸쳐 자연히 소화됩니다.

---

## 4. 증분 모드 (`--mode incremental`, 매일 1회)

두 가지 별개 작업이 한 실행 안에 들어갑니다.

**(a) 신규 영상 탐지**

②를 최신순으로 페이지 넘기다가, "이 페이지의 모든 video_id가 이미 `videos`에 있고 &
published_at이 안전 버퍼(마지막 수집시각 - 2일)보다 오래됐다" 조건이 되면 중단.
재생목록 정렬이 100% 보장되지 않으므로 **최대 페이지 캡(5페이지 = 250개)**을 안전장치로 둡니다.
캡에 걸리면 경고 로그만 남기고 계속 진행(전체 중단 금지).

**(b) 성장곡선 갱신 (업로드 후 7일간 매일)**

재생목록 호출 없이 SQL로 바로 대상 추출:

```sql
SELECT video_id FROM videos
WHERE channel_id IN (<include 채널>)
  AND published_at >= datetime('now', '-7 days')
```

이 결과를 ③으로 배치 조회 → `video_stats`에 append. `run_id`로 배치가 구분되므로 같은 영상이
하루 여러 번 찍혀도 문제없습니다(PK가 `video_id, collected_at`).

(a)+(b)에서 나온 video_id는 합쳐서 ③ 배치를 최소 호출 수로 묶습니다(중복 조회 방지).

채널 레벨 스냅샷(구독자/조회수/영상수)도 매일 ①로 갱신 → `channel_stats`.

---

## 5. DB 스키마 추가분

트렌딩 수집기와 공유: `videos`, `video_stats`, `video_meta_history`, `runs`.

```sql
-- 채널 풀에 tier 추가 (트렌딩 수집기 스펙의 channel_pool을 확장)
ALTER TABLE channel_pool ADD COLUMN tier TEXT DEFAULT 'panel'; -- core | panel

-- 채널 레벨 스냅샷 (append-only, video_stats와 대칭)
CREATE TABLE channel_stats (
  channel_id       TEXT NOT NULL,
  collected_at     TEXT NOT NULL,
  run_id           TEXT NOT NULL,
  subscriber_count INTEGER,
  view_count       INTEGER,
  video_count      INTEGER,
  PRIMARY KEY (channel_id, collected_at)
);

-- 채널별 수집 진행 상태 (관측 기록이 아니라 북키핑 → 유일하게 UPDATE 허용되는 테이블)
CREATE TABLE channel_collection_state (
  channel_id                 TEXT PRIMARY KEY,
  backfill_status             TEXT,     -- pending | done | failed
  backfill_completed_at       TEXT,
  oldest_video_published_at   TEXT,
  last_incremental_at         TEXT
);
```

`runs.job_name`에 `'channel_backfill'` / `'channel_incremental'` 두 값을 추가로 씁니다.

---

## 6. 쿼터 예산 (1,000~2,000개 채널 기준)

| 호출 | 배치 가능? | 매일 비용 (2,000채널 기준) |
| --- | --- | --- |
| `channels.list` (스냅샷) | O (50개씩) | ~40 units |
| `playlistItems.list` (신규 영상 탐지, 채널당 1콜) | X | ~2,000 units |
| `videos.list` (신규 + 7일 성장창 배치) | O (50개씩) | 대상 수 / 50 |

→ 매일 반복하는 증분 추적은 2,000개 채널이어도 하루 쿼터 10,000의 20~30% 수준. **지배 비용은
`playlistItems.list` 하나뿐**입니다.

백필(1회성)은 채널당 약 20 units(playlistItems 10 + videos.list 10, 500개 기준) → 채널 수만큼
곱해지므로 한 번에 몰아서 하지 않고 일일 예산 스케줄러로 며칠에 나눠 처리합니다.

`quota.py`에 아래 함수로 반영:

```python
def estimate_channel_incremental_quota(n_channels, avg_growth_window_videos, unit_cost) -> int: ...
def estimate_channel_backfill_quota(n_pending, per_channel_pages, unit_cost) -> int: ...
def allocate_daily_budget(daily_cap, trending_reserved, incremental_reserved) -> int: ...
```

---

## 7. 실패 처리

트렌딩 스펙 5번과 동일 원칙 + 추가: **백필 중 실패한 채널은 `backfill_status='failed'`로 남기고
다음날 재시도 대상에 포함**(전체 배치 중단 금지).

---

## 8. 테스트

- 재생목록 페이지네이션 종료조건 (안전 버퍼, 최대 페이지 캡)
- 7일 성장창 대상 선정 쿼리 (경계값: 정확히 7일째)
- 백필 vs 증분 모드 분기
- `channel_stats` append 멱등성
- 채널용 쿼터 추정 함수
- `tier` 우선순위 정렬(core 먼저, panel은 FIFO)

---

## 9. 파일 구조 (트렌딩 스펙에 추가)

```
media-insight/
├── docs/
│   ├── trending-collector-spec.md
│   └── collect-channels-spec.md
├── config/settings.toml                # 쿼터 예산, 백필/증분 파라미터 공유
├── src/
│   ├── parsers.py
│   ├── quota.py
│   ├── db.py
│   ├── youtube_client.py
│   ├── collect_trending.py
│   └── collect_channels.py             # --mode backfill|incremental
├── data/raw/channel/YYYY/MM/DD/{run_id}_{channel_id}_{page}.json.gz
└── tests/
```
