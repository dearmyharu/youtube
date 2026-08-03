# media-insight

YouTube 콘텐츠 데이터를 기반으로 채널 전략을 분석하고 성과를 예측하는 데이터 분석 플랫폼.
자세한 설계는 [docs/trending-collector-spec.md](docs/trending-collector-spec.md)와
[docs/collect-channels-spec.md](docs/collect-channels-spec.md) 참고.

## 설계 원칙 (변경 시 반드시 준수)

1. API 원본 응답은 `data/raw/`에 gzip으로 항상 보관한다. DB는 거기서 파생된 것.
2. 통계 관측 테이블(`video_stats`, `trending_rank`, `channel_stats`)은 append-only. UPDATE 금지.
3. 시각은 DB에 UTC로 저장. KST 변환은 분석 단계(`parsers.utc_to_kst`)에서만 한다.
4. 누적 카운터는 저장하지 않고 관측 테이블에서 계산한다.
5. `videos`/`video_stats`/`video_meta_history`/`runs` 테이블은 트렌딩 수집기와 채널 수집기가 공유한다.
6. 백필과 증분은 별도 실행 모드(`--mode backfill|incremental`)이며, 진행 상태는
   `channel_collection_state`에 남겨 재실행 시 이어받는다.

DB는 SQLite가 아니라 **Supabase Postgres**다 (`DATABASE_URL`). GitHub Actions 시크릿에
`YOUTUBE_API_KEY`, `DATABASE_URL` 둘 다 등록되어 있고, 워크플로도 이걸 그대로 사용한다.
원본 API 응답(`data/raw/`)은 러너 로컬이라 워크플로에서 매 실행마다 아티팩트로 업로드한다
(DB에는 파생 데이터만 들어감).

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # YOUTUBE_API_KEY, DATABASE_URL(Supabase) 채우기

python -m src.collect_trending
python -m src.collect_channels --mode backfill
python -m src.collect_channels --mode incremental
```

## 테스트

```bash
pytest
```

`tests/test_db.py`는 실제 Postgres가 필요해서 `TEST_DATABASE_URL`(없으면 `DATABASE_URL`)이
설정되어 있을 때만 돈다. 로컬에 Postgres가 없으면 자동으로 skip된다. **주의: 이 테이블들을
매 테스트 전에 TRUNCATE하므로 프로덕션 Supabase가 아니라 별도 테스트 DB/스키마를 가리킬 것.**

```bash
$env:TEST_DATABASE_URL = "postgresql://.../test_db"
pytest
```

## 미결 사항

- `config/channels.csv` / `channel_pool.tier`(core/panel) 채널 목록은 아직 비어 있음.
- 원본 JSONL 아티팩트는 90일 보관(GitHub Actions retention) — 장기 보관이 필요해지면
  오브젝트 스토리지로 옮길 것.
