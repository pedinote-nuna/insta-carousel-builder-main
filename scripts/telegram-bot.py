"""
소아과언니 카드뉴스 — 텔레그램 봇

운영자가 모바일에서 토픽 추천을 받고, 선택하고, 9장 카드뉴스를 생성·수신.

설치:
    pip install python-telegram-bot python-dotenv anthropic

환경변수 (.env):
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
    ANTHROPIC_API_KEY=...   # /topics 추천에 사용

실행:
    python scripts/telegram-bot.py

커맨드:
    /start          — 사용법 안내
    /topics         — Claude API 로 이번 주 주제 10개 추천
    /queue          — 보류 주제 목록
    /done           — 완료 주제 목록 (최근 10개)
    /usedtopics     — 사용한 주제 전체 이력 (used-topics.json)
    /new <slug>     — 카드뉴스 9장 생성
    /status         — 진행 상황

자유 입력 (커맨드 없이):
    "1 3 5"            — 추천 받은 후 번호 선택
    "다시"              — 추천 다시 (이전 추천 버림)
    "큐 1 3"            — pending 의 1, 3 번을 this_week 으로 이동
    "삭제 2"            — pending 의 2 번 삭제
    "예" / "아니오"     — /new 중복 경고에 답
    "<slug>"            — /new <slug> 와 동일
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as e:
    print(f"[ERROR] 의존성 누락: {e}")
    print("        pip install python-telegram-bot python-dotenv")
    sys.exit(1)

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent
NANO_GEN = REPO_ROOT / "scripts" / "nanobanana-gen.py"
TEMPLATES_DIR = REPO_ROOT / "templates"
OUTPUT_DIR = REPO_ROOT / "output"
DATA_DIR = REPO_ROOT / "data"
TOPICS_JSON = DATA_DIR / "topics.json"
USED_TOPICS_JSON = DATA_DIR / "used-topics.json"
# 톤 템플릿(디자인 DNA) 디렉터리 — find 결과: knowledge/tone-templates/{tone}.md
TONE_DIR = REPO_ROOT / "knowledge" / "tone-templates"
TONE_DIR_ALT = REPO_ROOT / "knowledge" / "tone"  # 대안 경로
SESSION_FILE = REPO_ROOT / "data" / "session.json"
TOPIC_SELECTION_MD = REPO_ROOT / "knowledge" / "topic-selection.md"

# 블로그 봇 연동 — generate-blog.js 위치
# 환경변수 BLOG_BOT_ROOT 로 덮어쓸 수 있음. 없으면 ../pediatric-blog-bot-main 추정.
BLOG_BOT_ROOT = Path(
    os.environ.get("BLOG_BOT_ROOT")
    or (REPO_ROOT.parent / "pediatric-blog-bot-main")
).resolve()
BLOG_GEN_JS = BLOG_BOT_ROOT / "generate-blog.js"
BLOG_GEN_TIMEOUT_SEC = 240

GEN_TIMEOUT_SEC = 300
SLIDE_COUNT = 9
DONE_PREVIEW = 10
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# === /batch (여러 주제 일괄 순차 처리) 설정 ===
# 단가·장수는 운영자가 조정 가능한 상수로 분리(하드코딩 금지).
BATCH_MAX_TOPICS = 10          # 한 배치 최대 주제 수 (초과분은 잘라냄)
BATCH_MIN_TOPIC_LEN = 5        # 주제 1개 최소 글자 수 (미만이면 그 줄 제외)
BATCH_IMAGES_PER_TOPIC = 14    # 주제당 생성 이미지 추정치(인스타 9 + 릴스/블로그 보강)
BATCH_COST_PER_TOPIC_USD = 1.88  # 주제당 예상 비용(USD) — 확인 게이트 안내용

APP_LINKS = """📲 소아과수첩 앱 다운로드
- Android: https://play.google.com/store/apps/details?id=com.pedinote.app
- iOS: https://apps.apple.com/kr/app/소아과수첩-해열제-성장기록-육아/id6758393052"""

BLOG_URL = "https://blog.naver.com/soagwa_unnie"
INSTA_URL = "https://www.instagram.com/dr.soa_unnie/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
# 봇 토큰이 URL 에 포함된 채로 INFO 로그에 찍히는 것 방지
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
log = logging.getLogger("soagwa-bot")


# ---------------------------------------------------------------- topics.json


def load_topics() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TOPICS_JSON.exists():
        save_topics({"this_week": [], "pending": [], "done": []})
    with TOPICS_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ("this_week", "pending", "done"):
        data.setdefault(key, [])
    return data


def save_topics(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOPICS_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(TOPICS_JSON)


def done_slugs() -> set[str]:
    return {item["slug"] for item in load_topics().get("done", [])}


def add_done(slug: str, title_kr: str = "") -> None:
    data = load_topics()
    if slug not in {item["slug"] for item in data["done"]}:
        data["done"].append({
            "slug": slug,
            "title_kr": title_kr or slug,
            "done_at": date.today().isoformat(),
        })
    data["this_week"] = [t for t in data["this_week"] if t["slug"] != slug]
    save_topics(data)


def title_for_slug(slug: str) -> str:
    data = load_topics()
    for area in ("this_week", "pending", "done"):
        for item in data.get(area, []):
            if item.get("slug") == slug:
                return item.get("title_kr", slug)
    return slug


def _clean_topic(r: dict) -> dict:
    return {
        "slug": r.get("slug"),
        "title_kr": r.get("title_kr", ""),
        "category": r.get("category", ""),
        "palette": r.get("palette", ""),
    }


# ---------------------------------------------------------------- used-topics.json
# 사용한 주제 이력 관리 + 중복(유사) 주제 체크용 키워드 저장소.

# 키워드 비교 시 무시할 너무 흔한 단어 (오탐 방지)
_KW_STOPWORDS = {
    "아이", "아기", "영아", "유아", "신생아", "소아", "우리",
    "방법", "증상", "주의", "관리", "예방", "케어", "가이드", "기준",
    "때", "후", "전", "및", "그리고", "대처", "정보", "팁",
}


def load_used_topics() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not USED_TOPICS_JSON.exists():
        save_used_topics({"topics": []})
    try:
        with USED_TOPICS_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("topics", [])
    return data


def save_used_topics(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USED_TOPICS_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(USED_TOPICS_JSON)


def _normalize_kw(kw: str) -> str:
    return re.sub(r"\s+", "", str(kw)).strip().lower()


def record_used_topic(
    slug: str, title: str, keywords: list[str], category: str = "",
    date_str: str = "",
) -> None:
    """used-topics.json 에 주제 1건 추가(또는 갱신). slug 기준 중복 방지."""
    data = load_used_topics()
    entry = {
        "slug": slug,
        "title": title or slug,
        "keywords": [k for k in (keywords or []) if k],
        "date": date_str or date.today().isoformat(),
        "category": category or "미분류",
    }
    topics = data["topics"]
    for i, t in enumerate(topics):
        if t.get("slug") == slug:
            topics[i] = entry  # 기존 항목 갱신
            break
    else:
        topics.append(entry)
    save_used_topics(data)


def gather_topic_meta(slug: str) -> tuple[str, str]:
    """slug 로 (title, category) 수집. 템플릿 → topics.json → slug 순 fallback."""
    title = ""
    category = ""
    tpl = template_path(slug)
    if tpl.exists():
        try:
            d = json.loads(tpl.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                title = d.get("topic_kr") or title
        except Exception:  # noqa: BLE001
            pass
    data = load_topics()
    for area in ("this_week", "pending", "done"):
        for item in data.get(area, []):
            if item.get("slug") == slug:
                title = title or item.get("title_kr", "")
                category = category or item.get("category", "")
    return (title or slug), (category or "미분류")


def _fallback_keywords(title: str) -> list[str]:
    """Claude 미사용 시 제목에서 길이 2+ 토큰을 키워드로 추출(간이)."""
    raw = re.split(r"[\s,·/()\[\]{}!?.~\"'-]+", str(title))
    out: list[str] = []
    for tok in raw:
        tok = tok.strip()
        if len(tok) >= 2 and _normalize_kw(tok) not in _KW_STOPWORDS:
            out.append(tok)
    return out[:6]


def _extract_keywords_sync(title: str, api_key: str) -> list[str]:
    """Claude 로 제목에서 핵심 키워드 3~5개 추출. 실패 시 fallback."""
    if not api_key or Anthropic is None:
        return _fallback_keywords(title)
    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=120,
            system=(
                "너는 소아과 카드뉴스 주제의 핵심 키워드 추출기야. "
                "주어진 한국어 제목에서 검색·중복판단에 쓸 핵심 키워드 3~5개를 뽑아 "
                'JSON 배열로만 답해. 예: ["코피","응급처치","지혈"]. '
                "조사·일반어(아이·방법·증상 등)는 제외하고 명사 위주로."
            ),
            messages=[{"role": "user", "content": str(title)}],
        )
        raw = _collect_text(resp).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        kws = json.loads(raw)
        if isinstance(kws, list) and kws:
            return [str(k).strip() for k in kws if str(k).strip()][:6]
    except Exception:  # noqa: BLE001
        log.exception("키워드 추출 실패 — fallback 사용")
    return _fallback_keywords(title)


async def extract_keywords(title: str) -> list[str]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    return await asyncio.to_thread(_extract_keywords_sync, title, api_key)


def similar_used_topics(keywords: list[str], limit: int = 5) -> list[dict]:
    """입력 키워드와 겹치는 used-topics 항목을 겹침수 내림차순으로 반환."""
    kset = {_normalize_kw(k) for k in keywords} - {_normalize_kw(s) for s in _KW_STOPWORDS}
    kset = {k for k in kset if k}
    if not kset:
        return []
    scored: list[tuple[int, str, dict]] = []
    for t in load_used_topics().get("topics", []):
        tset = {_normalize_kw(k) for k in t.get("keywords", [])}
        overlap = kset & tset
        if overlap:
            scored.append((len(overlap), t.get("date", ""), t))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [t for _, _, t in scored[:limit]]


async def record_used_topic_on_done(slug: str) -> None:
    """카드뉴스 생성 완료 직후 호출 — used-topics.json 에 자동 등록.
    실패해도 파이프라인에 영향 없도록 예외를 흡수한다."""
    try:
        title, category = gather_topic_meta(slug)
        keywords = await extract_keywords(title)
        record_used_topic(slug, title, keywords, category=category)
        log.info("used-topics 기록: %s (%s)", slug, category)
    except Exception as e:  # noqa: BLE001
        log.exception("used-topics 기록 실패 (무시): %s", e)


# ---------------------------------------------------------------- session state


current_task: dict = {
    "topic": None,
    "started_at": None,
    "status": "idle",
    "error": None,
}
_task_lock = asyncio.Lock()

# === 사용자 취소·중단 인프라 ===
# 운영자가 '중단'/'취소' 메시지를 보내면 _cancel_flag=True 가 되어
# 진행 중인 파이프라인이 다음 체크포인트(_check_cancel)에서 abort 한다.
# 이미지 생성 서브프로세스가 돌고 있으면 _current_proc 을 kill 해서 즉시 종료한다.
_cancel_flag: bool = False
_current_proc = None  # asyncio.subprocess.Process | None


def reset_cancel_flag() -> None:
    global _cancel_flag
    _cancel_flag = False


def is_cancel_requested() -> bool:
    return _cancel_flag


def request_cancel() -> None:
    """취소 플래그 켜고, 실행 중인 이미지 생성 서브프로세스가 있으면 kill."""
    global _cancel_flag
    _cancel_flag = True
    proc = _current_proc
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("subprocess kill 실패")


async def _check_cancel(update: Update) -> bool:
    """파이프라인 중간에 취소 요청이 들어왔는지 확인. True 면 호출 측이 즉시 return."""
    if is_cancel_requested():
        try:
            await update.message.reply_text("🛑 중단됨 — 현재 작업을 종료합니다.")
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def _load_session() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_session():
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session, ensure_ascii=False, indent=2))


session: dict = _load_session()


# ---------------------------------------------------------------- helpers


_NUM_RE = re.compile(r"\d+")


def extract_topic_slug(text: str) -> Optional[str]:
    """첫 번째 영문/숫자/하이픈 토큰을 슬러그로 추출."""
    for token in text.strip().split():
        cleaned = token.strip("/.,()[]{}\"'")
        if cleaned and all(c.isascii() and (c.isalnum() or c == "-") for c in cleaned):
            return cleaned.lower()
    return None


def template_path(slug: str) -> Path:
    return TEMPLATES_DIR / f"slides.{slug}.json"


def find_topic_by_hint(hint: str) -> dict | None:
    """
    hint(토픽명 또는 slug 일부)로 output/ 폴더에서 토픽 찾기.
    항상 파일 기반으로 검색 — session 의존 없음.
    """
    if not hint:
        return None
    hint_lower = hint.lower().replace(" ", "").replace("-", "")

    for d in OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        src = d / "sources.json"
        if not src.exists():
            continue
        try:
            data = json.loads(src.read_text())
            topic_kr = data.get("topic_kr", "")
            slug = d.name
            # slug 또는 topic_kr에서 hint 매칭
            if (hint_lower in slug.lower().replace("-", "")
                    or hint_lower in topic_kr.replace(" ", "")):
                return {
                    "slug": slug,
                    "topic_kr": topic_kr,
                    "sources_path": str(src),
                    "template_path": str(TEMPLATES_DIR / f"slides.{slug}.json"),
                    "output_dir": str(d),
                }
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- common_style 주입
# slides.example.json 의 common_style(디자인 DNA)을 새 템플릿에 자동 주입.
# 팔레트 B(응급·약물·예방접종 등 즉각 위험 주제)는 accent 를 coral→teal 로 변환.

EXAMPLE_TEMPLATE = TEMPLATES_DIR / "slides.example.json"
_EXAMPLE_COMMON_STYLE: Optional[str] = None

# CLAUDE.md 팔레트 룰: "잘못 적용 시 즉각적 위험?" YES→B. 위험 신호 키워드.
_PALETTE_B_KEYWORDS = [
    "응급", "약물", "용량", "투여", "예방접종", "백신", "경련", "열성경련",
    "119", "질식", "화상", "중독", "골절", "탈수", "아나필락시스", "위험",
    "mg", "dose", "vaccine", "emergency",
]


def _example_common_style() -> str:
    """slides.example.json 의 common_style 값(캐시)."""
    global _EXAMPLE_COMMON_STYLE
    if _EXAMPLE_COMMON_STYLE is None:
        try:
            d = json.loads(EXAMPLE_TEMPLATE.read_text(encoding="utf-8"))
            _EXAMPLE_COMMON_STYLE = (d.get("common_style") or "") if isinstance(d, dict) else ""
        except Exception:  # noqa: BLE001
            log.exception("slides.example.json common_style 로드 실패")
            _EXAMPLE_COMMON_STYLE = ""
    return _EXAMPLE_COMMON_STYLE


def decide_palette(sources: dict, topic_kr: str = "", slug: str = "") -> str:
    """팔레트 결정. sources 의 palette 우선, 없으면 위험 키워드로 A/B 판정."""
    p = str((sources or {}).get("palette", "")).strip().upper()
    if p in ("A", "B"):
        return p
    hay = f"{topic_kr} {slug}".lower()
    return "B" if any(k.lower() in hay for k in _PALETTE_B_KEYWORDS) else "A"


def common_style_for_palette(palette: str) -> str:
    """팔레트에 맞는 common_style 반환. B 면 primary accent 를 teal 로 치환.
    (톤 기반 common_style_for_tone 도입 이후엔 보조용)"""
    base = _example_common_style()
    if base and palette == "B":
        base = base.replace("coral #C44536", "teal #2C6E63")
    return base


# ---------------------------------------------------------------- 9종 톤 자동 선택
# 토픽 키워드 → 톤 매핑. 위에서부터 먼저 일치하는 규칙이 선택됨(순서 중요).

TONE_RULES = [
    {
        "tone": "emergency-alert",
        "keywords": ["응급", "경련", "골절", "출혈", "쇼크", "119", "숨골", "긴급", "위험 신호", "즉시"],
    },
    {
        "tone": "clean-infographic",
        "keywords": ["백신", "예방접종", "수치", "기준", "용량", "성장", "발달", "체크리스트", "비교", "vs"],
    },
    {
        "tone": "character-illustration",
        "keywords": ["수유", "이유식", "신생아", "모유", "분유", "수면", "영아", "아기"],
    },
    {
        "tone": "editorial-modern",
        "keywords": ["감기", "발열", "피부", "아토피", "기침", "콧물", "알레르기", "에어컨"],
    },
    {
        "tone": "handdrawn-notebook",
        "keywords": ["예방", "생활습관", "루틴", "체크리스트", "단계별", "가이드"],
    },
    {
        "tone": "dark-magazine",
        "keywords": ["심장", "소아심장", "선천성", "가와사키", "희귀", "전문"],
    },
    {
        "tone": "bold-typography",
        "keywords": ["통념", "오해", "사실은", "틀렸", "잘못"],
    },
    {
        "tone": "pastel-gradient",
        "keywords": ["영양", "성장", "키", "몸무게", "백분위", "발달"],
    },
    {
        "tone": "sticker-pop",
        "keywords": ["수족구", "chickenpox", "수두", "계절", "여름", "겨울"],
    },
]
DEFAULT_TONE = "editorial-modern"


# === 주제 "성격"(동사·맥락) 기반 1차 분류 ===
# 명사("신생아·아기")가 아니라 주제가 무엇을 하려는지(성격)로 스타일을 정한다.
# 순서 중요(위→아래 첫 일치): 위험 → 비교 → 부드러운 일상 → 실천·단계.
# 같은 "신생아" 주제라도 성격이 다르면 다른 스타일이 나오게 하는 것이 목적.
NATURE_RULES = [
    {   # 1) 위험·응급·판단형 — 잘못 적용 시 즉각 위험
        "tone": "emergency-alert",
        "keywords": [
            "응급", "응급실", "응급처치", "위험", "위급", "부딪", "찧", "화상",
            "데인", "경련", "발작", "질식", "삼킴", "삼켰", "이물", "기도막",
            "119", "쇼크", "출혈", "골절", "중독", "호흡곤란", "숨", "청색",
            "위험 신호", "즉시", "언제 병원", "응급 상황",
        ],
    },
    {   # 2) 비교·감별형
        "tone": "editorial-modern",
        "keywords": [
            "vs", "비교", "구별", "감별", "차이", "다른 점", "색깔로",
            "어떻게 다", "헷갈", "구분",
        ],
    },
    {   # 3) 부드러운 일상 — 캐릭터형(먹놀이·발달 놀이)
        "tone": "character-illustration",
        "keywords": [
            "이유식", "간식", "놀이", "장난감", "촉감", "그림책", "월령", "처음",
        ],
    },
    {   # 3') 부드러운 일상 — 손그림형(달램·돌봄·재우기)
        "tone": "handdrawn-notebook",
        "keywords": [
            "딸꾹질", "손톱", "달래", "달램", "트림", "배앓이", "산통", "잠투정",
            "낮잠", "목욕", "이앓이", "보채", "토닥", "재우", "안아", "스킨십",
            "기저귀", "속싸개",
        ],
    },
    {   # 4) 실천·단계·방법형 — 검증된 고성과 스타일
        "tone": "clean-infographic",
        "keywords": [
            "순서", "단계", "방법", "하는 법", "소독", "씻기", "씻는", "닦",
            "재우는 법", "먹이는", "관리법", "대처법", "준비물", "꿀팁", "루틴",
            "체크리스트",
        ],
    },
]


def classify_nature(text: str) -> str:
    """주제의 '성격'(동사·맥락 키워드)으로 1차 톤 분류. 신호 없으면 ""(폴백 유도)."""
    t = text or ""
    for rule in NATURE_RULES:
        for kw in rule["keywords"]:
            if kw in t:
                return rule["tone"]
    return ""


def normalize_tone(s: str) -> str:
    """톤 입력(정식명 또는 한글 별칭)을 정식 톤명으로 정규화. 모르면 ""."""
    if not s:
        return ""
    s = s.strip()
    if s.lower() in VALID_TONES:
        return s.lower()
    # 한글 별칭 부분 일치 (기존 TONE_NORMALIZE 재사용)
    for key, val in TONE_NORMALIZE.items():
        if key in s:
            return val
    return ""


def decide_tone(topic_kr: str, slug: str, user_requested_tone: str = None) -> str:
    """톤 자동 선택.
    우선순위: ① 사용자 수동 지정(별칭 포함) → ② 주제 '성격' 분류(명사 쏠림 방지)
    → ③ 기존 9종 명사 규칙 폴백(수족구→sticker-pop, 심장→dark-magazine 등 보존)
    → ④ DEFAULT_TONE."""
    # ① 사용자 수동 지정 우선
    if user_requested_tone:
        nt = normalize_tone(user_requested_tone)
        if nt:
            return nt
    # ② 주제 '성격'(동사·맥락) 우선 — "신생아/아기" 명사보다 먼저 판단
    hay = f"{topic_kr or ''} {slug or ''}"
    nature = classify_nature(hay)
    if nature:
        return nature
    # ③ 성격 신호가 없을 때만 기존 9종 명사 규칙 폴백
    for rule in TONE_RULES:
        for kw in rule["keywords"]:
            if kw in (topic_kr or "") or kw in (slug or ""):
                return rule["tone"]
    return DEFAULT_TONE


def common_style_for_tone(tone: str) -> str:
    """톤별 common_style(디자인 DNA) 로드. tone-templates → tone → example 순 폴백."""
    tone_path = TONE_DIR / f"{tone}.md"
    if tone_path.exists():
        return tone_path.read_text(encoding="utf-8")
    alt_path = TONE_DIR_ALT / f"{tone}.md"
    if alt_path.exists():
        return alt_path.read_text(encoding="utf-8")
    return _example_common_style()  # 최종 폴백


def is_authorized(update: Update, allowed_chat_id: Optional[str]) -> bool:
    if not allowed_chat_id:
        return True
    return str(update.effective_chat.id) == str(allowed_chat_id)


def _collect_text(msg) -> str:
    """anthropic Message 의 모든 텍스트 블록 결합."""
    parts = []
    for block in msg.content:
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
    return "\n".join(parts)


def _parse_json_object(raw: str):
    """raw 응답에서 JSON 배열 또는 객체 추출."""
    if not raw:
        return None
    # 1차: Markdown fence 제거
    text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    # 2차: 첫 { 또는 [ 부터 마지막 } 또는 ] 까지 추출
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


# ---------------------------------------------------------------- /start /status


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👶 소아과언니 카드뉴스 봇\n"
        "\n"
        "💬 자연어로 말씀해주세요:\n"
        "  • '주제 추천해줘' — 이번 주 주제 10개 추천\n"
        "  • '1번 만들어' — 추천 목록에서 선택해서 생성\n"
        "  • '수족구병 만들어줘' — 바로 생성\n"
        "  • '보류 목록 보여줘' — 나중에 쓸 주제 확인\n"
        "  • '완료된 거 뭐야' — 발행 완료 목록\n"
        "  • '다시' — 주제 다시 추천\n"
        "\n"
        "📌 슬래시 커맨드도 사용 가능:\n"
        "  /topics /new /batch /cancel /help\n"
        "  /queue /done /usedtopics /status\n"
        "\n"
        "📦 /batch — 여러 주제 한꺼번에 처리"
    )
    await update.message.reply_text(text)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if current_task["status"] == "idle":
        await update.message.reply_text("💤 진행 중인 작업 없음.")
        return
    started = current_task["started_at"]
    elapsed = int(time.time() - started) if started else 0
    text = (
        f"📊 현재 작업\n"
        f"  • 토픽: {current_task['topic']}\n"
        f"  • 상태: {current_task['status']}\n"
        f"  • 경과: {elapsed}초"
    )
    if current_task.get("error"):
        text += f"\n  • 에러: {current_task['error']}"
    await update.message.reply_text(text)


# ---------------------------------------------------------------- /topics


async def cmd_topics(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if Anthropic is None:
        await update.message.reply_text(
            "❌ anthropic 패키지 미설치. 'pip install anthropic' 후 봇 재시작."
        )
        return
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        await update.message.reply_text(
            "❌ .env 의 ANTHROPIC_API_KEY 가 비어있습니다."
        )
        return
    if not TOPIC_SELECTION_MD.exists():
        await update.message.reply_text(
            "❌ knowledge/topic-selection.md 파일을 찾을 수 없습니다."
        )
        return

    await update.message.reply_text("🤔 주제 추천 중... (Claude API 호출)")
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        recommendations = await asyncio.to_thread(call_anthropic_for_topics, api_key)
    except Exception as e:  # noqa: BLE001
        log.exception("anthropic 호출 실패")
        await update.message.reply_text(f"❌ 추천 실패: {e}")
        return

    if not recommendations:
        await update.message.reply_text(
            "❌ Claude 응답을 파싱하지 못했습니다. 다시 시도하려면 /topics."
        )
        return

    session["recommendation"] = recommendations
    _save_session()
    await update.message.reply_text(format_recommendations(recommendations))


def call_anthropic_for_topics(api_key: str) -> list[dict]:
    client = Anthropic(api_key=api_key)
    selection_md = TOPIC_SELECTION_MD.read_text(encoding="utf-8")
    today = date.today().isoformat()
    this_month = date.today().month
    done = sorted(done_slugs())

    system = (
        "당신은 소아과언니 카드뉴스 주제를 6필터로 선정하는 어시스턴트입니다.\n"
        "아래 정책을 그대로 적용하세요.\n\n"
        f"=== 정책 (knowledge/topic-selection.md) ===\n{selection_md}\n=== 정책 끝 ===\n\n"
        "반드시 아래 카테고리 구성으로 정확히 10개 추천:\n"
        "- 일상 케어·영양·생활건강: 2개\n"
        "- 성장발달: 2개\n"
        "- 질환관리: 2개\n"
        "- 응급·약물 안전: 2개\n"
        "- 소아심장: 1개\n"
        "- 예방접종: 1개\n"
        "총 10개. 카테고리 누락 금지. 순서는 카테고리 섞어서.\n\n"
        "카테고리 균형 필수 (7개 중):\n"
        "- 감염질환·바이러스 (수족구, 독감, RSV, 노로, 마이코플라스마 등): 1-2개 반드시 포함\n"
        "- 일상 케어·영양·생활건강: 1-2개\n"
        "- 성장발달: 1개\n"
        "- 응급·약물 안전: 1개\n"
        "- 예방접종: 1개\n"
        "- 소아심장: 0-1개\n\n"
        "시의성에만 치우치지 말 것.\n"
        "매번 다양한 카테고리에서 골고루 추천할 것.\n"
        "감염질환·바이러스 주제는 시즌과 무관하게 항상 후보에 포함.\n\n"
        "각 추천 주제에 가장 어울리는 톤도 함께 추천해줘.\n"
        "톤은 다음 5개 중 하나:\n"
        "- editorial-modern (일반 케어·예방법·생활건강)\n"
        "- handdrawn-notebook (영아 돌봄·수유·산통)\n"
        "- clean-infographic (수치·기준·비교표)\n"
        "- emergency-alert (응급·경련·즉시 신호)\n"
        "- character-illustration (이유식·발달·성장)"
    )
    user = (
        f"오늘은 {today} ({this_month}월)입니다.\n"
        f"이번 달 캘린더와 6필터를 적용해서 카드뉴스 주제 10개를 추천해주세요.\n"
        f"이미 완료된 다음 슬러그는 제외하세요: {done if done else '없음'}\n\n"
        "JSON 배열로만 응답하세요. 다른 텍스트, 설명, 마크다운은 절대 포함하지 마세요.\n"
        "각 항목 형식:\n"
        '[{"slug": "영문-소문자-하이픈-슬러그", '
        '"title_kr": "한국어 제목", '
        '"category": "카테고리 (예: 응급·약물 안전, 일상 케어 등)", '
        '"palette": "A 또는 B", '
        '"reason": "추천 이유 한 줄", '
        '"recommended_tone": "editorial-modern"}]'
    )

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = msg.content[0].text if msg.content else ""
    return parse_recommendations(raw)


def parse_recommendations(raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict) and "slug" in item]


TONE_DISPLAY = {
    "editorial-modern": "에디토리얼 모던",
    "handdrawn-notebook": "손그림 노트북",
    "clean-infographic": "클린 인포그래픽",
    "emergency-alert": "경고·긴급",
    "character-illustration": "캐릭터 일러스트",
    "dark-magazine": "다크 매거진",
    "pastel-gradient": "파스텔 그라데이션",
    "sticker-pop": "스티커 팝",
    "bold-typography": "볼드 타이포그래피",
}

TONE_NORMALIZE = {
    "에디토리얼": "editorial-modern",
    "모던": "editorial-modern",
    "손그림": "handdrawn-notebook",
    "노트북": "handdrawn-notebook",
    "클린": "clean-infographic",
    "인포그래픽": "clean-infographic",
    "경고": "emergency-alert",
    "긴급": "emergency-alert",
    "응급": "emergency-alert",
    "캐릭터": "character-illustration",
    "일러스트": "character-illustration",
    "다크": "dark-magazine",
    "매거진": "dark-magazine",
    "파스텔": "pastel-gradient",
    "그라데이션": "pastel-gradient",
    "그라디언트": "pastel-gradient",
    "스티커": "sticker-pop",
    "팝": "sticker-pop",
    "볼드": "bold-typography",
    "타이포": "bold-typography",
    "타이포그래피": "bold-typography",
}


async def cmd_topics_by_tone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tone_name: str,
):
    """특정 톤에 맞는 주제 10개 추천."""
    # 톤 이름 정규화 (자연어 → 파일명)
    for key, val in TONE_NORMALIZE.items():
        if key in tone_name:
            tone_name = val
            break

    tone_display = TONE_DISPLAY.get(tone_name, tone_name)
    await update.message.reply_text(
        f"🎨 {tone_display} 톤에 어울리는 주제 추천 중..."
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    done = done_slugs()

    def _sync():
        client = Anthropic(api_key=api_key)

        tone_path = REPO_ROOT / "knowledge" / "tone" / f"{tone_name}.md"
        tone_content = tone_path.read_text(encoding="utf-8") if tone_path.exists() else ""

        selection_path = REPO_ROOT / "knowledge" / "topic-selection.md"
        selection_content = (
            selection_path.read_text(encoding="utf-8")
            if selection_path.exists()
            else ""
        )

        today = datetime.now()
        month = today.month

        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=f"""소아과언니 카드뉴스 주제 추천자.
{selection_content}

JSON 배열로만 응답. 다른 텍스트 금지.
형식: [{{"slug": "영문-슬러그", "title_kr": "한국어 제목", "category": "카테고리", "palette": "A또는B", "reason": "추천 이유 1줄", "recommended_tone": "{tone_name}"}}]
""",
            messages=[{
                "role": "user",
                "content": f"""
현재 월: {month}월
선택된 톤: {tone_name}
톤 특성:
{tone_content[:500]}

이 톤에 가장 잘 어울리는 소아과 카드뉴스 주제 10개 추천.
done 목록(제외): {sorted(done)}
조건:
- {tone_name} 톤의 시각적 특성과 잘 맞는 주제
- 6필터 통과 가능한 주제
- 이번 달({month}월) 시의성 반영
JSON으로만 응답.
""",
            }],
        )
        raw = _collect_text(response)
        return parse_recommendations(raw)

    recs = await asyncio.to_thread(_sync)
    if not recs:
        await update.message.reply_text("❌ 추천 실패. 다시 시도해주세요.")
        return

    # recommended_tone 누락 시 선택된 톤으로 채워넣기
    for r in recs:
        if not r.get("recommended_tone"):
            r["recommended_tone"] = tone_name

    # 세션에 저장
    session["recommendation"] = recs
    _save_session()

    # 포맷 출력
    lines = [f"🎨 {tone_display} 톤 주제 추천 10개\n"]
    for i, r in enumerate(recs[:10], 1):
        lines.append(
            f"{i}. {r.get('title_kr','')}\n"
            f"   🎨 {tone_display} | {r.get('category','')}\n"
            f"   💡 {r.get('reason','')}"
        )
    lines.append("\n👉 원하는 번호를 입력하세요 (예: 1 3 5)")
    lines.append("🔄 마음에 안 들면 '다시' 입력")

    await update.message.reply_text("\n".join(lines))


async def cmd_keyword_topics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    keyword: str
):
    """특정 키워드/질환 관련 주제 7개 추천."""
    await update.message.reply_text(
        f"🔍 '{keyword}' 관련 주제 추천 중..."
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    done = done_slugs()

    def _sync():
        client = Anthropic(api_key=api_key)

        selection_path = REPO_ROOT / "knowledge" / "topic-selection.md"
        selection_content = selection_path.read_text() if selection_path.exists() else ""

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            system=f"""소아과언니 카드뉴스 주제 추천자.
{selection_content}

JSON 배열로만 응답. 다른 텍스트 금지.
형식: [{{"slug": "영문-슬러그", "title_kr": "한국어 제목", "category": "카테고리", "palette": "A또는B", "reason": "추천 이유 1줄", "recommended_tone": "톤이름"}}]
""",
            messages=[{
                "role": "user",
                "content": f"""
키워드: {keyword}
done 목록(제외): {list(done)}

'{keyword}' 와 직접 관련된 소아과 카드뉴스 주제 7개 추천.
예시:
- 수족구 → 수족구병 증상과 격리 기준, 수족구 입안 물집 대처법, 수족구 vs 헤르판지나 구별법 등
- 단순 날씨/시의성 주제 말고 이 키워드에 집중한 주제로
JSON으로만 응답.
"""
            }]
        )
        raw = _collect_text(response)
        return _parse_json_object(raw)

    recs = await asyncio.to_thread(_sync)
    if not recs or not isinstance(recs, list):
        await update.message.reply_text("❌ 추천 실패. 다시 시도해주세요.")
        return

    # 세션 저장
    session["recommendation"] = recs
    _save_session()

    tone_display_map = {
        "editorial-modern": "에디토리얼 모던",
        "handdrawn-notebook": "손그림 노트북",
        "clean-infographic": "클린 인포그래픽",
        "emergency-alert": "경고·긴급",
        "character-illustration": "캐릭터 일러스트",
    }

    lines = [f"🔍 '{keyword}' 관련 주제 추천 7개\n"]
    for i, r in enumerate(recs[:7], 1):
        tone = r.get("recommended_tone", "editorial-modern")
        tone_disp = tone_display_map.get(tone, tone)
        lines.append(
            f"{i}. {r.get('title_kr','')}\n"
            f"   🎨 {tone_disp} | {r.get('category','')}\n"
            f"   💡 {r.get('reason','')}"
        )
    lines.append("\n👉 원하는 번호를 입력하세요 (예: 1 3 5)")
    lines.append("🔄 마음에 안 들면 '다시' 입력")

    await update.message.reply_text("\n".join(lines))


def format_recommendations(recs: list[dict]) -> str:
    lines = ["📋 이번 주 주제 추천 10개", ""]
    for i, r in enumerate(recs[:10], 1):
        tone_key = r.get("recommended_tone", "")
        tone_display = TONE_DISPLAY.get(tone_key, tone_key or "?")
        lines.append(f"{i}. {r.get('title_kr','?')}")
        lines.append(f"   🎨 {tone_display} | {r.get('category','?')}")
        if r.get("reason"):
            lines.append(f"   💡 {r['reason']}")
    lines.append("")
    lines.append("👉 원하는 번호를 입력하세요 (예: 1 3 5)")
    lines.append("🔄 마음에 안 들면 '다시' 입력")
    return "\n".join(lines)


# ---------------------------------------------------------------- /queue /done


async def cmd_queue(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    pending = load_topics().get("pending", [])
    if not pending:
        await update.message.reply_text("📥 보류 주제 없음.")
        return
    lines = [f"📥 보류 주제 목록 ({len(pending)}개)", ""]
    for i, t in enumerate(pending, 1):
        lines.append(
            f"{i}. {t.get('title_kr','?')} ({t.get('category','?')} / {t.get('palette','?')})"
        )
    lines.append("")
    lines.append("👉 '큐 1 3' — 이번 주로 이동")
    lines.append("👉 '삭제 2' — 삭제")
    await update.message.reply_text("\n".join(lines))


async def cmd_done(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    done = load_topics().get("done", [])
    total = len(done)
    if total == 0:
        await update.message.reply_text("✅ 완료된 주제 없음.")
        return
    recent = done[-DONE_PREVIEW:]
    lines = [f"✅ 완료된 주제 (총 {total}개)", ""]
    for t in recent:
        lines.append(
            f"• {t.get('slug','?')} — {t.get('title_kr','?')} ({t.get('done_at','?')})"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_used_topics(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """used-topics.json 전체 목록을 날짜순(최신 먼저)으로 출력."""
    topics = load_used_topics().get("topics", [])
    total = len(topics)
    if total == 0:
        await update.message.reply_text(
            "📚 등록된 주제가 없어요.\n"
            "   python3 scripts/import-topics.py 로 기존 주제를 일괄 등록할 수 있어요."
        )
        return
    ordered = sorted(topics, key=lambda t: t.get("date", ""), reverse=True)
    lines = [f"📚 사용한 주제 목록 (총 {total}개)", ""]
    for t in ordered:
        lines.append(
            f"{t.get('date','?')} | {t.get('category','미분류')} | "
            f"{t.get('title', t.get('slug','?'))}"
        )
    # 텔레그램 4096자 제한 대비 분할 전송
    text = "\n".join(lines)
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += (line + "\n")
    if chunk.strip():
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------- /new + duplicate


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args_text = " ".join(context.args) if context.args else ""
    slug = extract_topic_slug(args_text)
    if not slug:
        await update.message.reply_text(
            "❗ 영문 슬러그가 필요합니다. 예) /new sleep-duration"
        )
        return
    await begin_new_with_duplicate_check(update, slug)


async def begin_new_with_duplicate_check(update: Update, slug: str) -> None:
    if slug in done_slugs():
        session["duplicate_slug"] = slug
        await update.message.reply_text(
            f"⚠️ '{slug}'는 이미 완료된 주제입니다. 계속할까요? (예/아니오)"
        )
        return
    await trigger_generation(update, slug)


class _MsgUpdate:
    """콜백 쿼리에서 auto_pipeline 으로 넘길 때 .message 만 노출하는 경량 래퍼.
    (auto_pipeline / trigger_generation_direct 은 update.message 만 사용)"""

    def __init__(self, message):
        self.message = message


async def precheck_and_generate(
    update: Update, topic_kr: str, slug: str, forced_tone: str = ""
) -> None:
    """새 주제 생성 직전, used-topics.json 과 유사도 체크.
    유사 주제가 있으면 인라인 키보드로 확인받고, 없으면 바로 생성."""
    try:
        keywords = await extract_keywords(topic_kr)
        matches = similar_used_topics(keywords)
    except Exception as e:  # noqa: BLE001
        log.exception("중복 체크 실패 — 그대로 진행: %s", e)
        matches = []

    if not matches:
        await auto_pipeline(update, topic_kr, slug, forced_tone=forced_tone)
        return

    # 유사 주제 발견 → 인라인 키보드로 확인
    session["pending_generation"] = {
        "topic_kr": topic_kr,
        "slug": slug,
        "forced_tone": forced_tone,
    }
    _save_session()

    lines = ["⚠️ 비슷한 주제가 이미 있어요!", "", "📋 유사한 이전 주제:"]
    for m in matches:
        lines.append(f"- {m.get('date','?')} | {m.get('title', m.get('slug','?'))}")
    lines.append("")
    lines.append("계속 진행할까요?")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 네, 새로 만들기", callback_data="dup:ok")],
            [InlineKeyboardButton("❌ 아니요, 취소", callback_data="dup:cancel")],
        ]
    )
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def on_duplicate_callback(
    update: Update, _: ContextTypes.DEFAULT_TYPE
) -> None:
    """인라인 키보드 '네/아니요' 처리."""
    q = update.callback_query
    await q.answer()
    pending = session.get("pending_generation")
    session["pending_generation"] = None
    _save_session()

    if q.data == "dup:cancel":
        await q.edit_message_text("❌ 취소됐어요.")
        return

    # dup:ok
    if not pending:
        await q.edit_message_text("⚠️ 진행할 주제 정보가 만료됐어요. 다시 입력해주세요.")
        return
    topic_kr = pending.get("topic_kr", "")
    slug = pending.get("slug", "")
    forced_tone = pending.get("forced_tone", "")
    await q.edit_message_text(f"✅ 새로 만들기 — '{topic_kr}'")
    reset_cancel_flag()
    await auto_pipeline(_MsgUpdate(q.message), topic_kr, slug, forced_tone=forced_tone)


# ---------------------------------------------------------------- intent router


async def intent_router(text: str) -> dict:
    """자연어 입력을 Claude API 로 분석해서 의도 + 파라미터 반환."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or Anthropic is None:
        return {"intent": "unknown", "params": {}}
    try:
        return await asyncio.to_thread(_intent_router_sync, text, api_key)
    except Exception as e:  # noqa: BLE001
        log.exception("intent_router 실패: %s", e)
        return {"intent": "unknown", "params": {}}


def _intent_router_sync(text: str, api_key: str) -> dict:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=200,
        system="""소아과언니 카드뉴스 봇의 의도 분류기.
사용자 입력을 분석해서 JSON으로만 반환. 다른 텍스트 금지.

의도 목록:
- topics: 주제 추천 요청 ("주제 추천해줘", "이번 주 주제", "뭐 만들까" 등)
- make: 카드뉴스 생성 ("X 만들어줘", "N번 만들어", "X 카드뉴스 해줘" 등)
  → 톤 키워드가 함께 언급되면 params.tone 에 톤 파일명을 채움
  → 톤 키워드 매핑:
    · 에디토리얼/모던 → editorial-modern
    · 손그림/노트북/따뜻한 → handdrawn-notebook
    · 클린/인포그래픽/데이터 → clean-infographic
    · 경고/긴급/응급/빨간 → emergency-alert
    · 캐릭터/일러스트/귀여운/파스텔 → character-illustration
  → 톤 언급 없으면 tone: "" (빈 문자열)
  → select 에서도 톤이 함께 언급되면 params.tone 동일 매핑 적용
- queue: 보류 목록 조회 ("보류 목록", "나중에 쓸 것", "큐 보여줘" 등)
- done: 완료 목록 조회 ("완료된 거", "다 만든 것", "발행한 것" 등)
- status: 현재 상태 ("지금 뭐 해", "진행 중인 거", "상태 확인" 등)
- select: 추천 목록 또는 이번 주 목록에서 번호 선택 ("1번", "1 3 5", "첫 번째" 등)
  → "만들어줘" / "만들어" / "해줘" / "생성" / "쭉" / "차례로" 같은 키워드가 함께 있으면 params.generate=true
  → 여러 번호 + generate=true 면 순차 생성 의도. 예) "1,3,5,6,7번 만들어줘"
  → 단순 번호만(생성 키워드 없음)이면 generate 생략 또는 false
- queue_move: 보류에서 이번 주로 이동 ("3번 이번 주로", "큐 3 올려줘" 등)
- delete: 보류에서 삭제 ("2번 없애줘", "삭제 1" 등)
- retry: 재추천 요청 ("다시", "다른 거", "다시 추천해줘" 등)
- confirm: 확인/진행 ("응", "좋아", "예", "ㅇㅇ", "ok", "진행해" 등)
- cancel: 취소/중단 ("아니", "취소", "됐어", "ㄴㄴ", "중단", "중단해", "그만", "그만해", "멈춰", "stop", "halt" 등) — 파이프라인 진행 중이면 이미지 생성도 즉시 중단됨
- feedback_regen: 특정 슬라이드 재생성 요청
  ("4장 내용 너무 어려워", "3번 다시 만들어줘",
   "slide-05 수정해줘", "5번 슬라이드 바꿔줘" 등)
  → params: {"slide_n": 4, "feedback": "너무 어려워", "topic_hint": ""}
- verify_slide: 의학 내용 확인 질문
  ("8번 내용 맞아?", "3장 의학적으로 정확해?",
   "이 내용 근거 있어?" 등)
  → params: {"slide_n": 8, "topic_hint": ""}
- edit_slide: 구체적 수정 지시
  ("3번 슬라이드 38도 아니고 38.5도야",
   "5장에 부루펜 6개월 이상이라고 추가해줘" 등)
  → params: {"slide_n": 3, "instruction": "38도 아니고 38.5도야", "topic_hint": ""}
- general_question: 현재 토픽 관련 일반 질문
  ("차멀미약 몇 살부터 먹여?",
   "이 내용 부모들한테 어떻게 설명하면 좋아?" 등)
  → params: {"question": "질문 내용", "topic_hint": ""}

★ topic_hint 추출 규칙 (feedback_regen·verify_slide·edit_slide·general_question 공통):
  입력에 토픽명/주제가 함께 언급되면 params.topic_hint 에 그 토픽명을 채움.
  - "신생아 배꼽관리 2번 슬라이드 수정해줘" → topic_hint: "신생아 배꼽관리", slide_n: 2
  - "차멀미 카드뉴스 3번 다시 만들어줘" → topic_hint: "차멀미", slide_n: 3
  - "4번 내용 너무 어려워" → topic_hint: "" (현재 토픽 사용)
- context_reference: 이미 만든 카드뉴스를 언급
  ("travel-emergency-kit 말이야", "아까 만든 거 말이야",
   "방금 그거", "그 카드뉴스" 등)
  → params: {"slug": "travel-emergency-kit"}
- edit_existing: 이미 만들어진 카드뉴스 수정 요청
  ("신생아 배꼽관리 이미 만들어져있는데 수정",
   "기존 X 카드뉴스 수정해줘",
   "X 슬라이드 내용 틀렸어 고쳐줘" 등)
  → params: {"slug_hint": "신생아 배꼽관리", "instruction": "수정 내용"}
- topics_by_tone: 특정 톤으로 주제 추천 요청
  ("손그림 톤으로 추천해줘", "캐릭터 스타일 주제 뭐 있어",
   "에디토리얼 모던으로 해줘", "클린 인포그래픽 주제 추천" 등)
  → params: {"tone": "handdrawn-notebook"}
- keyword_topics: 특정 키워드/질환 관련 주제 추천 요청
  ("수족구 관련 주제 추천해줘", "RSV로 뭔가 만들어봐",
   "독감 관련 뭐 있어?" 등)
  → params: {"keyword": "수족구"}
- unknown: 위 중 어느 것도 아님

JSON 형식:
{"intent": "make", "params": {"topic_kr": "차멀미 예방법", "tone": ""}}
{"intent": "make", "params": {"topic_kr": "수족구병", "tone": "character-illustration"}}
{"intent": "select", "params": {"numbers": [1, 3, 5]}}
{"intent": "select", "params": {"numbers": [1, 3, 5, 6, 7], "generate": true}}  // "1,3,5,6,7번 만들어줘" — 순차 생성
{"intent": "queue_move", "params": {"numbers": [3]}}
{"intent": "delete", "params": {"numbers": [2]}}
{"intent": "topics", "params": {}}

톤 추출 예시:
"수족구병 캐릭터스타일로 해줘" → {"intent": "make", "params": {"topic_kr": "수족구병", "tone": "character-illustration"}}
"열성경련 경고톤으로 만들어줘" → {"intent": "make", "params": {"topic_kr": "열성경련", "tone": "emergency-alert"}}
"1번 손그림으로 만들어줘" → {"intent": "select", "params": {"numbers": [1], "tone": "handdrawn-notebook"}}
{"intent": "feedback_regen", "params": {"slide_n": 4, "feedback": "너무 어려워", "topic_hint": ""}}
{"intent": "verify_slide", "params": {"slide_n": 8, "topic_hint": ""}}
{"intent": "edit_slide", "params": {"slide_n": 3, "instruction": "38도 아니고 38.5도야", "topic_hint": "신생아 배꼽관리"}}
{"intent": "general_question", "params": {"question": "차멀미약 몇 살부터?", "topic_hint": ""}}
{"intent": "context_reference", "params": {"slug": "travel-emergency-kit"}}
{"intent": "edit_existing", "params": {"slug_hint": "신생아 배꼽관리", "instruction": "3번 슬라이드 내용 수정"}}
{"intent": "topics_by_tone", "params": {"tone": "handdrawn-notebook"}}
{"intent": "keyword_topics", "params": {"keyword": "수족구"}}
{"intent": "unknown", "params": {}}
""",
        messages=[{"role": "user", "content": text}],
    )
    raw = _collect_text(response).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"intent": "unknown", "params": {}}


# ---------------------------------------------------------------- /batch (일괄 처리)
# 설계: 기존 stage 머신이 없으므로 session["batch"] dict 하나로 상태를 표현.
#   - {"stage": "awaiting_topics"}                  → 주제 줄목록 입력 대기
#   - {"stage": "awaiting_confirm", "queue": [...]} → 비용 확인('확인') 대기
#   - {"stage": "running", "queue": [...]}          → 순차 처리 중
# 풀 파이프라인은 기존 auto_pipeline 을 그대로 호출(본체 무수정).
# 취소는 기존 request_cancel() 인프라 재사용 — 즉시 중단(서브프로세스 kill).

_BATCH_CONFIRM_WORDS = {"확인", "ok", "진행", "예", "네", "ㅇㅋ", "ㅇㅇ"}
_BATCH_CANCEL_WORDS = {"취소", "중단", "그만", "그만해", "멈춰", "cancel", "stop"}


def _parse_batch_topics(text: str) -> tuple[list[dict], list[str]]:
    """줄바꿈 기준 주제 목록 파싱.
    반환: (처리할 주제 리스트[{"topic","tone"}], 운영자 안내 메시지 리스트).
    규칙:
      - 빈 줄 제거, 앞뒤 공백 제거, 주제 5자 미만 줄 제외, 최대 10개
      - '주제 | 스타일' 형식이면 스타일 수동 지정(정식명·한글별칭 허용)
        · 5종/9종에 없는 스타일이면 안내 후 자동매핑으로 폴백(tone="")"""
    notices: list[str] = []
    topics: list[dict] = []
    entry_no = 0  # 비어있지 않은 줄에만 번호 부여 (운영자가 보는 항목 순번)
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        entry_no += 1

        # '주제 | 스타일' 수동 지정 분리 (첫 '|' 기준)
        tone = ""
        if "|" in line:
            topic_part, style_part = line.split("|", 1)
            topic = topic_part.strip()
            style_raw = style_part.strip()
            if style_raw:
                nt = normalize_tone(style_raw)
                if nt:
                    tone = nt
                else:
                    notices.append(
                        f"⚠️ {entry_no}번째 줄의 스타일 '{style_raw}'은 없는 스타일이라 "
                        f"자동매핑을 사용할게요."
                    )
        else:
            topic = line

        if len(topic) < BATCH_MIN_TOPIC_LEN:
            notices.append(f"⚠️ {entry_no}번째 줄('{topic}')은 너무 짧아 제외했어요.")
            continue
        topics.append({"topic": topic, "tone": tone})

    if len(topics) > BATCH_MAX_TOPICS:
        dropped = len(topics) - BATCH_MAX_TOPICS
        topics = topics[:BATCH_MAX_TOPICS]
        notices.append(
            f"⚠️ 최대 {BATCH_MAX_TOPICS}개까지만 가능해서 뒤 {dropped}개는 제외하고 "
            f"앞 {BATCH_MAX_TOPICS}개만 처리할게요."
        )
    return topics, notices


def _format_batch_confirm(topics: list[dict]) -> str:
    """확인 게이트 메시지(주제 목록 + 수동지정 스타일 + 예상 비용)."""
    n = len(topics)
    cost = n * BATCH_COST_PER_TOPIC_USD
    lines = [f"📋 처리할 주제 {n}개:", ""]
    for i, t in enumerate(topics, 1):
        suffix = ""
        if t.get("tone"):
            disp = TONE_DISPLAY.get(t["tone"], t["tone"])
            suffix = f"  🎨 {disp}(지정)"
        lines.append(f" {i}. {t['topic']}{suffix}")
    lines.append("")
    lines.append(
        f"예상: 이미지 {n}×{BATCH_IMAGES_PER_TOPIC}장, 약 ${cost:.2f} 비용 발생"
    )
    lines.append("진행하려면 '확인', 취소하려면 /cancel")
    return "\n".join(lines)


async def cmd_batch(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/batch — 여러 주제를 줄바꿈으로 받아 순차 일괄 처리 시작."""
    if _task_lock.locked():
        await update.message.reply_text(
            f"⏳ 이미 다른 작업이 진행 중입니다 ({current_task.get('topic')}).\n"
            f"   끝난 뒤 다시 /batch 해주세요. (/status 로 확인)"
        )
        return
    session["batch"] = {"stage": "awaiting_topics"}
    _save_session()
    await update.message.reply_text(
        "📝 처리할 주제를 줄바꿈으로 여러 개 보내주세요. (예: 한 줄에 하나씩)\n"
        f"   • 한 줄에 한 주제, 최대 {BATCH_MAX_TOPICS}개\n"
        f"   • 너무 짧은 줄({BATCH_MIN_TOPIC_LEN}자 미만)은 자동 제외\n"
        "   • 그만두려면 /cancel"
    )


async def run_batch(update: Update, queue: list[dict]) -> None:
    """큐의 주제를 하나씩 기존 auto_pipeline 으로 순차 처리.
    각 항목은 {"topic","tone"} — tone 이 있으면 수동지정 스타일로 강제.
    한 개 실패해도 멈추지 않고 다음으로. /cancel(즉시 중단) 시 남은 큐 취소."""
    reset_cancel_flag()
    session["batch"] = {"stage": "running", "queue": queue}
    _save_session()

    total = len(queue)
    success = 0
    failed: list[str] = []
    cancelled = False

    await update.message.reply_text(
        f"🚀 배치 시작 — 총 {total}개 주제를 순서대로 처리합니다.\n"
        "💡 중간에 멈추려면 /cancel (현재 이미지 생성까지 즉시 중단)"
    )

    for i, item in enumerate(queue, 1):
        topic_kr = item["topic"]
        forced_tone = item.get("tone", "")
        if is_cancel_requested():
            cancelled = True
            await update.message.reply_text(
                f"🛑 중단됨 — {i - 1}/{total} 완료, 나머지 {total - (i - 1)}개 취소"
            )
            break

        tone_note = f" (🎨 {forced_tone} 지정)" if forced_tone else ""
        await update.message.reply_text(
            f"⏳ ({i}/{total}) '{topic_kr}' 처리 시작...{tone_note}"
        )
        try:
            slug = await korean_to_slug(topic_kr)
            # 기존 풀 파이프라인 재사용 (sources→검증→템플릿→이미지→캡션→블로그→릴스)
            # forced_tone 은 decide_tone 에서 최우선 적용됨
            await auto_pipeline(update, topic_kr, slug, forced_tone=forced_tone)
        except Exception as e:  # noqa: BLE001
            log.exception("batch auto_pipeline 실패 (%s)", topic_kr)
            failed.append(topic_kr)
            await update.message.reply_text(
                f"⚠️ ({i}/{total}) '{topic_kr}' 실패: {e}. 다음 주제로 넘어갑니다."
            )
            continue

        # 파이프라인 내부에서 취소된 경우 — 성공으로 치지 않고 종료
        if is_cancel_requested():
            cancelled = True
            await update.message.reply_text(
                f"🛑 중단됨 — {i}/{total} 진행 중 취소, 나머지 건너뜀"
            )
            break

        success += 1
        await update.message.reply_text(f"✅ ({i}/{total}) '{topic_kr}' 완료")

    session["batch"] = None
    _save_session()

    if not cancelled:
        lines = [
            f"🎉 배치 완료 — 총 {total}개 중 성공 {success}개 / 실패 {len(failed)}개"
        ]
        if failed:
            lines.append("")
            lines.append("실패 목록:")
            for t in failed:
                lines.append(f"  • {t}")
        await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — 진행 중이면 즉시 중단, 배치 대기 상태면 큐 비우고 IDLE 복귀."""
    was_running = _task_lock.locked() or _current_proc is not None
    had_batch = bool(session.get("batch"))
    request_cancel()  # 실행 중 서브프로세스가 있으면 즉시 kill
    if session.get("recommendation"):
        session["recommendation"] = None
    session["batch"] = None
    session["duplicate_slug"] = None
    session["pending_generation"] = None
    _save_session()

    if was_running:
        await update.message.reply_text(
            "🛑 중단 요청됨 — 현재 단계 종료 후 멈춥니다.\n"
            "(이미지 생성 중이면 프로세스도 즉시 종료됩니다)"
        )
    elif had_batch:
        await update.message.reply_text("✅ 배치를 취소했어요. 남은 주제는 비웠습니다.")
    else:
        await update.message.reply_text("✅ 취소할 작업이 없어요.")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — 사용 가능한 커맨드 안내."""
    text = (
        "👶 소아과언니 카드뉴스 봇 — 도움말\n"
        "\n"
        "📌 슬래시 커맨드:\n"
        "  /topics — 이번 주 주제 10개 추천\n"
        "  /new <slug> — 단일 주제 카드뉴스 생성\n"
        "  /batch — 여러 주제 한꺼번에 처리\n"
        "  /cancel — 진행 중 작업 즉시 중단\n"
        "  /queue /done /usedtopics /status — 목록·상태 조회\n"
        "\n"
        "💬 자연어도 됩니다:\n"
        "  • '주제 추천해줘' / '수족구병 만들어줘'\n"
        "  • '1 3 5번 만들어줘' (추천 목록 순차 생성)\n"
        "  • '중단' / '취소' (진행 중 멈춤)"
    )
    await update.message.reply_text(text)


# ---------------------------------------------------------------- handle_text


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return

    # === /batch 진행 상태 인터셉트 (intent_router 보다 먼저) ===
    # 주제 줄목록·'확인'이 Claude 의도 분류기로 잘못 들어가지 않도록 여기서 가로챈다.
    batch = session.get("batch")
    if batch and batch.get("stage") == "awaiting_topics":
        topics, notices = _parse_batch_topics(text)
        for note in notices:
            await update.message.reply_text(note)
        if not topics:
            await update.message.reply_text(
                "❌ 처리할 주제가 없어요. 한 줄에 하나씩 다시 보내주세요. (그만두려면 /cancel)"
            )
            return
        session["batch"] = {"stage": "awaiting_confirm", "queue": topics}
        _save_session()
        await update.message.reply_text(_format_batch_confirm(topics))
        return
    if batch and batch.get("stage") == "awaiting_confirm":
        low = text.strip().lower()
        if low in _BATCH_CONFIRM_WORDS:
            queue = list(batch.get("queue", []))
            if not queue:
                session["batch"] = None
                _save_session()
                await update.message.reply_text("❌ 큐가 비어 있어요. /batch 로 다시 시작해주세요.")
                return
            await run_batch(update, queue)
            return
        if low in _BATCH_CANCEL_WORDS:
            session["batch"] = None
            _save_session()
            await update.message.reply_text("✅ 배치를 취소했어요.")
            return
        await update.message.reply_text(
            "❓ '확인' 이라고 보내면 시작하고, /cancel 이면 취소돼요."
        )
        return

    # duplicate confirm/cancel 대기 중이면 기존 로직
    if session.get("duplicate_slug"):
        if text.lower() in ("예", "yes", "y", "응", "ㅇ", "ㅇㅇ", "ok", "진행"):
            slug = session["duplicate_slug"]
            session["duplicate_slug"] = None
            await trigger_generation(update, slug)
        else:
            session["duplicate_slug"] = None
            await update.message.reply_text("❌ 취소됐어요.")
        return

    # Claude API 로 자연어 의도 분류
    intent_data = await intent_router(text)
    intent = intent_data.get("intent", "unknown")
    params = intent_data.get("params", {})

    if intent == "topics":
        await cmd_topics(update, context)

    elif intent == "make":
        topic_kr = params.get("topic_kr", "")
        if not topic_kr:
            await update.message.reply_text("💡 어떤 주제로 만들까요?")
            return
        data = load_topics()
        matched = next(
            (
                t
                for t in data.get("this_week", [])
                if topic_kr in t.get("title_kr", "")
            ),
            None,
        )
        if matched:
            slug = matched["slug"]
        else:
            await update.message.reply_text(f"🔤 '{topic_kr}' 영문 슬러그 생성 중...")
            slug = await korean_to_slug(topic_kr)
            if not slug:
                await update.message.reply_text(
                    f"❌ '{topic_kr}' 슬러그 변환 실패."
                )
                return
        forced_tone = params.get("tone", "")
        reset_cancel_flag()
        await precheck_and_generate(update, topic_kr, slug, forced_tone=forced_tone)

    elif intent == "select":
        numbers = params.get("numbers", [])
        forced_tone = params.get("tone", "")
        generate = bool(params.get("generate", False))
        if not numbers:
            await update.message.reply_text("💡 번호를 알려주세요.")
            return

        # 소스 결정 — 추천 활성 시 추천 목록, 아니면 this_week
        recs = session.get("recommendation") or []
        if recs:
            source_list = recs
            source_name = "추천"
        else:
            _data = load_topics()
            source_list = _data.get("this_week", [])
            source_name = "이번 주"

        print(
            f"[DEBUG] select intent — numbers={numbers}, generate={generate}, "
            f"source={source_name}({len(source_list)}개)",
            flush=True,
        )

        if not source_list:
            await update.message.reply_text(
                "❓ 활성 추천 목록도 이번 주 주제도 없습니다. /topics 로 시작하세요."
            )
            return

        valid_nums = sorted({n for n in numbers if 1 <= n <= len(source_list)})
        if not valid_nums:
            await update.message.reply_text(
                f"❌ 유효한 번호가 없습니다 (1-{len(source_list)})."
            )
            return

        # === 순차 생성 모드: "1,3,5,6,7번 만들어줘" ===
        if generate:
            reset_cancel_flag()
            total = len(valid_nums)
            nums_label = ",".join(str(n) for n in valid_nums)
            await update.message.reply_text(
                f"🚀 순차 생성 시작 — {source_name} 목록 {nums_label}번 (총 {total}개)\n"
                f"💡 중간에 멈추고 싶으면 '중단' 또는 '취소' 라고 보내주세요."
            )
            for i, n in enumerate(valid_nums, 1):
                if is_cancel_requested():
                    await update.message.reply_text(
                        f"🛑 중단됨 — {i-1}/{total} 완료, 나머지 건너뜀"
                    )
                    return
                topic = source_list[n - 1]
                topic_kr = topic.get("title_kr", "")
                slug = topic.get("slug", "")
                if not slug:
                    slug = await korean_to_slug(topic_kr)
                await update.message.reply_text(
                    f"\n━━━ [{i}/{total}] {n}번 ━━━\n📌 {topic_kr}"
                )
                try:
                    await auto_pipeline(
                        update, topic_kr, slug, forced_tone=forced_tone
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("auto_pipeline 실패 (%s)", slug)
                    await update.message.reply_text(
                        f"⚠️ {slug} 실패 — 다음으로 넘어갑니다: {e}"
                    )
                if is_cancel_requested():
                    await update.message.reply_text(
                        f"🛑 중단됨 — {i}/{total} 완료, 나머지 건너뜀"
                    )
                    return
            await update.message.reply_text(f"✅ 순차 생성 전체 완료 — {total}/{total}")
            return

        # === 기존 동작: 추천 활성 + 톤 + 단일 번호 → 추천에서 직접 생성 ===
        if recs and forced_tone and len(numbers) == 1:
            idx = numbers[0] - 1
            if 0 <= idx < len(recs):
                topic = recs[idx]
                topic_kr = topic.get("title_kr", "")
                slug = topic.get("slug", "")
                if not slug:
                    slug = await korean_to_slug(topic_kr)
                # this_week 에도 저장
                await apply_recommendation_selection(update, numbers)
                reset_cancel_flag()
                await auto_pipeline(
                    update, topic_kr, slug, forced_tone=forced_tone
                )
                return

        # === 기존: 추천 모드 다중 선택 → 확정만 ===
        if recs:
            await apply_recommendation_selection(update, numbers)
            return

        # === 기존: this_week 직접 선택 (단일 번호 즉시 생성) ===
        idx = numbers[0] - 1
        if 0 <= idx < len(source_list):
            topic = source_list[idx]
            topic_kr = topic.get("title_kr", "")
            slug = topic.get("slug", "")
            if not slug:
                slug = await korean_to_slug(topic_kr)
            reset_cancel_flag()
            await auto_pipeline(
                update, topic_kr, slug, forced_tone=forced_tone
            )
        else:
            await update.message.reply_text(
                f"💡 이번 주 확정된 주제가 {len(source_list)}개예요.\n"
                f"1-{len(source_list)} 사이 번호를 말씀해주세요."
            )

    elif intent == "queue":
        await cmd_queue(update, context)

    elif intent == "done":
        await cmd_done(update, context)

    elif intent == "status":
        await cmd_status(update, context)

    elif intent == "queue_move":
        numbers = params.get("numbers", [])
        if numbers:
            await move_pending_to_week(update, numbers)
        else:
            await update.message.reply_text("💡 번호를 알려주세요. 예: '3번 이번 주로'")

    elif intent == "delete":
        numbers = params.get("numbers", [])
        if numbers:
            await delete_from_pending(update, numbers)
        else:
            await update.message.reply_text("💡 번호를 알려주세요. 예: '2번 삭제'")

    elif intent == "retry":
        if session.get("recommendation"):
            session["recommendation"] = None
            await cmd_topics(update, context)
        else:
            await update.message.reply_text("💡 먼저 /topics 로 주제를 추천받으세요.")

    elif intent == "confirm":
        await update.message.reply_text(
            "💡 무엇을 진행할까요? 주제 이름이나 번호를 알려주세요."
        )

    elif intent == "cancel":
        # 항상 cancel 플래그 set — 진행 중인 파이프라인/순차 루프가 다음 체크포인트에서 멈춤
        # 이미지 생성 서브프로세스가 떠 있으면 즉시 kill 됨
        was_running = _task_lock.locked() or _current_proc is not None
        request_cancel()
        if session.get("recommendation"):
            session["recommendation"] = None
        if was_running:
            await update.message.reply_text(
                "🛑 중단 요청됨 — 현재 단계 종료 후 멈춥니다.\n"
                "(이미지 생성 중이면 프로세스도 종료됩니다)"
            )
        else:
            await update.message.reply_text("✅ 취소됐어요.")

    elif intent == "feedback_regen":
        slide_n = params.get("slide_n")
        feedback = params.get("feedback", "더 쉽게 설명해줘")
        # topic_hint 있으면 파일에서 검색, 없으면 session 사용
        topic_hint = params.get("topic_hint", "")
        last = find_topic_by_hint(topic_hint) if topic_hint else session.get("last_topic")
        if not last:
            await update.message.reply_text(
                "💡 어떤 카드뉴스를 수정할지 알려주세요.\n"
                "예) '신생아 배꼽관리 2번 슬라이드 수정해줘'"
            )
            return
        # 찾은 토픽을 session에 업데이트
        session["last_topic"] = last
        _save_session()
        await regen_single_slide(update, last, slide_n, feedback)

    elif intent == "verify_slide":
        slide_n = params.get("slide_n")
        # topic_hint 있으면 파일에서 검색, 없으면 session 사용
        topic_hint = params.get("topic_hint", "")
        last = find_topic_by_hint(topic_hint) if topic_hint else session.get("last_topic")
        if not last:
            await update.message.reply_text(
                "💡 어떤 카드뉴스를 수정할지 알려주세요.\n"
                "예) '신생아 배꼽관리 2번 슬라이드 수정해줘'"
            )
            return
        # 찾은 토픽을 session에 업데이트
        session["last_topic"] = last
        _save_session()
        await verify_single_slide(update, last, slide_n)

    elif intent == "edit_slide":
        slide_n = params.get("slide_n")
        instruction = params.get("instruction", "")
        # topic_hint 있으면 파일에서 검색, 없으면 session 사용
        topic_hint = params.get("topic_hint", "")
        last = find_topic_by_hint(topic_hint) if topic_hint else session.get("last_topic")
        if not last:
            await update.message.reply_text(
                "💡 어떤 카드뉴스를 수정할지 알려주세요.\n"
                "예) '신생아 배꼽관리 2번 슬라이드 수정해줘'"
            )
            return
        if not instruction:
            await update.message.reply_text(
                "💡 수정 내용을 구체적으로 알려주세요.\n"
                "예) '3번 슬라이드 38도 아니고 38.5도야'"
            )
            return
        # 찾은 토픽을 session에 업데이트
        session["last_topic"] = last
        _save_session()
        await regen_single_slide(update, last, slide_n, instruction)

    elif intent == "general_question":
        question = params.get("question", "")
        # topic_hint 있으면 파일에서 검색, 없으면 session 사용
        topic_hint = params.get("topic_hint", "")
        last = find_topic_by_hint(topic_hint) if topic_hint else session.get("last_topic")
        if last:
            # 찾은 토픽을 session에 업데이트
            session["last_topic"] = last
            _save_session()
        await answer_general_question(update, last, question)

    elif intent == "context_reference":
        slug = params.get("slug", "")
        last = session.get("last_topic")
        if last and (not slug or slug in last.get("slug", "")):
            await update.message.reply_text(
                f"📌 '{last['topic_kr']}' 카드뉴스를 말씀하시는 거죠?\n"
                f"수정이나 질문이 있으시면 말씀해주세요!\n"
                f"예) '4번 슬라이드 다시 만들어줘' / '8번 내용 맞아?'"
            )
        else:
            await update.message.reply_text(
                "💡 어떤 카드뉴스를 말씀하시는지 알려주세요."
            )

    elif intent == "edit_existing":
        slug_hint = params.get("slug_hint", "")
        instruction = params.get("instruction", "")

        # output/ 폴더에서 매칭되는 토픽 찾기 (파일 기반)
        matched = find_topic_by_hint(slug_hint)

        if matched:
            session["last_topic"] = matched
            _save_session()
            await update.message.reply_text(
                f"📌 '{matched['topic_kr']}' 카드뉴스로 설정했어요.\n"
                f"수정 내용: {instruction}\n"
                f"어떤 슬라이드를 수정할까요? (예: '2번 슬라이드 수정해줘')"
            )
        else:
            await update.message.reply_text(
                f"💡 '{slug_hint}' 카드뉴스를 찾지 못했어요.\n"
                f"output/ 폴더에 있는지 확인해주세요."
            )

    elif intent == "topics_by_tone":
        tone = params.get("tone", "")
        if tone:
            await cmd_topics_by_tone(update, context, tone)
        else:
            await update.message.reply_text(
                "🎨 어떤 스타일로 추천해드릴까요?\n\n"
                "1️⃣ 에디토리얼 모던 — 일반 케어·예방법\n"
                "2️⃣ 손그림 노트북 — 영아 돌봄·따뜻한 주제\n"
                "3️⃣ 클린 인포그래픽 — 수치·기준·비교\n"
                "4️⃣ 경고·긴급 — 응급·즉시 판단\n"
                "5️⃣ 캐릭터 일러스트 — 이유식·발달·성장\n\n"
                "예) '손그림 톤으로 추천해줘'"
            )

    elif intent == "keyword_topics":
        keyword = params.get("keyword", "")
        if keyword:
            await cmd_keyword_topics(update, context, keyword)
        else:
            await update.message.reply_text("💡 어떤 키워드로 찾을까요?")

    else:
        await update.message.reply_text(
            "💡 이렇게 말씀해보세요:\n"
            "  • '주제 추천해줘'\n"
            "  • '차멀미 예방법 만들어줘'\n"
            "  • '1번 만들어'\n"
            "  • '보류 목록 보여줘'\n"
            "  • '완료된 거 뭐야'"
        )


# ---------------------------------------------------------------- recommendation selection


async def apply_recommendation_selection(update: Update, nums: list[int]) -> None:
    recs = session.get("recommendation") or []
    if not recs:
        await update.message.reply_text("❓ 활성 추천이 없습니다. /topics 로 시작하세요.")
        return
    valid = sorted({n for n in nums if 1 <= n <= len(recs)})
    if not valid:
        await update.message.reply_text(f"❌ 유효한 번호가 없습니다 (1-{len(recs)}).")
        return

    selected_idx = valid
    leftover_idx = [i for i in range(1, len(recs) + 1) if i not in valid]
    selected = [recs[i - 1] for i in selected_idx]
    leftover = [recs[i - 1] for i in leftover_idx]

    data = load_topics()
    done_set = {t["slug"] for t in data["done"]}
    week_slugs = {t["slug"] for t in data["this_week"]}
    pending_slugs = {t["slug"] for t in data["pending"]}

    for r in selected:
        s = r.get("slug")
        if not s or s in done_set or s in week_slugs:
            continue
        data["pending"] = [t for t in data["pending"] if t["slug"] != s]
        data["this_week"].append(_clean_topic(r))
        week_slugs.add(s)

    for r in leftover:
        s = r.get("slug")
        if not s or s in done_set or s in week_slugs or s in pending_slugs:
            continue
        data["pending"].append(_clean_topic(r))
        pending_slugs.add(s)

    save_topics(data)
    session["recommendation"] = None

    lines = ["✅ 이번 주 확정:"]
    for n, r in zip(selected_idx, selected):
        lines.append(f"{n}. {r.get('title_kr','?')}")
    if leftover_idx:
        lines.append("")
        lines.append(f"📥 보류 이동: {', '.join(str(n) for n in leftover_idx)}번")
    lines.append("")
    lines.append("만들고 싶은 번호를 말씀해주세요!")
    lines.append("예) '1번 만들어' 또는 '여행 비상약 만들어줘'")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------- pending move/delete


async def move_pending_to_week(update: Update, nums: list[int]) -> None:
    data = load_topics()
    pending = data.get("pending", [])
    valid = sorted({n for n in nums if 1 <= n <= len(pending)})
    if not valid:
        await update.message.reply_text(f"❌ 유효한 번호가 없습니다 (1-{len(pending)}).")
        return
    week_slugs = {t["slug"] for t in data["this_week"]}
    moved_titles = []
    for n in sorted(valid, reverse=True):
        item = pending.pop(n - 1)
        if item.get("slug") in week_slugs:
            continue
        data["this_week"].append(item)
        week_slugs.add(item.get("slug"))
        moved_titles.append(item.get("title_kr", item.get("slug", "?")))
    save_topics(data)
    moved_titles.reverse()
    text = "✅ 이번 주로 이동:\n" + "\n".join(f"• {t}" for t in moved_titles)
    await update.message.reply_text(text)


async def delete_from_pending(update: Update, nums: list[int]) -> None:
    data = load_topics()
    pending = data.get("pending", [])
    valid = sorted({n for n in nums if 1 <= n <= len(pending)})
    if not valid:
        await update.message.reply_text(f"❌ 유효한 번호가 없습니다 (1-{len(pending)}).")
        return
    removed_titles = []
    for n in sorted(valid, reverse=True):
        item = pending.pop(n - 1)
        removed_titles.append(item.get("title_kr", item.get("slug", "?")))
    save_topics(data)
    removed_titles.reverse()
    text = "🗑️ 삭제:\n" + "\n".join(f"• {t}" for t in removed_titles)
    await update.message.reply_text(text)


# ---------------------------------------------------------------- auto pipeline


def _local_slug_fallback(topic_kr: str) -> str:
    """API 미사용·실패 시 로컬 슬러그 생성.
    특수문자 제거 → 공백을 하이픈으로 → 영문·숫자·하이픈만 유지.
    결과가 비면 'card-YYYYMMDD-NNN' 타임스탬프 슬러그 + 같은 날 카운터 증가.
    """
    cleaned = re.sub(r"[?!‼⁉！？,，·~%&#@]", "", topic_kr)
    cleaned = re.sub(r"\s+", "-", cleaned.strip())
    cleaned = re.sub(r"[^a-z0-9-]", "", cleaned.lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if cleaned:
        return cleaned

    prefix = f"card-{datetime.now().strftime('%Y%m%d')}-"
    next_num = 1
    try:
        data = load_topics()
        used_nums = []
        for key in ("done", "this_week", "pending"):
            for item in data.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug", "")
                if isinstance(slug, str) and slug.startswith(prefix):
                    m = re.match(r".*-(\d+)$", slug)
                    if m:
                        used_nums.append(int(m.group(1)))
        if used_nums:
            next_num = max(used_nums) + 1
    except Exception as e:  # noqa: BLE001
        log.debug("_local_slug_fallback 카운터 조회 실패: %s", e)
    return f"{prefix}{next_num:03d}"


async def korean_to_slug(topic_kr: str) -> str:
    """한국어 토픽을 영문 케밥-케이스 슬러그로 변환 (Claude API). 실패 시 로컬 fallback."""
    if Anthropic is None:
        return _local_slug_fallback(topic_kr)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _local_slug_fallback(topic_kr)
    try:
        return await asyncio.to_thread(_korean_to_slug_sync, topic_kr, api_key)
    except Exception as e:  # noqa: BLE001
        log.exception("korean_to_slug 실패: %s", e)
        return _local_slug_fallback(topic_kr)


def _korean_to_slug_sync(topic_kr: str, api_key: str) -> str:
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=200,
        system=(
            "한국어 토픽을 영문 소문자 케밥-케이스 슬러그로 변환하세요. "
            "슬러그만 출력. 다른 텍스트·따옴표·설명 금지. "
            "예: '차멀미 예방법' → car-sickness, '아이 수면 시간' → sleep-duration"
        ),
        messages=[{"role": "user", "content": topic_kr}],
    )
    raw = _collect_text(msg).strip()
    m = re.search(r"[a-z][a-z0-9-]+", raw)
    return m.group(0) if m else _local_slug_fallback(topic_kr)


def generate_sources(
    topic_kr: str, slug: str, today: str, api_key: str
) -> Optional[dict]:
    """Claude API + web_search 로 sources.json 초안 생성."""
    client = Anthropic(api_key=api_key)
    system = """당신은 소아과언니 카드뉴스 의학 리서처입니다.

규칙:
- Tier 1: AAP CPG, 대한소아청소년과학회, 질병관리청, Nelson Textbook, UpToDate, WHO
- Tier 2: PubMed peer-reviewed 논문, AAFP, CDC (Tier 1 부족 시)
- Tier 3 이하 (사용 금지): 일반 병원 의학정보 사이트, 블로그, 위키, 뉴스기사
  - 예시: 서울대학교병원 의학정보, 서울아산병원 건강정보, 네이버 건강, 헬스조선 등
  - 한국 출처는 반드시 대한소아청소년과학회 또는 질병관리청만 Tier 1 허용
  - 그 외 한국 출처는 모두 Tier 3 이하로 분류하고 사용 금지
- StatPearls, MedScape, MedlinePlus, eMedicine 등 reference compilation 사이트:
  내용 신뢰 가능하지만 정식 출처 아님 → Tier 2 분류
- 학술지 논문 (NEJM, JAMA, Lancet, BMJ 등 peer-reviewed 저널):
  항상 Tier 2 분류 (아무리 유명해도 Tier 1 아님)
- url 필드 규칙: 반드시 출처 publisher 정식 URL만 허용
  · AAP 논문 → publications.aap.org 또는 pubmed.ncbi.nlm.nih.gov
  · Nelson Textbook → clinicalkey.com 또는 elsevier.com
  · 질병관리청 → kdca.go.kr
  · 대한소아청소년과학회 → pediatrics.or.kr
  · WHO → who.int
  · secondary mirror (Medscape, MedlinePlus, emedicine 등) URL 사용 금지
- 출처 없는 사실 포함 금지

sources.json 형식으로만 응답. JSON 외 텍스트 금지.
형식:
{
  "topic": "<slug>",
  "topic_kr": "<한국어 제목>",
  "palette": "A 또는 B",
  "compiled_at": "<오늘 날짜>",
  "claims": [
    {
      "claim_id": "C01",
      "slide_n": 2,
      "claim_text": "...",
      "source": {
        "tier": 1,
        "type": "guideline",
        "title": "...",
        "authors": ["..."],
        "publication": "...",
        "publication_date": "...",
        "applicable_age": "...",
        "url": "...",
        "last_accessed_at": "<오늘 날짜>"
      },
      "writer_used": false,
      "reviewer_pass": null
    }
  ],
  "verification": {
    "tier_1_count": 0,
    "tier_2_count": 0,
    "total_claims": 0,
    "notes": ""
  }
}

팔레트 결정: "잘못 적용 시 즉각적 위험?" YES→B / NO→A"""
    user = f"""토픽: {topic_kr}
슬러그: {slug}
오늘 날짜: {today}

이 토픽으로 인스타 카드뉴스 9장 sources.json을 작성해줘.
슬라이드 구성:
- slide 1: 커버 (claim 없음)
- slide 2-8: 본문 (각 슬라이드당 1-2개 claim)
- slide 9: 아우트로 (claim 없음)

웹 검색으로 최신 가이드라인 확인 후 작성."""
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        system=system,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user}],
    )
    raw = _collect_text(msg)
    parsed = _parse_json_object(raw)
    if parsed is None:
        print(
            f"[DEBUG] generate_sources 파싱 실패. raw 응답: {raw[:500]}",
            flush=True,
        )
    result = parsed if parsed else None
    if isinstance(result, list):
        result = {"claims": result}
    return result


def verify_sources(sources, topic_kr: str, api_key: str) -> dict:
    """Claude API + web_search 로 sources.json 자동 검증·수정."""
    # sources가 list로 들어오는 경우 처리
    if isinstance(sources, list):
        sources = {"claims": sources}
    client = Anthropic(api_key=api_key)
    system = """당신은 소아청소년과 전문의 수준의 의학 검증자입니다.

각 claim을 원전과 대조하여:
1. 수치 정확성 확인 (온도·용량·연령 기준)
2. 표현의 과장·단순화 여부
3. 출처와 claim_text 일치 여부
오류 발견 시 자동 수정.

검증 완료된 sources.json을 JSON으로만 반환. JSON 외 텍스트 금지.
reviewer_pass 필드는 건드리지 말 것."""
    user = (
        "다음 sources.json을 검증하고 오류가 있으면 수정해서 반환해줘.\n"
        "웹 검색으로 각 claim의 출처 원전 확인.\n\n"
        + json.dumps(sources, ensure_ascii=False)
    )
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        system=system,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user}],
    )
    parsed = _parse_json_object(_collect_text(msg))
    result = parsed if parsed else sources
    if isinstance(result, list):
        result = {"claims": result}
    return result


async def generate_template(
    sources: dict, topic_kr: str, slug: str, api_key: str, forced_tone: str = ""
) -> tuple[Optional[dict], str]:
    """톤 자동 선택 + sources 기반으로 9장 슬라이드 템플릿 생성. (template, tone_name) 반환.

    톤 결정: 사용자 요청(forced_tone, 한글 별칭 포함) 우선 → 키워드 기반 decide_tone()
    → DEFAULT_TONE. (Claude 기반 select_tone 은 보조용으로 보존만)"""
    tone_name = decide_tone(topic_kr, slug, forced_tone or None)
    tone_path = REPO_ROOT / "knowledge" / "tone" / f"{tone_name}.md"
    tone_content = tone_path.read_text(encoding="utf-8") if tone_path.exists() else ""

    template = await asyncio.to_thread(
        _generate_template_sync, sources, topic_kr, slug, tone_name, tone_content, api_key
    )
    return template, tone_name


def _generate_template_sync(
    sources: dict,
    topic_kr: str,
    slug: str,
    tone_name: str,
    tone_content: str,
    api_key: str,
) -> Optional[dict]:
    client = Anthropic(api_key=api_key)
    palette = decide_palette(sources, topic_kr, slug)

    tone_template_path = REPO_ROOT / "knowledge" / "tone-templates" / f"{tone_name}.md"
    tone_template = (
        tone_template_path.read_text(encoding="utf-8")
        if tone_template_path.exists()
        else ""
    )

    system = """당신은 소아과언니 카드뉴스 슬라이드 프롬프트 작성자입니다.

규칙:
- sources.json에 없는 사실 절대 사용 금지
- 9장 모두 Portrait 1080x1350
- 시그니쳐 "소아과언니" — 우상단, 따옴표 없이 4글자만
- 숫자+px 텍스트 카드에 표시 금지
- 각 슬라이드 일러스트/아이콘 1개 이상

반드시 아래 정확한 JSON 스키마로만 응답. JSON 외 텍스트 금지.
다른 스키마 절대 사용 금지.

{
  "topic": "<영문 슬러그>",
  "topic_kr": "<한국어 제목>",
  "palette": "A 또는 B",
  "tone": "",
  "common_style": "",
  "output_dir": "output/<영문 슬러그>",
  "image_size": "1080x1350",
  "image_orientation": "portrait",
  "slides": [
    {
      "n": 1,
      "role": "cover",
      "prompt": "Portrait 1080x1350. [영문 프롬프트 전문]"
    },
    {
      "n": 2,
      "role": "body",
      "prompt": "Portrait 1080x1350. [영문 프롬프트 전문]"
    }
    ...9개까지
  ]
}

필수 규칙:
- slides 배열의 각 항목은 반드시 n, role, prompt 3개 키만 사용
- n은 1~9 정수
- prompt는 반드시 영문으로 작성
- prompt 첫 단어는 반드시 "Portrait"
- 시그니쳐: Top-right 소아과언니 (따옴표 없이 4글자만)
- 숫자+px 텍스트 카드에 표시 금지
- common_style·tone 필드는 시스템이 자동으로 채우니 빈 문자열("")로 두면 됨"""
    user = f"""토픽: {topic_kr}
슬러그: {slug}
팔레트: {palette}
선택된 톤: {tone_name}
톤 가이드:
{tone_content}

sources.json:
{json.dumps(sources, ensure_ascii=False)}

위 톤 스타일로 9장 슬라이드 프롬프트 JSON 작성해줘.

---
좋은 prompt 예시 (이 품질로 작성해줘):

slide n:1 cover 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right corner: 소아과언니 in deep navy #1A1F36 Pretendard SemiBold — 4 characters only, no quotes, no decoration. Top-left small gray: PARENTING NOTE · 010. Left side upper area: very large bold Korean headline two lines — first line [토픽 관련 명사] in deep navy, second line [핵심 단어] in coral #C44536. Below headline: small muted gray subtitle [부제] · AAP 권장 기준. Right side center-bottom: large clean editorial illustration related to topic, line art style in coral and teal, minimal clean strokes. Bottom area: thin coral horizontal accent line. No pixel values as text on card. No quotes on card."

slide n:3-8 body 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right: 소아과언니 navy small — 4 characters only, no quotes, no decoration. Top-left small coral rounded label: [섹션명]. Large bold headline: [앞부분] navy, [마지막 핵심 단어] coral #C44536. Center: clean editorial illustration related to content, line art coral/navy minimal. Below: [카드 or 리스트 구조]. Bottom thin coral divider. No pixel text on card. No quotes on card."

slide n:9 outro 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right: 소아과언니 navy small — 4 characters only, no quotes, no decoration. Top-center small gray: SAVE & SHARE. Large bold headline center: 오늘 확인하고 navy, 저장하세요 coral #C44536 with thin coral underline. Source box light gray #F0F0EE rounded: 출처 [4개 출처 목록]. Horizontal thin navy divider. Center: 소아과언니 navy bold large — NO quotes NO decoration. Small gray DR.SOA UNNIE · 소아청소년과 전문의. Horizontal divider. @dr.soa_unnie · 매주 새 가이드 navy. [앱 CTA] coral. No pixel text. No quotes anywhere."
---

검증된 prompt 패턴 (반드시 이 패턴 따를 것):
{tone_template}"""
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    result = _parse_json_object(_collect_text(msg))
    if isinstance(result, list):
        result = {"slides": result}

    # common_style(디자인 DNA) 강제 주입 — Claude 가 비우거나 변형해도 항상 정규값 보장.
    # 톤별 디자인 DNA(tone-templates/{tone}.md)를 common_style 로 사용.
    if isinstance(result, dict):
        result["palette"] = palette
        result["tone"] = tone_name
        injected = common_style_for_tone(tone_name)
        if injected:
            result["common_style"] = injected
        elif not result.get("common_style"):
            log.warning(
                "common_style 주입값이 비어 있음 — tone-templates/%s.md 확인 필요", tone_name
            )
    return result


async def auto_pipeline(
    update: Update, topic_kr: str, slug: str, forced_tone: str = ""
) -> None:
    """자동 파이프라인: sources → verify → template → image."""
    if Anthropic is None:
        await update.message.reply_text("❌ anthropic 패키지 미설치.")
        return
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        await update.message.reply_text("❌ ANTHROPIC_API_KEY 가 비어있습니다.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"🚀 자동 파이프라인 시작 — '{topic_kr}' (slug: {slug})"
    )

    if await _check_cancel(update):
        return

    # STEP 1: sources.json 생성
    await update.message.reply_text("⏳ 의학 출처 리서치 중...")
    try:
        sources = await asyncio.to_thread(
            generate_sources, topic_kr, slug, today, api_key
        )
    except Exception as e:  # noqa: BLE001
        log.exception("generate_sources 실패")
        await update.message.reply_text(f"❌ 리서치 실패: {e}")
        return
    if not sources:
        await update.message.reply_text("❌ 리서치 응답을 파싱하지 못했습니다.")
        return

    if await _check_cancel(update):
        return

    # STEP 2: 의학 검증
    await update.message.reply_text("🔍 의학 내용 자동 검증 중...")
    try:
        verified_sources = await asyncio.to_thread(
            verify_sources, sources, topic_kr, api_key
        )
    except Exception as e:  # noqa: BLE001
        log.exception("verify_sources 실패")
        await update.message.reply_text(
            f"⚠️ 검증 실패, 미검증 sources 로 진행: {e}"
        )
        verified_sources = sources

    sources_path = OUTPUT_DIR / slug / "sources.json"
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(
        json.dumps(verified_sources, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    n_claims = len(verified_sources.get("claims", []))
    await update.message.reply_text(f"✓ sources.json 저장 ({n_claims}개 claim)")

    if await _check_cancel(update):
        return

    # STEP 3: template 생성 (톤 자동 선택 포함)
    try:
        template, tone_name = await generate_template(
            verified_sources, topic_kr, slug, api_key, forced_tone=forced_tone
        )
    except Exception as e:  # noqa: BLE001
        log.exception("generate_template 실패")
        await update.message.reply_text(f"❌ 템플릿 생성 실패: {e}")
        return
    if not template:
        await update.message.reply_text("❌ 템플릿 응답을 파싱하지 못했습니다.")
        return
    await update.message.reply_text(f"🎨 슬라이드 구성 중... (톤: {tone_name})")

    tpl_path = TEMPLATES_DIR / f"slides.{slug}.json"
    tpl_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    n_slides = len(template.get("slides", []))
    await update.message.reply_text(f"✓ 템플릿 저장 ({n_slides}장 슬라이드)")

    if await _check_cancel(update):
        return

    # STEP 4: 이미지 생성
    await update.message.reply_text("🖼️ 이미지 생성 중... (약 4분, 중간에 '중단' 가능)")
    await trigger_generation_direct(update, slug)

    if is_cancel_requested():
        # 이미지 생성 단계에서 중단된 경우 — 캡션 생략하고 종료
        return

    # 세션에 현재 토픽 저장 (피드백·검증·질문 대화용)
    session["last_topic"] = {
        "slug": slug,
        "topic_kr": topic_kr,
        "sources_path": str(REPO_ROOT / "output" / slug / "sources.json"),
        "template_path": str(TEMPLATES_DIR / f"slides.{slug}.json"),
        "output_dir": str(OUTPUT_DIR / slug),
    }
    _save_session()
    print(f"[DEBUG] session 파일 저장됨: {slug}", flush=True)

    # 캡션 생성 + 저장
    try:
        sources_path_p = Path(session["last_topic"]["sources_path"])
        sources_data = (
            json.loads(sources_path_p.read_text())
            if sources_path_p.exists()
            else {}
        )
        if isinstance(sources_data, list):
            sources_data = {"claims": sources_data}
        caption = await asyncio.to_thread(
            generate_caption, topic_kr, slug, sources_data
        )
        caption_path = OUTPUT_DIR / slug / "caption.txt"
        caption_path.write_text(caption, encoding="utf-8")
        # 텔레그램으로 캡션 전송
        await update.message.reply_text(
            f"📝 인스타 캡션:\n\n{caption}",
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ 캡션 생성 실패: {e}")


# ---------------------------------------------------------------- caption generator


def generate_caption(topic_kr: str, slug: str, sources: dict) -> str:
    """Claude API로 인스타 캡션 생성 후 파일 저장."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    client = Anthropic(api_key=api_key)

    claims = sources.get("claims", [])
    key_points = [c["claim_text"] for c in claims[:5]]
    key_points_text = "\n".join(f"- {p}" for p in key_points)

    # 앱 CTA 매칭
    app_cta_map = {
        "해열제": "💊 해열제 용량이 헷갈리면?\n소아과수첩 앱 → 해열제 계산기",
        "발열": "💊 해열제 용량이 헷갈리면?\n소아과수첩 앱 → 해열제 계산기",
        "응급": "🏥 야간에 아이가 아프면?\n소아과수첩 앱 → 야간·달빛병원 바로 찾기",
        "야간": "🏥 야간에 아이가 아프면?\n소아과수첩 앱 → 야간·달빛병원 바로 찾기",
        "경련": "🏥 응급 상황에서는?\n소아과수첩 앱 → 야간·달빛병원 바로 찾기",
        "성장": "📈 우리 아이 잘 크고 있나요?\n소아과수첩 앱 → 성장 기록",
        "이유식": "📈 이유식 시기 체크하고 싶다면?\n소아과수첩 앱 → 성장 기록",
        "발달": "📈 발달이정표 궁금하다면?\n소아과수첩 앱 → 성장 기록",
        "약": "📷 약봉투 사진으로 복약 기록\n소아과수첩 앱 → 약봉투 스캔",
        "처방": "📷 처방약 기록해두세요\n소아과수첩 앱 → 약봉투 스캔",
        "진료": "📝 진료 전 질문 미리 메모\n소아과수첩 앱 → 진료 메모",
        "여행": "📝 여행 전 소아과 상담 메모\n소아과수첩 앱 → 진료 메모",
    }
    app_cta = "📝 진료 전 질문은 미리 메모해두세요\n소아과수첩 앱 → 진료 메모"
    for keyword, cta in app_cta_map.items():
        if keyword in topic_kr:
            app_cta = cta
            break

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1000,
        system="""소아과언니 인스타그램 캡션 작성자.

브랜드 룰:
- 작성자: 소아청소년과 전문의 (소아과 전문의 ❌)
- 독자 호칭: 부모님들 (엄마들/여러분 ❌)
- 톤: 따뜻하고 전문적, 부모 공감에서 시작
- 실명/병원/지역 노출 금지
- 이모지 적절히 사용
- 길이: 200-300자 본문 + 해시태그

캡션 구조:
1. 공감 첫 줄 (부모님 상황 공감)
2. 핵심 내용 2-3줄
3. 저장 권유
4. [앱 CTA는 별도로 추가됨]
5. 해시태그 10개
   - 필수: #소아과언니 #소아청소년과전문의
   - 토픽 관련 5-6개
   - 육아 일반 2-3개

본문만 작성. 앱 링크/인스타 링크/블로그 링크는 포함하지 말 것 (별도 추가됨).
""",
        messages=[{
            "role": "user",
            "content": f"""
토픽: {topic_kr}
핵심 내용:
{key_points_text}

위 내용으로 인스타 캡션 작성해줘.
""",
        }],
    )

    body = response.content[0].text.strip()

    # 최종 캡션 조합
    caption = f"""{body}

─────────────────
{app_cta}

{APP_LINKS}

📷 인스타그램
{INSTA_URL}

🔍 더 많은 소아과 정보
{BLOG_URL}"""

    return caption


# ---------------------------------------------------------------- per-slide feedback / verify / Q&A


async def regen_single_slide(
    update, last_topic: dict, slide_n: int, instruction: str
):
    """특정 슬라이드만 재생성해서 전송."""
    slug = last_topic["slug"]
    topic_kr = last_topic["topic_kr"]
    template_path_str = last_topic["template_path"]
    output_dir = last_topic["output_dir"]

    await update.message.reply_text(
        f"🔄 slide-{slide_n:02d} 재생성 중...\n피드백: {instruction}"
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")

    def _regen_sync():
        import json as _json
        client = Anthropic(api_key=api_key)

        # 기존 template 읽기
        with open(template_path_str) as f:
            template = _json.load(f)

        # 해당 슬라이드 찾기
        slide = next(
            (s for s in template.get("slides", []) if s.get("n") == slide_n),
            None,
        )
        if not slide:
            return None, "슬라이드를 찾을 수 없어요."

        # sources.json 읽기
        sources_path = last_topic["sources_path"]
        sources_content = ""
        if Path(sources_path).exists():
            with open(sources_path) as f:
                sources_content = _json.dumps(
                    _json.load(f), ensure_ascii=False
                )

        # 새 prompt 생성
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            system="""소아과언니 카드뉴스 슬라이드 프롬프트 수정자.
기존 prompt를 피드백에 맞게 수정해서 반환.
수정된 prompt 텍스트만 반환. 다른 텍스트 금지.
규칙:
- Portrait 1080x1350 유지
- 소아과언니 우상단 시그니쳐 유지 (따옴표 없이)
- 톤과 컬러 시스템 유지
- sources.json에 없는 새 사실 추가 금지
- 픽셀값 텍스트 금지""",
            messages=[{
                "role": "user",
                "content": f"""
토픽: {topic_kr}
슬라이드: {slide_n}번
피드백/수정 지시: {instruction}

기존 prompt:
{slide['prompt']}

sources.json:
{sources_content[:2000]}

위 피드백을 반영해서 수정된 prompt만 반환해줘.
""",
            }],
        )

        new_prompt = response.content[0].text.strip()

        # template 업데이트
        slide["prompt"] = new_prompt
        with open(template_path_str, "w") as f:
            _json.dump(template, f, ensure_ascii=False, indent=2)

        return new_prompt, None

    new_prompt, error = await asyncio.to_thread(_regen_sync)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    # 해당 슬라이드만 나노바나나로 재생성
    cmd = [
        sys.executable,
        str(NANO_GEN),
        "--topic", slug,
        "--slides", template_path_str,
        "--slide-n", str(slide_n),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)

    # 해당 슬라이드 PNG 전송
    png = Path(output_dir) / f"slide-{slide_n:02d}.png"
    if png.exists():
        with png.open("rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"✅ slide-{slide_n:02d} 재생성 완료",
            )
    else:
        await update.message.reply_text("❌ 재생성 실패. 로그 확인해주세요.")


async def verify_single_slide(update, last_topic: dict, slide_n: int):
    """해당 슬라이드의 의학 내용을 sources.json으로 확인해서 답변."""

    def _verify_sync():
        import json as _json
        api_key = os.getenv("ANTHROPIC_API_KEY")
        client = Anthropic(api_key=api_key)

        sources_path = last_topic["sources_path"]
        if not Path(sources_path).exists():
            return "sources.json을 찾을 수 없어요."

        with open(sources_path) as f:
            sources = _json.load(f)

        # 해당 슬라이드 claims 추출
        slide_claims = [
            c for c in sources.get("claims", [])
            if c.get("slide_n") == slide_n
        ]

        if not slide_claims:
            return f"slide-{slide_n:02d}에 연결된 출처 정보가 없어요."

        claims_text = _json.dumps(slide_claims, ensure_ascii=False, indent=2)

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            system="""소아청소년과 전문의 수준의 의학 검증자.
sources.json의 claim을 바탕으로 슬라이드 내용의 정확성을 확인.
한국어로 간결하게 답변. 2-3문장 이내.""",
            messages=[{
                "role": "user",
                "content": f"""
slide-{slide_n}번의 출처 정보:
{claims_text}

이 슬라이드의 의학 내용이 정확한지 확인해줘.
출처와 함께 간결하게 답변해줘.
""",
            }],
        )
        return response.content[0].text.strip()

    await update.message.reply_text(f"🔍 slide-{slide_n:02d} 의학 내용 확인 중...")
    answer = await asyncio.to_thread(_verify_sync)
    await update.message.reply_text(f"📋 slide-{slide_n:02d} 검증 결과:\n\n{answer}")


async def answer_general_question(
    update, last_topic: Optional[dict], question: str
):
    """현재 토픽 컨텍스트로 일반 질문에 답변."""

    def _answer_sync():
        import json as _json
        api_key = os.getenv("ANTHROPIC_API_KEY")
        client = Anthropic(api_key=api_key)

        context = ""
        if last_topic and Path(last_topic["sources_path"]).exists():
            with open(last_topic["sources_path"]) as f:
                sources = _json.load(f)
            context = f"현재 작업 중인 카드뉴스: {last_topic['topic_kr']}\n"
            context += (
                "출처: "
                + _json.dumps(
                    sources.get("claims", [])[:3], ensure_ascii=False
                )
            )

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            system="""소아청소년과 전문의 관점에서 답변.
의학적으로 정확하고 부모가 이해하기 쉽게.
한국어로 간결하게 3-5문장 이내.""",
            messages=[{
                "role": "user",
                "content": f"{context}\n\n질문: {question}",
            }],
        )
        return response.content[0].text.strip()

    answer = await asyncio.to_thread(_answer_sync)
    await update.message.reply_text(f"💡 {answer}")


# ---------------------------------------------------------------- tone selection


def load_tone_history() -> dict:
    path = REPO_ROOT / "data" / "tone_history.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"recent": []}


def save_tone_history(data: dict) -> None:
    path = REPO_ROOT / "data" / "tone_history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


VALID_TONES = [
    "editorial-modern",
    "handdrawn-notebook",
    "clean-infographic",
    "emergency-alert",
    "character-illustration",
    "dark-magazine",
    "pastel-gradient",
    "sticker-pop",
    "bold-typography",
]

TONE_GUIDE = """
9가지 톤과 어울리는 토픽 유형:
1. editorial-modern — 일반 케어·예방법·여행·생활건강·약물 안전
2. handdrawn-notebook — 영아 돌봄·수유·산통·야간 육아·따뜻한 주제
3. clean-infographic — 가벼운 비교표·일반 예방접종·생활 통계
4. emergency-alert — 아나필락시스·경련 대처·즉시 119 신호 등 위급
5. character-illustration — 귀여운 영아 일반·캐릭터로 풀어내는 따뜻한 주제
6. dark-magazine — 소아심장·선천성 질환, 성장 백분위·표준편차, 희귀질환, 수치·데이터 중심 전문 정보 (신호어: 백분위, 표준편차, 선천성, 심장, 가와사키, 통계, 기준치)
7. pastel-gradient — 수면, 모유수유, 신생아 케어, 예방접종, 부드러운 육아 정보 (신호어: 수면, 수유, 신생아, 태아, 임신, 백신, 돌봄, 안정)
8. sticker-pop — 이유식, 간식, 놀이발달, 월령별 체크리스트, 경쾌하고 밝은 생활 육아 (신호어: 이유식, 간식, 놀이, 발달, 월령, 체크리스트, 처음, 시작)
9. bold-typography — 증상 한 가지 집중 설명, 응급 기준 수치, 즉각 판단 (신호어: 열, 경련, 발진, 기침, 설사, 응급, 즉시, 언제 병원)

스타일 선택 우선순위:
1. 토픽 키워드가 6~9번 신호어와 일치하면 해당 신규 양식 우선 선택
2. 복수 일치 시: 데이터 비중이 높으면 dark-magazine, 감성·부드러움이 강하면 pastel-gradient
3. 최근 사용 톤 피해서 다양성 유지 (직전 3개와 같은 양식 반복 금지)
"""


async def select_tone(topic_kr: str, slug: str) -> str:
    """토픽에 맞는 톤을 Claude API 로 선택 + 최근 이력으로 다양성 유지."""
    history = load_tone_history()
    recent_tones = history.get("recent", [])
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or Anthropic is None:
        for t in VALID_TONES:
            if t not in recent_tones[-3:]:
                tone_name = t
                break
        else:
            tone_name = VALID_TONES[0]
    else:
        try:
            tone_name = await asyncio.to_thread(
                _select_tone_sync, topic_kr, slug, recent_tones, api_key
            )
        except Exception as e:  # noqa: BLE001
            log.exception("select_tone API 실패: %s", e)
            tone_name = next(
                (t for t in VALID_TONES if t not in recent_tones[-3:]),
                VALID_TONES[0],
            )

    if tone_name not in VALID_TONES:
        tone_name = next(
            (t for t in VALID_TONES if t not in recent_tones[-3:]), VALID_TONES[0]
        )

    recent_tones.append(tone_name)
    history["recent"] = recent_tones[-5:]
    save_tone_history(history)
    return tone_name


def _select_tone_sync(
    topic_kr: str, slug: str, recent_tones: list, api_key: str
) -> str:
    client = Anthropic(api_key=api_key)
    recent_str = ", ".join(recent_tones[-3:]) if recent_tones else "없음"
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=50,
        system="소아과언니 카드뉴스 디자인 디렉터. 톤 파일명만 반환. 다른 텍스트 금지.",
        messages=[
            {
                "role": "user",
                "content": (
                    f"토픽: {topic_kr}\n"
                    f"슬러그: {slug}\n"
                    f"최근 사용 톤(피할 것): {recent_str}\n\n"
                    f"{TONE_GUIDE}\n\n가장 어울리는 톤 파일명만 반환:"
                ),
            }
        ],
    )
    raw = _collect_text(response).strip().lower()
    m = re.search(r"[a-z][a-z0-9-]+", raw)
    return m.group(0) if m else ""


# ---------------------------------------------------------------- generator pipeline


async def trigger_generation(update: Update, slug: str) -> None:
    tpl = template_path(slug)
    if not tpl.exists():
        await update.message.reply_text(
            f"❌ 템플릿 없음: templates/slides.{slug}.json\n"
            f"   먼저 템플릿을 생성해주세요."
        )
        return

    if _task_lock.locked():
        await update.message.reply_text(
            f"⏳ 이미 다른 작업 진행 중입니다 ({current_task['topic']}).\n"
            f"   /status 로 확인하세요."
        )
        return

    async with _task_lock:
        current_task.update(
            topic=slug, started_at=time.time(), status="running", error=None
        )
        await update.message.reply_text(
            f"⏳ 카드뉴스 생성 시작합니다: {slug}\n"
            f"   예상 시간 약 4분, 비용 약 500-1,000원."
        )
        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            ok, log_tail = await run_generator(slug)
        except asyncio.TimeoutError:
            current_task["status"] = "failed"
            current_task["error"] = "timeout"
            await update.message.reply_text(
                f"❌ 생성 타임아웃 ({GEN_TIMEOUT_SEC}초). 로그 확인해주세요."
            )
            current_task["status"] = "idle"
            return
        except Exception as e:  # noqa: BLE001
            current_task["status"] = "failed"
            current_task["error"] = str(e)
            log.exception("generator 실패")
            await update.message.reply_text(
                f"❌ 생성 실패: {e}\n로그 확인해주세요."
            )
            current_task["status"] = "idle"
            return

        if not ok:
            current_task["status"] = "failed"
            await update.message.reply_text(
                f"❌ 생성 실패. 마지막 로그:\n```\n{log_tail}\n```",
                parse_mode="Markdown",
            )
            current_task["status"] = "idle"
            return

        current_task["status"] = "sending"
        await update.message.reply_text(
            f"✅ 완료! output/{slug}/ 에 저장됐어요. 9장 전송합니다."
        )
        await send_slides(update, slug)

        try:
            title = title_for_slug(slug)
            add_done(slug, title)
            await update.message.reply_text(f"📌 '{slug}' 완료 목록에 기록됐어요.")
        except Exception as e:  # noqa: BLE001
            log.exception("add_done 실패")
            await update.message.reply_text(f"⚠️ done 기록 실패: {e}")

        # 주제 이력 자동 등록 (used-topics.json) — 실패해도 파이프라인 영향 없음
        await record_used_topic_on_done(slug)

        # 인스타 9장 완료 → 블로그 글 자동 생성 (실패해도 인스타 결과는 보존)
        await run_blog_generator(update, slug)

        # 블로그 생성 직후 → 릴스 보강 컷 자동 생성 (실패해도 파이프라인 중단 안 함)
        try:
            await asyncio.to_thread(run_reels_supplement, slug)
        except Exception as e:  # noqa: BLE001
            log.warning(f"reels-supplement 호출 실패 (무시): {e}")

        current_task["status"] = "idle"


async def run_blog_generator(update: Update, slug: str) -> None:
    """인스타 9장 PNG 생성 직후 → 블로그 글도 자동 생성.

    실패해도 인스타 작업은 이미 끝났으므로 예외를 막고 메시지로만 알린다.
    """
    if not BLOG_GEN_JS.exists():
        log.info(f"블로그 생성 스킵 — generate-blog.js 없음: {BLOG_GEN_JS}")
        return

    await update.message.reply_text(
        f"📝 블로그 글도 같이 만들고 있어요... ({slug})"
    )

    env = os.environ.copy()
    env["INSTA_ROOT"] = str(REPO_ROOT)

    cmd = ["node", str(BLOG_GEN_JS), slug, "--notify"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BLOG_BOT_ROOT),
            env=env,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=BLOG_GEN_TIMEOUT_SEC
        )
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        tail = "\n".join(out.strip().splitlines()[-10:])

        if proc.returncode == 0:
            log.info(f"블로그 생성 완료 — {slug}")
            # generate-blog.js 가 --notify 로 직접 텔레그램 발송하므로 여기선 조용히.
        else:
            await update.message.reply_text(
                f"⚠️ 블로그 생성 실패 (인스타는 완료). 마지막 로그:\n```\n{tail or '(없음)'}\n```",
                parse_mode="Markdown",
            )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"⚠️ 블로그 생성 타임아웃 ({BLOG_GEN_TIMEOUT_SEC}초). 인스타는 정상 완료됐어요."
        )
    except Exception as e:  # noqa: BLE001
        log.exception("블로그 생성 호출 실패")
        await update.message.reply_text(
            f"⚠️ 블로그 생성 호출 오류: {e}\n인스타는 정상 완료됐어요."
        )


def run_reels_supplement(slug: str):
    """reels-supplement.py를 백그라운드로 실행"""
    import subprocess
    script_path = REPO_ROOT / "scripts" / "reels-supplement.py"
    if not script_path.exists():
        print(f"[reels-supplement] 스크립트 없음: {script_path}")
        return
    print(f"[reels-supplement] 시작: {slug}")
    try:
        result = subprocess.run(
            ["python3", str(script_path), "--topic", slug],
            cwd=str(REPO_ROOT),
            timeout=600,  # 10분 타임아웃
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"[reels-supplement] 완료: {slug}")
        else:
            print(f"[reels-supplement] 오류: {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        print(f"[reels-supplement] 타임아웃 (10분 초과): {slug}")
    except Exception as e:
        print(f"[reels-supplement] 실행 실패: {e}")


async def run_generator(slug: str) -> tuple[bool, str]:
    cmd = [
        sys.executable,
        str(NANO_GEN),
        "--topic",
        slug,
        "--slides",
        str(template_path(slug)),
    ]
    global _current_proc
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    _current_proc = proc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GEN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        _current_proc = None
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    log_tail = "\n".join(out.strip().splitlines()[-15:])
    if is_cancel_requested():
        return False, "사용자 요청으로 중단됨"
    return proc.returncode == 0, log_tail


async def send_slides(update: Update, slug: str) -> None:
    folder = OUTPUT_DIR / slug
    for i in range(1, SLIDE_COUNT + 1):
        png = folder / f"slide-{i:02d}.png"
        if not png.exists():
            await update.message.reply_text(f"⚠️ slide-{i:02d}.png 없음 — 건너뜀")
            continue
        with png.open("rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{slug} · {i:02d}/09")


async def trigger_generation_direct(update: Update, slug: str) -> None:
    """auto_pipeline 전용 — 템플릿 존재 체크 생략 (방금 생성한 직후)."""
    if _task_lock.locked():
        await update.message.reply_text(
            f"⏳ 이미 다른 작업 진행 중입니다 ({current_task['topic']}). /status 로 확인하세요."
        )
        return

    async with _task_lock:
        current_task.update(
            topic=slug, started_at=time.time(), status="running", error=None
        )
        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            ok, log_tail = await run_generator(slug)
        except asyncio.TimeoutError:
            current_task["status"] = "failed"
            current_task["error"] = "timeout"
            await update.message.reply_text(
                f"❌ 생성 타임아웃 ({GEN_TIMEOUT_SEC}초)."
            )
            current_task["status"] = "idle"
            return
        except Exception as e:  # noqa: BLE001
            current_task["status"] = "failed"
            current_task["error"] = str(e)
            log.exception("generator 실패")
            await update.message.reply_text(f"❌ 생성 실패: {e}")
            current_task["status"] = "idle"
            return

        if not ok:
            current_task["status"] = "failed"
            await update.message.reply_text(
                f"❌ 생성 실패. 마지막 로그:\n```\n{log_tail}\n```",
                parse_mode="Markdown",
            )
            current_task["status"] = "idle"
            return

        current_task["status"] = "sending"
        await update.message.reply_text(
            f"✅ 완료! output/{slug}/ 에 저장됐어요. 9장 전송합니다."
        )
        await send_slides(update, slug)

        try:
            title = title_for_slug(slug)
            add_done(slug, title)
            await update.message.reply_text(f"📌 '{slug}' 완료 목록에 기록됐어요.")
        except Exception as e:  # noqa: BLE001
            log.exception("add_done 실패")
            await update.message.reply_text(f"⚠️ done 기록 실패: {e}")

        # 주제 이력 자동 등록 (used-topics.json) — 실패해도 파이프라인 영향 없음
        await record_used_topic_on_done(slug)

        # 인스타 9장 완료 → 블로그 글 자동 생성 (실패해도 인스타 결과는 보존)
        await run_blog_generator(update, slug)

        # 블로그 생성 직후 → 릴스 보강 컷 자동 생성 (실패해도 파이프라인 중단 안 함)
        try:
            await asyncio.to_thread(run_reels_supplement, slug)
        except Exception as e:  # noqa: BLE001
            log.warning(f"reels-supplement 호출 실패 (무시): {e}")

        current_task["status"] = "idle"


# ---------------------------------------------------------------- bootstrap


def build_app() -> Application:
    load_dotenv(REPO_ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error(".env 의 TELEGRAM_BOT_TOKEN 이 비어있습니다.")
        sys.exit(1)
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    load_topics()  # data/topics.json 자동 생성

    # concurrent_updates=True : 긴 파이프라인(이미지 생성 ~4분) 도중에도
    # 운영자의 '중단/취소' 메시지가 즉시 처리되도록 함.
    app = Application.builder().token(token).concurrent_updates(True).build()

    def wrap(handler):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not is_authorized(update, allowed_chat_id):
                if update.message:
                    await update.message.reply_text("⛔ 인증되지 않은 사용자입니다.")
                return
            try:
                return await handler(update, context)
            except Exception as e:  # noqa: BLE001
                log.exception("handler 실패")
                try:
                    if update.message:
                        await update.message.reply_text(f"❌ 봇 에러: {e}")
                except Exception:
                    pass
        return wrapped

    app.add_handler(CommandHandler("start", wrap(cmd_start)))
    app.add_handler(CommandHandler("status", wrap(cmd_status)))
    app.add_handler(CommandHandler("topics", wrap(cmd_topics)))
    app.add_handler(CommandHandler("queue", wrap(cmd_queue)))
    app.add_handler(CommandHandler("done", wrap(cmd_done)))
    app.add_handler(CommandHandler("usedtopics", wrap(cmd_used_topics)))
    app.add_handler(CommandHandler("new", wrap(cmd_new)))
    app.add_handler(CommandHandler("batch", wrap(cmd_batch)))
    app.add_handler(CommandHandler("cancel", wrap(cmd_cancel)))
    app.add_handler(CommandHandler("help", wrap(cmd_help)))
    app.add_handler(
        CallbackQueryHandler(wrap(on_duplicate_callback), pattern=r"^dup:")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrap(handle_text)))
    return app


def main() -> None:
    log.info("소아과언니 텔레그램 봇 시작 — %s", datetime.now(timezone.utc).isoformat())
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
