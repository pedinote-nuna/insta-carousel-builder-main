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
    /topics         — Claude API 로 이번 주 주제 7개 추천
    /queue          — 보류 주제 목록
    /done           — 완료 주제 목록 (최근 10개)
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
    from telegram import Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application,
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
TOPIC_SELECTION_MD = REPO_ROOT / "knowledge" / "topic-selection.md"

GEN_TIMEOUT_SEC = 300
SLIDE_COUNT = 9
DONE_PREVIEW = 10
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

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


# ---------------------------------------------------------------- session state


current_task: dict = {
    "topic": None,
    "started_at": None,
    "status": "idle",
    "error": None,
}
_task_lock = asyncio.Lock()

session: dict = {
    "recommendation": None,    # /topics 후 번호 선택 대기 (List[dict]) 또는 None
    "duplicate_slug": None,    # /new 중복 확인 대기 (str) 또는 None
}


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


def _parse_json_object(raw: str) -> Optional[dict]:
    """raw 텍스트에서 JSON object 추출·파싱. 실패 시 None."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- /start /status


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👶 소아과언니 카드뉴스 봇\n\n"
        "📋 주제 관리\n"
        "  /topics      — 주 7개 추천 (Claude API)\n"
        "  /queue       — 보류 목록\n"
        "  /done        — 완료 목록 (최근 10개)\n\n"
        "🎨 생성\n"
        "  /new <슬러그>  — 9장 생성\n"
        "  /status        — 진행 상황\n\n"
        "💬 자유 입력\n"
        "  '1 3 5'        — 추천 후 번호 선택\n"
        "  '다시'         — 추천 다시\n"
        "  '큐 1'         — 보류 → 이번 주\n"
        "  '삭제 2'       — 보류 삭제\n"
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
        f"=== 정책 (knowledge/topic-selection.md) ===\n{selection_md}\n=== 정책 끝 ==="
    )
    user = (
        f"오늘은 {today} ({this_month}월)입니다.\n"
        f"이번 달 캘린더와 6필터를 적용해서 카드뉴스 주제 7개를 추천해주세요.\n"
        f"이미 완료된 다음 슬러그는 제외하세요: {done if done else '없음'}\n\n"
        "JSON 배열로만 응답하세요. 다른 텍스트, 설명, 마크다운은 절대 포함하지 마세요.\n"
        "각 항목 형식:\n"
        '[{"slug": "영문-소문자-하이픈-슬러그", '
        '"title_kr": "한국어 제목", '
        '"category": "카테고리 (예: 응급·약물 안전, 일상 케어 등)", '
        '"palette": "A 또는 B", '
        '"reason": "추천 이유 한 줄"}]'
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


def format_recommendations(recs: list[dict]) -> str:
    lines = ["📋 이번 주 주제 추천 7개", ""]
    for i, r in enumerate(recs, 1):
        lines.append(
            f"{i}. {r.get('title_kr','?')} ({r.get('category','?')} / {r.get('palette','?')})"
        )
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


# ---------------------------------------------------------------- handle_text


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return

    # 1) 중복 확인 대기
    if session.get("duplicate_slug"):
        slug = session["duplicate_slug"]
        if text.lower() in ("예", "yes", "y", "ok"):
            session["duplicate_slug"] = None
            await trigger_generation(update, slug)
            return
        if text.lower() in ("아니오", "아니요", "no", "n", "취소"):
            session["duplicate_slug"] = None
            await update.message.reply_text("❌ 취소했습니다.")
            return
        await update.message.reply_text(
            f"❓ '{slug}' 중복 확인 대기 중. '예' 또는 '아니오'로 답해주세요."
        )
        return

    # 2) "다시" 키워드
    if text in ("다시", "재추천", "refresh"):
        session["recommendation"] = None
        await cmd_topics(update, context)
        return

    # 3) "큐 N N..."
    if text.startswith("큐 ") or text == "큐":
        nums = [int(m) for m in _NUM_RE.findall(text)]
        await move_pending_to_week(update, nums)
        return

    # 4) "삭제 N N..."
    if text.startswith("삭제 ") or text == "삭제":
        nums = [int(m) for m in _NUM_RE.findall(text)]
        await delete_from_pending(update, nums)
        return

    # 자연어 만들기 — "N번 만들어" / "X 만들어줘"
    if "만들어" in text:
        m = re.search(r"(\d+)\s*번(?:째)?\s*만들어", text)
        if m:
            n = int(m.group(1))
            week = load_topics().get("this_week", [])
            if 1 <= n <= len(week):
                item = week[n - 1]
                await auto_pipeline(
                    update,
                    item.get("title_kr") or item.get("slug"),
                    item.get("slug"),
                )
            else:
                await update.message.reply_text(
                    f"❌ 이번 주 항목에 {n}번이 없습니다 (총 {len(week)}개)."
                )
            return
        topic_part = re.split(
            r"\s*(?:카드뉴스)?\s*만들어\s*(?:줘|주세요)?\.?\s*$", text, maxsplit=1
        )[0].strip()
        topic_part = re.sub(r"[을를]\s*$", "", topic_part).strip()
        if topic_part:
            week = load_topics().get("this_week", [])
            match = None
            for item in week:
                t = item.get("title_kr", "")
                if t and (topic_part in t or t in topic_part):
                    match = item
                    break
            if match:
                await auto_pipeline(update, match["title_kr"], match["slug"])
            else:
                await update.message.reply_text(
                    f"🔤 '{topic_part}' 영문 슬러그 생성 중..."
                )
                slug = await korean_to_slug(topic_part)
                if slug:
                    await auto_pipeline(update, topic_part, slug)
                else:
                    await update.message.reply_text(
                        f"❌ '{topic_part}' 슬러그 변환 실패. /topics 로 등록 후 시도하세요."
                    )
            return

    # 5) recommendation 활성 시 숫자 입력 → 선택
    if session.get("recommendation"):
        nums = [int(m) for m in _NUM_RE.findall(text)]
        if nums:
            await apply_recommendation_selection(update, nums)
            return

    # 6) 슬러그 추출
    slug = extract_topic_slug(text)
    if slug and template_path(slug).exists():
        await begin_new_with_duplicate_check(update, slug)
        return

    await update.message.reply_text(
        "💡 사용법은 /start. 슬러그가 있다면 /new <slug>."
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
    lines.append("/new <slug> 로 생성 시작하세요!")
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


async def korean_to_slug(topic_kr: str) -> Optional[str]:
    """한국어 토픽을 영문 케밥-케이스 슬러그로 변환 (Claude API)."""
    if Anthropic is None:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        return await asyncio.to_thread(_korean_to_slug_sync, topic_kr, api_key)
    except Exception as e:  # noqa: BLE001
        log.exception("korean_to_slug 실패: %s", e)
        return None


def _korean_to_slug_sync(topic_kr: str, api_key: str) -> Optional[str]:
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
    return m.group(0) if m else None


def generate_sources(
    topic_kr: str, slug: str, today: str, api_key: str
) -> Optional[dict]:
    """Claude API + web_search 로 sources.json 초안 생성."""
    client = Anthropic(api_key=api_key)
    system = """당신은 소아과언니 카드뉴스 의학 리서처입니다.

규칙:
- Tier 1만: AAP CPG, 대한소아청소년과학회, 질병관리청, Nelson Textbook, UpToDate
- Tier 2: PubMed peer-reviewed (Tier 1 부족 시)
- 블로그·위키·일반기사 절대 금지
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
    return _parse_json_object(_collect_text(msg))


def verify_sources(sources: dict, topic_kr: str, api_key: str) -> dict:
    """Claude API + web_search 로 sources.json 자동 검증·수정."""
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
    return parsed if parsed else sources


def generate_template(
    sources: dict, topic_kr: str, slug: str, api_key: str
) -> Optional[dict]:
    """톤 가이드 + sources 기반으로 9장 슬라이드 템플릿 생성."""
    client = Anthropic(api_key=api_key)
    palette = sources.get("palette", "A")

    tone_dir = REPO_ROOT / "knowledge" / "tone"
    tone_path = tone_dir / "editorial-modern.md"  # 팔레트 A·B 둘 다 정의
    if tone_path.exists():
        tone_content = tone_path.read_text(encoding="utf-8")
    else:
        files = sorted(tone_dir.glob("*.md"))
        tone_content = files[0].read_text(encoding="utf-8") if files else ""

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
- 숫자+px 텍스트 카드에 표시 금지"""
    user = f"""토픽: {topic_kr}
슬러그: {slug}
팔레트: {palette}
톤 가이드:
{tone_content}

sources.json:
{json.dumps(sources, ensure_ascii=False)}

위 내용으로 9장 슬라이드 프롬프트 JSON 작성해줘.

---
좋은 prompt 예시 (이 품질로 작성해줘):

slide n:1 cover 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right corner: 소아과언니 in deep navy #1A1F36 Pretendard SemiBold — 4 characters only, no quotes, no decoration. Top-left small gray: PARENTING NOTE · 010. Left side upper area: very large bold Korean headline two lines — first line [토픽 관련 명사] in deep navy, second line [핵심 단어] in coral #C44536. Below headline: small muted gray subtitle [부제] · AAP 권장 기준. Right side center-bottom: large clean editorial illustration related to topic, line art style in coral and teal, minimal clean strokes. Bottom area: thin coral horizontal accent line. No pixel values as text on card. No quotes on card."

slide n:3-8 body 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right: 소아과언니 navy small — 4 characters only, no quotes, no decoration. Top-left small coral rounded label: [섹션명]. Large bold headline: [앞부분] navy, [마지막 핵심 단어] coral #C44536. Center: clean editorial illustration related to content, line art coral/navy minimal. Below: [카드 or 리스트 구조]. Bottom thin coral divider. No pixel text on card. No quotes on card."

slide n:9 outro 예시:
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right: 소아과언니 navy small — 4 characters only, no quotes, no decoration. Top-center small gray: SAVE & SHARE. Large bold headline center: 오늘 확인하고 navy, 저장하세요 coral #C44536 with thin coral underline. Source box light gray #F0F0EE rounded: 출처 [4개 출처 목록]. Horizontal thin navy divider. Center: 소아과언니 navy bold large — NO quotes NO decoration. Small gray SOAGWA UNNIE · 소아청소년과 전문의. Horizontal divider. @soagwa_unnie · 매주 새 가이드 navy. [앱 CTA] coral. No pixel text. No quotes anywhere."
---"""
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return _parse_json_object(_collect_text(msg))


async def auto_pipeline(update: Update, topic_kr: str, slug: str) -> None:
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

    # STEP 3: template 생성
    await update.message.reply_text("🎨 슬라이드 구성 중...")
    try:
        template = await asyncio.to_thread(
            generate_template, verified_sources, topic_kr, slug, api_key
        )
    except Exception as e:  # noqa: BLE001
        log.exception("generate_template 실패")
        await update.message.reply_text(f"❌ 템플릿 생성 실패: {e}")
        return
    if not template:
        await update.message.reply_text("❌ 템플릿 응답을 파싱하지 못했습니다.")
        return

    tpl_path = TEMPLATES_DIR / f"slides.{slug}.json"
    tpl_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    n_slides = len(template.get("slides", []))
    await update.message.reply_text(f"✓ 템플릿 저장 ({n_slides}장 슬라이드)")

    # STEP 4: 이미지 생성
    await update.message.reply_text("🖼️ 이미지 생성 중... (약 4분)")
    await trigger_generation_direct(update, slug)


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

        current_task["status"] = "idle"


async def run_generator(slug: str) -> tuple[bool, str]:
    cmd = [
        sys.executable,
        str(NANO_GEN),
        "--topic",
        slug,
        "--slides",
        str(template_path(slug)),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GEN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    log_tail = "\n".join(out.strip().splitlines()[-15:])
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

    app = Application.builder().token(token).build()

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
    app.add_handler(CommandHandler("new", wrap(cmd_new)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrap(handle_text)))
    return app


def main() -> None:
    log.info("소아과언니 텔레그램 봇 시작 — %s", datetime.now(timezone.utc).isoformat())
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
