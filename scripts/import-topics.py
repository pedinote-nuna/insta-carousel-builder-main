"""기존 카드뉴스 주제를 used-topics.json 에 일괄 등록.

templates/slides.*.json (실제 생성된 주제 템플릿) 을 모두 스캔해서
data/used-topics.json 에 slug·title·keywords·date·category 로 등록한다.
title/category/date 는 템플릿 + data/topics.json + 파일 시각에서 견고하게 수집,
keywords 는 Claude 1회 배치 호출로 추출(없거나 실패하면 제목 토큰 fallback).

사용법:
    python3 scripts/import-topics.py
    python3 scripts/import-topics.py --no-claude   # 키워드 추출에 Claude 안 씀(빠름)
    python3 scripts/import-topics.py --force        # 이미 등록된 항목도 다시 갱신
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
DATA_DIR = REPO_ROOT / "data"
TOPICS_JSON = DATA_DIR / "topics.json"
USED_TOPICS_JSON = DATA_DIR / "used-topics.json"
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# example/샘플 템플릿은 실제 주제가 아니므로 제외
_SKIP_SLUGS = {"example", "example-paletteB"}

_KW_STOPWORDS = {
    "아이", "아기", "영아", "유아", "신생아", "소아", "우리",
    "방법", "증상", "주의", "관리", "예방", "케어", "가이드", "기준",
    "때", "후", "전", "및", "그리고", "대처", "정보", "팁",
}


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def topic_slugs() -> list[str]:
    """templates/slides.{slug}.json 에서 slug 목록 추출 (reels-extra·example 제외)."""
    slugs = []
    for p in sorted(TEMPLATES_DIR.glob("slides.*.json")):
        name = p.name
        if name.endswith(".reels-extra.json"):
            continue
        slug = name[len("slides."):-len(".json")]
        if slug in _SKIP_SLUGS:
            continue
        slugs.append(slug)
    return slugs


def gather_meta(slug: str, topics_data: dict) -> tuple[str, str, str]:
    """(title, category, date) 수집. 템플릿 → topics.json → 파일시각 fallback."""
    title = ""
    category = ""
    date_str = ""

    tpl = TEMPLATES_DIR / f"slides.{slug}.json"
    d = load_json(tpl) if tpl.exists() else None
    if isinstance(d, dict):
        title = d.get("topic_kr") or ""

    for area in ("this_week", "pending", "done"):
        for item in topics_data.get(area, []):
            if item.get("slug") == slug:
                title = title or item.get("title_kr", "")
                category = category or item.get("category", "")
                date_str = date_str or item.get("done_at", "")

    # 날짜 fallback: 템플릿 파일 수정 시각
    if not date_str and tpl.exists():
        try:
            date_str = datetime.fromtimestamp(tpl.stat().st_mtime).strftime("%Y-%m-%d")
        except Exception:
            pass

    return (title or slug), (category or "미분류"), (date_str or date.today().isoformat())


def _normalize_kw(kw: str) -> str:
    return re.sub(r"\s+", "", str(kw)).strip().lower()


def fallback_keywords(title: str) -> list[str]:
    raw = re.split(r"[\s,·/()\[\]{}!?.~\"'-]+", str(title))
    out = []
    for tok in raw:
        tok = tok.strip()
        if len(tok) >= 2 and _normalize_kw(tok) not in _KW_STOPWORDS:
            out.append(tok)
    return out[:6]


def batch_keywords(titles: list[str], api_key: str | None) -> list[list[str]]:
    """모든 제목의 키워드를 1회 Claude 호출로 추출. 실패 시 제목별 fallback."""
    if not api_key:
        return [fallback_keywords(t) for t in titles]
    try:
        from anthropic import Anthropic
    except ImportError:
        print("[WARN] anthropic 미설치 → fallback 키워드 사용 (pip install anthropic)")
        return [fallback_keywords(t) for t in titles]

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=(
                "너는 소아과 카드뉴스 주제 제목들의 키워드 추출기야. "
                "번호가 매겨진 제목 목록을 받으면, 각 제목마다 핵심 키워드 3~5개를 뽑아 "
                "JSON 객체로만 답해. 형식: {\"0\": [\"키워드\",...], \"1\": [...], ...}. "
                "조사·일반어(아이·방법·증상 등)는 제외하고 명사 위주로. 다른 텍스트 금지."
            ),
            messages=[{"role": "user", "content": numbered}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "text", None)
        ).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        mapping = json.loads(raw)
        result = []
        for i in range(len(titles)):
            kws = mapping.get(str(i))
            if isinstance(kws, list) and kws:
                result.append([str(k).strip() for k in kws if str(k).strip()][:6])
            else:
                result.append(fallback_keywords(titles[i]))
        return result
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Claude 배치 키워드 추출 실패({e}) → fallback 사용")
        return [fallback_keywords(t) for t in titles]


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="기존 주제 → used-topics.json 일괄 등록")
    ap.add_argument("--no-claude", action="store_true", help="키워드 추출에 Claude 미사용")
    ap.add_argument("--force", action="store_true", help="이미 등록된 항목도 갱신")
    args = ap.parse_args()

    topics_data = load_json(TOPICS_JSON) or {}
    used = load_json(USED_TOPICS_JSON) or {}
    used.setdefault("topics", [])
    existing = {t.get("slug") for t in used["topics"]}

    slugs = topic_slugs()
    if not slugs:
        print("[INFO] templates/slides.*.json 주제가 없습니다.")
        return 0

    targets = [s for s in slugs if args.force or s not in existing]
    skipped = len(slugs) - len(targets)
    print(f"[1/3] 주제 스캔: 총 {len(slugs)}개 (신규/갱신 대상 {len(targets)}개, 이미 등록 {skipped}개 건너뜀)")

    if not targets:
        print("[완료] 새로 등록할 주제가 없습니다.")
        return 0

    metas = {s: gather_meta(s, topics_data) for s in targets}
    titles = [metas[s][0] for s in targets]

    api_key = None if args.no_claude else os.getenv("ANTHROPIC_API_KEY")
    mode = "fallback(토큰)" if not api_key else "Claude 배치"
    print(f"[2/3] 키워드 추출 ({mode}) — {len(targets)}개 제목...")
    kw_lists = batch_keywords(titles, api_key)

    # used["topics"] 갱신 (slug 기준 dedupe)
    by_slug = {t.get("slug"): t for t in used["topics"]}
    added, updated = 0, 0
    for slug, kws in zip(targets, kw_lists):
        title, category, date_str = metas[slug]
        entry = {
            "slug": slug,
            "title": title,
            "keywords": kws,
            "date": date_str,
            "category": category,
        }
        if slug in by_slug:
            updated += 1
        else:
            added += 1
        by_slug[slug] = entry

    # 날짜순 정렬해서 저장
    merged = sorted(by_slug.values(), key=lambda t: t.get("date", ""))
    used["topics"] = merged

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USED_TOPICS_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(used, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(USED_TOPICS_JSON)

    print(f"[3/3] 저장 완료: {USED_TOPICS_JSON.relative_to(REPO_ROOT)}")
    print(f"  신규 {added}개 · 갱신 {updated}개 · 전체 {len(merged)}개")
    print()
    print("=== 등록된 주제 (최근 10개) ===")
    for t in merged[-10:]:
        kw = ", ".join(t.get("keywords", [])[:5])
        print(f"  {t['date']} | {t['category']} | {t['title']}  →  [{kw}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
