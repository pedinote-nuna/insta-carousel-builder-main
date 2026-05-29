"""릴스 보강 컷 생성 스크립트.

기존 카드뉴스 9장(templates/slides.{topic}.json)을 분석해서
릴스용 추가 6장 프롬프트를 자동 생성하고 이미지까지 만든다.

사용 예:
    python scripts/reels-supplement.py --topic nosebleed-head-back-danger
    python scripts/reels-supplement.py --topic air-conditioning-cold-myth --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    from google import genai
    from anthropic import Anthropic
except ImportError as e:
    print(f"[ERROR] 필수 패키지 누락: {e}")
    print("  pip install python-dotenv google-genai anthropic")
    sys.exit(1)

try:
    import requests as _requests
except ImportError:
    _requests = None  # 텔레그램 전송 단계에서만 확인

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None  # generate_images 단계에서만 확인


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
OUTPUT_DIR = REPO_ROOT / "output"

GEMINI_MODEL = "gemini-3-pro-image-preview"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"  # 기존 telegram-bot.py ANTHROPIC_MODEL 과 일치

TARGET_W = 1080
TARGET_H = 1350


def _resize_to_card(path: Path, target_w: int = TARGET_W, target_h: int = TARGET_H) -> None:
    """center-crop 으로 비율 유지 후 1080×1350 으로 리사이즈해 PNG 로 덮어쓰기."""
    if _PILImage is None:
        return
    img = _PILImage.open(path)
    src_w, src_h = img.size
    tgt_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > tgt_ratio:
        # 가로가 더 넓음 → 좌우 잘라냄
        new_w = int(round(src_h * tgt_ratio))
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    elif src_ratio < tgt_ratio:
        # 세로가 더 김 → 상하 잘라냄
        new_h = int(round(src_w / tgt_ratio))
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
    img = img.resize((target_w, target_h), _PILImage.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, "PNG", optimize=True)

TYPE_A = "통념깨기형"
TYPE_B = "응급정보형"
TYPE_C = "보관관리형"
TYPE_D = "발달체크형"
TYPE_E = "비교형"

TYPE_KEYWORDS = {
    TYPE_A: ["통념", "오해", "myth", "틀렸", "사실은", "X mark", "prohibition"],
    TYPE_B: ["응급", "emergency", "119", "위험 신호", "즉시", "긴급"],
    TYPE_C: ["보관", "storage", "냉장", "냉동", "온도", "시간"],
    TYPE_D: ["발달", "체크리스트", "milestone", "개월", "성장"],
    TYPE_E: ["vs", "비교", "차이", "다릅니다", "comparison"],
}


def classify_topic_type(slides_data: dict) -> str:
    """common_style + 모든 슬라이드 prompt 텍스트에서 키워드 매치 수가 최대인 유형 반환."""
    common = slides_data.get("common_style", "") or ""
    if isinstance(common, dict):
        common = json.dumps(common, ensure_ascii=False)
    parts = [str(common)]
    for s in slides_data.get("slides", []) or []:
        parts.append(str(s.get("prompt", "")))
        parts.append(str(s.get("role", "")))
    haystack = "\n".join(parts).lower()

    scores: dict[str, int] = {}
    for type_name, keywords in TYPE_KEYWORDS.items():
        scores[type_name] = sum(1 for kw in keywords if kw.lower() in haystack)

    best_score = max(scores.values())
    if best_score == 0:
        return TYPE_A

    for type_name in (TYPE_A, TYPE_B, TYPE_C, TYPE_D, TYPE_E):
        if scores[type_name] == best_score:
            return type_name
    return TYPE_A


SYSTEM_PROMPT = """당신은 소아과언니(@dr.soa_unnie) 인스타그램 릴스 전문 비주얼 디렉터입니다.
기존 카드뉴스 9장을 분석해서 릴스용 추가 6장 프롬프트를 작성합니다.

릴스 추가 컷 원칙:
1. 카드뉴스에 이미 있는 내용 반복 금지
2. 첫 2컷은 반드시 감정/공감/충격 훅
3. 실사 클로즈업 최소 2장 포함
4. 카드뉴스에 없는 실용 정보 1장 포함
5. 마지막 컷은 저장 유도

비율: 1080x1920 (9:16 세로)
스타일: 카드뉴스와 동일한 배경색/폰트/액센트 컬러 유지

각 추가 컷에 insert_after 필드를 반드시 포함하세요.
insert_after는 카드뉴스 몇 번 슬라이드 뒤에 삽입할지 나타냅니다.
0이면 카드뉴스 1번 앞에 삽입합니다.
예시: insert_after: 1 이면 카드뉴스 1번 뒤에 삽입.

출력은 JSON 객체만. 다른 텍스트·마크다운·코드펜스 금지."""


def _summarize_slides(slides: list[dict]) -> str:
    """슬라이드 목록을 'n번, role, prompt 요약' 한 줄씩으로 줄임."""
    lines = []
    for s in slides:
        n = s.get("n", "?")
        role = s.get("role", "?")
        prompt = str(s.get("prompt", "")).strip().replace("\n", " ")
        if len(prompt) > 200:
            prompt = prompt[:200] + "..."
        lines.append(f"- {n}번 ({role}): {prompt}")
    return "\n".join(lines)


def generate_extra_prompts(
    slides_data: dict, topic_type: str, topic_name: str, api_key: str
) -> list[dict]:
    """Claude 로 릴스 추가 6장 프롬프트 생성. extra_slides 리스트 반환."""
    client = Anthropic(api_key=api_key)
    user_prompt = f"""주제 슬러그: {topic_name}
주제 유형: {topic_type}
카드뉴스 기존 9장 내용:
{_summarize_slides(slides_data.get("slides", []))}

위 카드뉴스를 보완하는 릴스 추가 6장 프롬프트를 JSON으로 작성해주세요.

출력 형식:
{{
  "extra_slides": [
    {{
      "n": 10,
      "insert_after": 1,
      "role": "reels-hook",
      "purpose": "이 컷의 목적 한 줄",
      "prompt": "나노바나나용 영문 프롬프트 (카드뉴스 스타일과 동일)"
    }}
  ]
}}

총 6개 (n=10~15). insert_after 는 0~9 정수."""
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    ).strip()

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"Claude 응답에서 JSON 객체를 찾지 못함: {text[:300]}")
    parsed = json.loads(m.group(0))
    extras = parsed.get("extra_slides", [])
    if not isinstance(extras, list) or len(extras) != 6:
        raise ValueError(f"extra_slides 가 6개가 아님 (got {len(extras) if isinstance(extras, list) else '?'})")
    return extras


def assign_filenames(extras: list[dict], n_slides: int = 9) -> list[dict]:
    """각 extra 에 'file' 필드 추가 ({insert_after}-{idx}.png).

    insert_after 누락·범위 밖이면 0 으로 보정. 같은 insert_after 면 등장 순서대로 -1, -2.
    """
    for e in extras:
        pos = e.get("insert_after")
        if not isinstance(pos, int) or pos < 0 or pos > n_slides:
            e["insert_after"] = 0
    counter: dict[int, int] = {}
    for e in extras:
        pos = e["insert_after"]
        counter[pos] = counter.get(pos, 0) + 1
        e["file"] = f"{pos}-{counter[pos]}.png"
    return extras


def render_final_order(extras_with_file: list[dict], n_slides: int = 9) -> str:
    """slide-01 → 1-1.png(role) → slide-02 → ... 인터리브 문자열."""
    by_pos: dict[int, list[dict]] = {}
    for e in extras_with_file:
        by_pos.setdefault(e["insert_after"], []).append(e)
    parts: list[str] = []
    for extra in by_pos.get(0, []):
        parts.append(f"{extra['file']}({extra.get('role','?')})")
    for i in range(1, n_slides + 1):
        parts.append(f"slide-{i:02d}")
        for extra in by_pos.get(i, []):
            parts.append(f"{extra['file']}({extra.get('role','?')})")
    return " → ".join(parts)


def generate_images(extras_with_file: list[dict], topic_name: str, api_key: str) -> list[tuple]:
    """각 추가 슬라이드 이미지 생성. e['file'] 사용. e['_path'] 에 저장 경로 기록.
    저장 직후 PIL center-crop + 1080×1350 리사이즈 (PIL 없으면 원본 유지 + 안내)."""
    out_dir = OUTPUT_DIR / topic_name / "reels"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=api_key)
    summary = []

    if _PILImage is None:
        print("[WARN] Pillow 미설치 → 이미지 리사이즈(1080×1350) 스킵. `pip install Pillow` 후 재실행하면 자동 적용.")

    for slide in extras_with_file:
        n = slide.get("n", 0)
        filename = slide["file"]
        out_path = out_dir / filename
        prompt = slide.get("prompt", "")
        print(f"[{n}→{filename}] generating {slide.get('role','?')}...", end=" ", flush=True)
        t0 = time.time()
        try:
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=[prompt])
            if not resp.candidates:
                dt = time.time() - t0
                print(f"SKIP ({dt:.1f}s, no candidates — safety filter)")
                summary.append((n, "SKIP_SAFETY", f"{dt:.1f}s", "-"))
                continue
            parts = resp.candidates[0].content.parts if resp.candidates[0].content else []
            if not parts:
                dt = time.time() - t0
                print(f"SKIP ({dt:.1f}s, empty parts)")
                summary.append((n, "SKIP_EMPTY", f"{dt:.1f}s", "-"))
                continue
            image_saved = False
            for part in parts:
                if getattr(part, "inline_data", None) and part.inline_data.data:
                    blob = part.inline_data.data
                    if isinstance(blob, str):
                        blob = base64.b64decode(blob)
                    out_path.write_bytes(blob)
                    image_saved = True
                    break
            dt = time.time() - t0
            if image_saved:
                try:
                    _resize_to_card(out_path)
                except Exception as resize_err:  # noqa: BLE001
                    print(f"  [WARN] 리사이즈 실패({resize_err}) — 원본 유지")
                slide["_path"] = str(out_path)
                kb = out_path.stat().st_size / 1024
                print(f"OK ({dt:.1f}s, {kb:.0f}KB) -> {filename}")
                summary.append((n, "OK", f"{dt:.1f}s", filename))
            else:
                print(f"FAIL ({dt:.1f}s, no image in response)")
                summary.append((n, "FAIL_NO_IMG", f"{dt:.1f}s", "-"))
        except Exception as e:  # noqa: BLE001
            dt = time.time() - t0
            print(f"ERR ({dt:.1f}s): {e}")
            summary.append((n, "ERR", f"{dt:.1f}s", str(e)[:80]))
    return summary


VOICEOVER_SYSTEM_PROMPT = """너는 소아청소년과 전문의 소아과언니의 인스타그램 릴스 대본 작성 전문가야.
슬라이드 프롬프트 목록이 주어지면 아래 규칙에 따라 릴스 대본을 작성해.

대본 구조:
소아청소년꽈전문의가 알려드립니다.
{각 슬라이드 핵심 내용 한 문장씩}
소아꽈언니의 소아꽈수첩입니다.

규칙:
- 시작 멘트 고정: 소아청소년꽈전문의가 알려드립니다.
- 끝 멘트 고정: 소아꽈언니의 소아꽈수첩입니다.
- 슬라이드당 한 문장 (1.5~2초 분량)
- 목표 자수: 200~250자 (공백 제외), 슬라이드 8장 이상이면 300자 내외 허용
- 구어체, 말하듯 자연스럽게. 문어체 금지 (~합니다→~해요, ~입니다→~이에요)
- 흐름이 자연스러워야 함, 딱딱한 나열 금지

숫자 한글 변환:
400→사백, 600→육백, 38℃→삼십팔도, 12개월→돌/한살,
24시간→하루, 20분→이십분, 7~10일→칠일에서열흘

영어 한글 변환:
AAP→에이에이피, SPF→에스피에프, DEET→디트, HEPA→히파,
ORS→경구수액제, HFMD→수족구병

기호 제거: · / % 등은 말로 풀어서
예: 80%→열에여덟, 55℃→오십오도

출력 형식:
[슬라이드 1] 대본 한 문장
[슬라이드 2] 대본 한 문장
...
[전체 대본] 이어서 읽는 버전
총 자수: N자 (공백 제외)"""


def _collect_ordered_slides(
    slides_data: dict, extras_with_file: list[dict], n_card_slides: int
) -> list[dict]:
    """카드뉴스 + extras 를 insert_after 기준으로 인터리브해서 [{label, role, prompt}] 반환."""
    by_pos: dict[int, list[dict]] = {}
    for e in extras_with_file:
        by_pos.setdefault(e["insert_after"], []).append(e)
    card_by_n = {s.get("n"): s for s in (slides_data.get("slides") or [])}

    out: list[dict] = []
    for extra in by_pos.get(0, []):
        out.append({
            "label": extra["file"],
            "role": str(extra.get("role", "")),
            "prompt": str(extra.get("prompt", "")),
        })
    for i in range(1, n_card_slides + 1):
        card = card_by_n.get(i)
        if card:
            out.append({
                "label": f"slide-{i:02d}",
                "role": str(card.get("role", "")),
                "prompt": str(card.get("prompt", "")),
            })
        for extra in by_pos.get(i, []):
            out.append({
                "label": extra["file"],
                "role": str(extra.get("role", "")),
                "prompt": str(extra.get("prompt", "")),
            })
    return out


def generate_voiceover_script(ordered_slides: list[dict], api_key: str) -> str:
    """Claude 로 릴스 대본 생성. 전체 응답 텍스트 반환."""
    client = Anthropic(api_key=api_key)
    lines = []
    for i, s in enumerate(ordered_slides, 1):
        prompt = str(s.get("prompt", "")).replace("\n", " ").strip()
        if len(prompt) > 150:
            prompt = prompt[:150]
        lines.append(f"{s['label']} / {s.get('role','?')} / {prompt}")
    user_prompt = (
        "아래는 최종 순서로 정렬된 전체 슬라이드 목록입니다.\n"
        "각 슬라이드에 맞는 릴스 대본을 작성해주세요.\n\n"
        + "\n".join(lines)
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=VOICEOVER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    ).strip()


def send_voiceover_to_telegram(script_text: str) -> None:
    """대본 텍스트를 텔레그램 sendMessage 로 전송. 4096자 초과 시 분할."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음 → 대본 전송 스킵")
        return
    if _requests is None:
        print("[WARN] requests 라이브러리 없음 → 대본 전송 스킵")
        return

    header = "📝 릴스 대본\n\n"
    body = script_text
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # 텔레그램 sendMessage 제한 4096자. 첫 청크는 header 포함, 이후 청크는 본문만.
    limit = 4096
    first_room = limit - len(header)
    chunks: list[str] = []
    if len(body) <= first_room:
        chunks.append(header + body)
    else:
        # 줄 경계 기준 분할 — 첫 청크에만 header
        idx = 0
        first = True
        while idx < len(body):
            room = first_room if first else limit
            end = min(idx + room, len(body))
            if end < len(body):
                nl = body.rfind("\n", idx, end)
                if nl != -1 and nl > idx + room // 2:
                    end = nl + 1
            chunk_body = body[idx:end]
            chunks.append((header + chunk_body) if first else chunk_body)
            idx = end
            first = False

    for chunk in chunks:
        try:
            r = _requests.post(
                url,
                data={"chat_id": chat_id, "text": chunk},
                timeout=30,
            )
            if r.status_code != 200:
                print(f"[WARN] 대본 sendMessage 실패 ({r.status_code}): {r.text[:200]}")
        except Exception as ex:  # noqa: BLE001
            print(f"[WARN] 대본 sendMessage 오류: {ex}")
        time.sleep(0.3)


def send_to_telegram(
    extras_with_file: list[dict], final_order_text: str
) -> None:
    """insert_after 순서대로 sendPhoto + 마지막에 최종 순서 sendMessage."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음 → 텔레그램 전송 스킵")
        return
    if _requests is None:
        print("[WARN] requests 라이브러리 없음 → `pip install requests` 후 다시 실행. 전송 스킵")
        return

    sorted_extras = sorted(
        extras_with_file,
        key=lambda e: (e.get("insert_after", 0), e.get("file", "")),
    )

    sent = 0
    for e in sorted_extras:
        path = e.get("_path")
        if not path or not Path(path).exists():
            continue
        caption = f"📸 {e.get('insert_after', 0)}번 뒤 삽입\n목적: {e.get('purpose','')}"
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        try:
            with open(path, "rb") as f:
                r = _requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": f},
                    timeout=60,
                )
            if r.status_code == 200:
                sent += 1
            else:
                print(f"[WARN] sendPhoto 실패 ({r.status_code}): {r.text[:200]}")
        except Exception as ex:  # noqa: BLE001
            print(f"[WARN] sendPhoto 오류: {ex}")
        time.sleep(0.5)

    if sent == 0:
        print("[WARN] 전송된 이미지 0장 → 최종 메시지 스킵")
        return

    final_msg = f"✅ 릴스 추가 {sent}장 완료\n\n=== 최종 순서 ===\n{final_order_text}"
    msg_url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = _requests.post(
            msg_url,
            data={"chat_id": chat_id, "text": final_msg},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[WARN] sendMessage 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as ex:  # noqa: BLE001
        print(f"[WARN] sendMessage 오류: {ex}")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")

    ap = argparse.ArgumentParser(description="릴스 보강 6장 자동 생성")
    ap.add_argument("--topic", required=True, help="슬러그 (templates/slides.{topic}.json)")
    ap.add_argument("--dry-run", action="store_true", help="이미지 생성·텔레그램 전송 없이 프롬프트만 저장")
    args = ap.parse_args()

    slides_path = TEMPLATES_DIR / f"slides.{args.topic}.json"
    if not slides_path.exists():
        print(f"[ERROR] 카드뉴스 템플릿 없음: {slides_path}")
        print(f"  먼저 /carousel-new <주제>로 9장 슬라이드를 생성하세요.")
        return 1

    anth_key = os.getenv("ANTHROPIC_API_KEY")
    if not anth_key:
        print("[ERROR] ANTHROPIC_API_KEY 가 .env 에 없습니다.")
        return 1

    gem_key = os.getenv("GEMINI_API_KEY")
    if not args.dry_run and not gem_key:
        print("[ERROR] GEMINI_API_KEY 가 .env 에 없습니다 (--dry-run 으로 우회 가능).")
        return 1

    slides_data = json.loads(slides_path.read_text(encoding="utf-8"))
    n_card_slides = len(slides_data.get("slides", []) or []) or 9

    topic_type = classify_topic_type(slides_data)
    print(f"[1/6] 주제 유형 분류: {topic_type}")

    print(f"[2/6] Claude 로 릴스 추가 6장 프롬프트 생성 중...")
    try:
        extras = generate_extra_prompts(slides_data, topic_type, args.topic, anth_key)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 프롬프트 생성 실패: {e}")
        return 1

    extras = assign_filenames(extras, n_slides=n_card_slides)
    final_order_text = render_final_order(extras, n_slides=n_card_slides)

    extra_path = TEMPLATES_DIR / f"slides.{args.topic}.reels-extra.json"
    extra_path.write_text(
        json.dumps(
            {
                "topic": args.topic,
                "topic_type": topic_type,
                "source_template": slides_path.name,
                "n_card_slides": n_card_slides,
                "extra_slides": extras,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[3/6] 추가 프롬프트 저장: {extra_path.relative_to(REPO_ROOT)}")

    if args.dry_run:
        print("[4/6] --dry-run: 이미지 생성·텔레그램 전송 건너뜀")
        print()
        print("=== 추가 6장 미리보기 ===")
        for s in extras:
            print(
                f"  {s.get('n','?')}번 ({s.get('role','?')}) → {s['file']} "
                f"[insert_after={s['insert_after']}]: {s.get('purpose','')}"
            )
        print()
        print("=== 최종 릴스 순서 ===")
        print(final_order_text)
        print()
        print("[5/6] 대본 생성 건너뜀 (--dry-run)")
        print("[6/6] 텔레그램 전송 건너뜀 (--dry-run)")
        return 0

    print(f"[4/6] 이미지 생성 (Gemini {GEMINI_MODEL})")
    summary = generate_images(extras, args.topic, gem_key)
    print()
    print("=== SUMMARY ===")
    for n, status, dt, info in summary:
        print(f"  n={n:>3}  {status:12}  {dt:>7}  {info}")
    out_dir = OUTPUT_DIR / args.topic / "reels"
    print(f"\nOutput: {out_dir.relative_to(REPO_ROOT)}/")
    print()
    print("=== 최종 릴스 순서 ===")
    print(final_order_text)

    print()
    print("[5/6] 텔레그램 이미지 전송")
    send_to_telegram(extras, final_order_text)

    print()
    print("[6/6] Claude 로 릴스 대본 생성")
    if not anth_key:
        print("[WARN] ANTHROPIC_API_KEY 없음 → 대본 생성 스킵")
    else:
        ordered = _collect_ordered_slides(slides_data, extras, n_card_slides)
        try:
            script_text = generate_voiceover_script(ordered, anth_key)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 대본 생성 실패: {e}")
        else:
            voiceover_path = out_dir / "voiceover.txt"
            voiceover_path.write_text(script_text + "\n", encoding="utf-8")
            print(f"대본 저장: {voiceover_path.relative_to(REPO_ROOT)}")
            send_voiceover_to_telegram(script_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
