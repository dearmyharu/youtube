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

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # YOUTUBE_API_KEY 채우기

python -m src.collect_trending
python -m src.collect_channels --mode backfill
python -m src.collect_channels --mode incremental
```

## 테스트

```bash
pytest
```

## 미결 사항

- GitHub Actions 산출물 반영 방식(SQLite 레포 커밋 vs Postgres/Supabase 적재)이 아직 확정되지 않음
  (docs/trending-collector-spec.md #9). Supabase 연동은 다음 단계.
- `config/channels.csv` / `channel_pool.tier`(core/panel) 채널 목록은 아직 비어 있음.
