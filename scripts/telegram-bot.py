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
SESSION_FILE = REPO_ROOT / "data" / "session.json"
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
        "👶 소아과언니 카드뉴스 봇\n"
        "\n"
        "💬 자연어로 말씀해주세요:\n"
        "  • '주제 추천해줘' — 이번 주 주제 7개 추천\n"
        "  • '1번 만들어' — 추천 목록에서 선택해서 생성\n"
        "  • '수족구병 만들어줘' — 바로 생성\n"
        "  • '보류 목록 보여줘' — 나중에 쓸 주제 확인\n"
        "  • '완료된 거 뭐야' — 발행 완료 목록\n"
        "  • '다시' — 주제 다시 추천\n"
        "\n"
        "📌 슬래시 커맨드도 사용 가능:\n"
        "  /topics /queue /done /status"
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
- queue: 보류 목록 조회 ("보류 목록", "나중에 쓸 것", "큐 보여줘" 등)
- done: 완료 목록 조회 ("완료된 거", "다 만든 것", "발행한 것" 등)
- status: 현재 상태 ("지금 뭐 해", "진행 중인 거", "상태 확인" 등)
- select: 추천 목록에서 번호 선택 ("1번", "1 3 5", "첫 번째" 등)
- queue_move: 보류에서 이번 주로 이동 ("3번 이번 주로", "큐 3 올려줘" 등)
- delete: 보류에서 삭제 ("2번 없애줘", "삭제 1" 등)
- retry: 재추천 요청 ("다시", "다른 거", "다시 추천해줘" 등)
- confirm: 확인/진행 ("응", "좋아", "예", "ㅇㅇ", "ok", "진행해" 등)
- cancel: 취소 ("아니", "취소", "됐어", "ㄴㄴ" 등)
- feedback_regen: 특정 슬라이드 재생성 요청
  ("4장 내용 너무 어려워", "3번 다시 만들어줘",
   "slide-05 수정해줘", "5번 슬라이드 바꿔줘" 등)
  → params: {"slide_n": 4, "feedback": "너무 어려워"}
- verify_slide: 의학 내용 확인 질문
  ("8번 내용 맞아?", "3장 의학적으로 정확해?",
   "이 내용 근거 있어?" 등)
  → params: {"slide_n": 8}
- edit_slide: 구체적 수정 지시
  ("3번 슬라이드 38도 아니고 38.5도야",
   "5장에 부루펜 6개월 이상이라고 추가해줘" 등)
  → params: {"slide_n": 3, "instruction": "38도 아니고 38.5도야"}
- general_question: 현재 토픽 관련 일반 질문
  ("차멀미약 몇 살부터 먹여?",
   "이 내용 부모들한테 어떻게 설명하면 좋아?" 등)
  → params: {"question": "질문 내용"}
- context_reference: 이미 만든 카드뉴스를 언급
  ("travel-emergency-kit 말이야", "아까 만든 거 말이야",
   "방금 그거", "그 카드뉴스" 등)
  → params: {"slug": "travel-emergency-kit"}
- unknown: 위 중 어느 것도 아님

JSON 형식:
{"intent": "make", "params": {"topic_kr": "차멀미 예방법"}}
{"intent": "select", "params": {"numbers": [1, 3, 5]}}
{"intent": "queue_move", "params": {"numbers": [3]}}
{"intent": "delete", "params": {"numbers": [2]}}
{"intent": "topics", "params": {}}
{"intent": "feedback_regen", "params": {"slide_n": 4, "feedback": "너무 어려워"}}
{"intent": "verify_slide", "params": {"slide_n": 8}}
{"intent": "edit_slide", "params": {"slide_n": 3, "instruction": "38도 아니고 38.5도야"}}
{"intent": "general_question", "params": {"question": "차멀미약 몇 살부터?"}}
{"intent": "context_reference", "params": {"slug": "travel-emergency-kit"}}
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


# ---------------------------------------------------------------- handle_text


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
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
        await auto_pipeline(update, topic_kr, slug)

    elif intent == "select":
        numbers = params.get("numbers", [])
        if not numbers:
            await update.message.reply_text("💡 번호를 알려주세요. 예) '1번 만들어'")
            return
        if session.get("recommendation"):
            # 추천 목록에서 선택
            await apply_recommendation_selection(update, numbers)
        else:
            # this_week에서 선택해서 바로 생성
            data = load_topics()
            this_week = data.get("this_week", [])
            idx = numbers[0] - 1
            if 0 <= idx < len(this_week):
                topic = this_week[idx]
                topic_kr = topic.get("title_kr", "")
                slug = topic.get("slug", "")
                if not slug:
                    slug = await korean_to_slug(topic_kr)
                await auto_pipeline(update, topic_kr, slug)
            else:
                await update.message.reply_text(
                    f"💡 이번 주 확정된 주제가 {len(this_week)}개예요.\n"
                    f"1-{len(this_week)} 사이 번호를 말씀해주세요."
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
        session["recommendation"] = None
        await update.message.reply_text("✅ 취소됐어요.")

    elif intent == "feedback_regen":
        slide_n = params.get("slide_n")
        feedback = params.get("feedback", "더 쉽게 설명해줘")
        last = session.get("last_topic")
        if not last:
            await update.message.reply_text(
                "💡 먼저 카드뉴스를 만들어주세요."
            )
            return
        await regen_single_slide(update, last, slide_n, feedback)

    elif intent == "verify_slide":
        slide_n = params.get("slide_n")
        last = session.get("last_topic")
        if not last:
            await update.message.reply_text(
                "💡 먼저 카드뉴스를 만들어주세요."
            )
            return
        await verify_single_slide(update, last, slide_n)

    elif intent == "edit_slide":
        slide_n = params.get("slide_n")
        instruction = params.get("instruction", "")
        last = session.get("last_topic")
        if not last or not instruction:
            await update.message.reply_text(
                "💡 수정 내용을 구체적으로 알려주세요.\n"
                "예) '3번 슬라이드 38도 아니고 38.5도야'"
            )
            return
        await regen_single_slide(update, last, slide_n, instruction)

    elif intent == "general_question":
        question = params.get("question", "")
        last = session.get("last_topic")
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


async def generate_template(
    sources: dict, topic_kr: str, slug: str, api_key: str
) -> tuple[Optional[dict], str]:
    """톤 자동 선택 + sources 기반으로 9장 슬라이드 템플릿 생성. (template, tone_name) 반환."""
    tone_name = await select_tone(topic_kr, slug)
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
    palette = sources.get("palette", "A")

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
"Portrait 1080x1350. Editorial modern style. Clean white background #FAFAF7. Top-right: 소아과언니 navy small — 4 characters only, no quotes, no decoration. Top-center small gray: SAVE & SHARE. Large bold headline center: 오늘 확인하고 navy, 저장하세요 coral #C44536 with thin coral underline. Source box light gray #F0F0EE rounded: 출처 [4개 출처 목록]. Horizontal thin navy divider. Center: 소아과언니 navy bold large — NO quotes NO decoration. Small gray SOAGWA UNNIE · 소아청소년과 전문의. Horizontal divider. @soagwa_unnie · 매주 새 가이드 navy. [앱 CTA] coral. No pixel text. No quotes anywhere."
---

검증된 prompt 패턴 (반드시 이 패턴 따를 것):
{tone_template}"""
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

    # STEP 3: template 생성 (톤 자동 선택 포함)
    try:
        template, tone_name = await generate_template(
            verified_sources, topic_kr, slug, api_key
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

    # STEP 4: 이미지 생성
    await update.message.reply_text("🖼️ 이미지 생성 중... (약 4분)")
    await trigger_generation_direct(update, slug)

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
]

TONE_GUIDE = """
5가지 톤과 어울리는 토픽 유형:
1. editorial-modern — 일반 케어·예방법·여행·생활건강·약물 안전
2. handdrawn-notebook — 영아 돌봄·수유·산통·야간 육아·따뜻한 주제
3. clean-infographic — 수치·기준·비교표·예방접종·성장 백분위
4. emergency-alert — 응급 대처·경련·아나필락시스·즉시 신호 판단
5. character-illustration — 이유식·발달·성장·영양·귀여운 영아 주제

규칙:
- 토픽 특성 보고 가장 자연스럽게 어울리는 톤 선택
- 딱 하나에만 묶이지 않고 유연하게 판단
- 최근 사용 톤 피해서 다양성 유지
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
