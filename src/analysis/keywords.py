"""Title keyword trend analysis (소재 트렌드).

Pipeline: soynlp (unsupervised, no Java/dictionary dependency) learns word
boundaries from the title corpus -> tokenize each title -> drop stopwords/short
tokens -> drop each channel's own recurring fixed phrases (channel name, series
branding that would otherwise dominate every month) -> aggregate monthly
frequency -> compare each month against a trailing baseline (never just "up").

The soynlp-dependent functions (train_word_extractor/build_tokenizer) need a
real corpus and are exercised via src/analyze_trends.py against Supabase; the
rest of this module is pure data-in/data-out so it can be unit tested without
training a model each time.
"""
import re
from collections import Counter, defaultdict

# particles/function words/generic filler that survive tokenization but carry
# no topical meaning — not exhaustive, extend as junk keywords show up in practice.
# Note: genre words like 브이로그/먹방 stay OUT of this list on purpose — for a
# topic-trend report those are exactly the signal we want, not noise.
STOPWORDS = {
    "은", "는", "이", "가", "을", "를", "에", "에서", "에게", "께", "와", "과",
    "도", "만", "의", "로", "으로", "다", "고", "며", "하다", "합니다", "입니다",
    "그리고", "그런데", "근데", "진짜", "너무", "정말", "완전", "그냥", "오늘",
    "이번", "저희", "우리", "나의", "제가", "이거", "저거", "그거", "영상",
    "채널", "구독", "좋아요", "댓글", "여러분", "안녕하세요",
    "하는", "했다", "합니다", "이야기", "모습",
}

_URL_RE = re.compile(r"https?://\S+")
_SERIES_NOTATION_RE = re.compile(r"(EP\.?\s?\d+|E\d{1,3}\b|\d+화|\d+편|#\d+)", re.IGNORECASE)
# bare reaction jamo (ㅋㅋㅋ, ㄷㄷ, ㅠㅠ, ㅎㅎ...) — laughter/shock, not a topic word
_REACTION_RE = re.compile(r"^[ㄱ-ㅎㅏ-ㅣ]+$")
# calendar labels (4월, 25일, 2026년) — always co-occurs with "now", not a trend signal
_CALENDAR_RE = re.compile(r"^\d+(월|일|년|시|분)$")


def clean_title(title: str) -> str:
    if not title:
        return ""
    title = _URL_RE.sub(" ", title)
    return re.sub(r"\s+", " ", title).strip()


def is_series_notation(title: str) -> bool:
    return bool(_SERIES_NOTATION_RE.search(title or ""))


def train_word_extractor(titles: list):
    """Real soynlp training — needs a sizeable corpus (hundreds+ of titles)."""
    from soynlp.word import WordExtractor

    extractor = WordExtractor()
    extractor.train([clean_title(t) for t in titles if t])
    return extractor.extract()


def build_tokenizer(word_scores):
    from soynlp.tokenizer import LTokenizer

    cohesion_scores = {word: score.cohesion_forward for word, score in word_scores.items()}
    return LTokenizer(scores=cohesion_scores)


def filter_tokens(tokens: list, min_len: int = 2) -> list:
    """Pure filtering step: drop stopwords, short tokens, pure-numeric tokens,
    bare reaction jamo (ㅋㅋㅋ/ㄷㄷ/ㅠㅠ), and calendar labels (4월/25일/2026년)."""
    out = []
    for tok in tokens:
        tok = tok.strip()
        if len(tok) < min_len:
            continue
        if tok in STOPWORDS:
            continue
        if tok.isdigit():
            continue
        if _REACTION_RE.match(tok):
            continue
        if _CALENDAR_RE.match(tok):
            continue
        out.append(tok)
    return out


def tokenize_title(tokenizer, title: str, min_len: int = 2) -> list:
    raw = tokenizer.tokenize(clean_title(title))
    return filter_tokens(raw, min_len=min_len)


def find_channel_fixed_phrases(channel_tokens: dict, min_videos: int = 10, min_ratio: float = 0.5) -> dict:
    """channel_tokens: {channel_id: [tokens_of_video_1, tokens_of_video_2, ...]}.

    A token that shows up in >= min_ratio of a channel's own videos (channel name,
    recurring series branding) is treated as that channel's fixed phrase and should
    be stripped before counting toward global trend keywords.
    """
    fixed = {}
    for channel_id, per_video_tokens in channel_tokens.items():
        n_videos = len(per_video_tokens)
        if n_videos < min_videos:
            continue
        doc_freq = Counter()
        for tokens in per_video_tokens:
            for tok in set(tokens):
                doc_freq[tok] += 1
        fixed[channel_id] = {tok for tok, n in doc_freq.items() if n / n_videos >= min_ratio}
    return fixed


def strip_channel_phrases(tokens: list, fixed_phrases: set) -> list:
    if not fixed_phrases:
        return tokens
    return [t for t in tokens if t not in fixed_phrases]


def monthly_keyword_frequency(records: list) -> dict:
    """records: [(year_month, tokens), ...] — one entry per video, tokens already
    filtered/de-branded. Counts each keyword once per video (not per raw occurrence)
    so a title repeating a word doesn't inflate its own contribution.

    Returns {year_month: {keyword: video_count}}.
    """
    freq = defaultdict(Counter)
    for year_month, tokens in records:
        for tok in set(tokens):
            freq[year_month][tok] += 1
    return {ym: dict(counter) for ym, counter in freq.items()}


def top_keywords(monthly_freq: dict, year_month: str, n: int = 20, min_frequency: int = 20) -> list:
    counts = monthly_freq.get(year_month, {})
    qualifying = [(kw, c) for kw, c in counts.items() if c >= min_frequency]
    qualifying.sort(key=lambda pair: pair[1], reverse=True)
    return qualifying[:n]


def detect_trend(monthly_freq: dict, target_month: str, baseline_months: int = 4, min_frequency: int = 20) -> list:
    """Compare target_month's qualifying keywords against the trailing-baseline
    average (the `baseline_months` chronologically preceding months present in the
    data) instead of just reporting "went up" off two data points.

    Returns a list of dicts sorted by pct_change desc: keywords with no prior
    occurrence at all get pct_change=None and are surfaced as "new" rather than
    a fake infinite percentage.
    """
    all_months = sorted(monthly_freq.keys())
    if target_month not in all_months:
        return []
    prior_months = [m for m in all_months if m < target_month][-baseline_months:]

    target_counts = monthly_freq[target_month]
    results = []
    for keyword, current in target_counts.items():
        if current < min_frequency:
            continue
        prior_values = [monthly_freq[m].get(keyword, 0) for m in prior_months]
        baseline = sum(prior_values) / len(prior_values) if prior_values else 0
        pct_change = None if baseline == 0 else (current - baseline) / baseline
        results.append({
            "keyword": keyword,
            "month": target_month,
            "current": current,
            "baseline": baseline,
            "pct_change": pct_change,
            "is_new": baseline == 0,
        })
    results.sort(key=lambda r: (r["pct_change"] is None, r["pct_change"] or 0), reverse=True)
    return results
