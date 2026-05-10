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
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
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
