"""릴스 영상 빌더 — 카드뉴스 슬라이드 + 릴스 보충컷 + 음성 + 자막 → output.mp4

기존 카드뉴스/릴스 산출물을 모아 ElevenLabs 음성 + Whisper 타임스탬프 +
자막 번인 + (선택)BGM 을 합성해 세로형(1080x1920) 릴스 영상을 만든다.

사용 예:
    python3 scripts/reels-video-builder.py --topic vs
    python3 scripts/reels-video-builder.py --topic vs --dry-run

입력:
    output/{topic}/slide-01.png ~ slide-09.png
    output/{topic}/reels/*.png          (보충 이미지, 예: 0-1.png, 3-1.png)
    output/{topic}/reels/voiceover.txt  ("**[슬라이드 N - ...]**" 패턴)
    assets/soabook-promo.png            (마지막 고정 이미지)
    assets/bgm/*.mp3                    (있으면 랜덤, 없으면 스킵)

출력:
    output/{topic}/reels/voiceover_raw.mp3
    output/{topic}/reels/voiceover.mp3
    output/{topic}/reels/whisper_segments.json
    output/{topic}/reels/final.srt
    output/{topic}/reels/output.mp4

요구사항: ffmpeg/ffprobe, python: requests, openai-whisper, Pillow
환경(.env): ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL

주의: 이 ffmpeg 빌드에는 subtitles(libass)/drawtext 필터가 없어,
      자막은 Pillow 로 프레임에 직접 그려 합성한다.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"
ASSETS_DIR = REPO_ROOT / "assets"
PROMO_IMG = ASSETS_DIR / "soabook-promo.png"
BGM_DIR = ASSETS_DIR / "bgm"
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

# 캔버스 (세로형): 상단 1080x1350 이미지 + 하단 570 자막 여백
CANVAS_W = 1080
CANVAS_H = 1920
IMG_AREA_H = 1350
BOTTOM_MARGIN_H = CANVAS_H - IMG_AREA_H  # 570
FPS = 30

# 자막 스타일
SUB_FONT_SIZE = 44
SUB_FILL = (255, 255, 255)
SUB_STROKE = (0, 0, 0)
SUB_STROKE_W = 3
SUB_BOTTOM_OFFSET = 80   # 화면 하단에서 80px 위
SUB_MAX_CHARS = 13       # 13자 초과 시 줄바꿈

# 음성 후처리
ATEMPO = 1.1
SILENCE_THRESHOLD = "-40dB"
SILENCE_MIN_DUR = 0.3

ELEVEN_DEFAULT_MODEL = "eleven_multilingual_v2"
BGM_VOLUME = 0.07

# 브랜드 발음/표기 치환
FIXED_FIRST = "소아청소년꽈 전문의가 알려드립니다."
FIXED_LAST = "소아꽈언니의 소아꽈수첩입니다."

# 에러 보고 컨텍스트 (main 에서 설정) — fail() 출력/텔레그램용
_RUN_TOPIC = ""
_RUN_TELEGRAM = False


def to_voice(t: str) -> str:
    """음성용: 소아과 → 소아꽈."""
    return (t or "").replace("소아과", "소아꽈")


def to_sub(t: str) -> str:
    """자막용: 소아꽈 → 소아과 역치환."""
    return (t or "").replace("소아꽈", "소아과")


# ---------------------------------------------------------------- 로그
def log(msg: str) -> None:
    print(msg, flush=True)


def step(n, title: str) -> None:
    log(f"\n[STEP {n}] ▶ {title}")


def done(n, title: str) -> None:
    log(f"[STEP {n}] ✅ {title} 완료")


_STEP_NO = {
    "입력 확인": "1",
    "대본 파싱": "1",
    "STEP 1.5 검증": "1.5",
    "음성 생성": "2",
    "음성 후처리": "3",
    "오디오 길이 확인": "3",
    "Whisper 분석": "4",
    "최종 SRT 생성": "5",
    "이미지 시퀀스": "7",
    "영상 합성": "8",
}


def _err_step_and_hint(stepname: str, msg: str):
    """에러 stepname/메시지로 (STEP 번호, 해결 힌트) 결정."""
    s = stepname or ""
    m = msg or ""
    low = m.lower()
    step_no = _STEP_NO.get(s, "?")
    # 1) script.txt 줄 수 불일치
    mm = re.search(r"script\.txt (\d+)줄인데 슬라이드 이미지는 (\d+)장", m)
    if mm:
        return "1.5", f"script.txt 줄 수({mm.group(1)})를 슬라이드 수({mm.group(2)})에 맞게 수정해주세요."
    # 2) output.srt 없음
    if "output.srt" in m and ("없" in m or "추출하지" in m):
        cmd = (f"python3 scripts/estimate_srt.py output/{_RUN_TOPIC}/reels/voiceover.txt "
               f"-o output/{_RUN_TOPIC}/reels/output.srt")
        return step_no, f"estimate_srt.py로 output.srt를 생성해주세요:\n{cmd}"
    # 3) ElevenLabs API 실패
    if "음성 생성" in s or "elevenlabs" in low:
        return step_no, "ELEVENLABS_API_KEY를 확인해주세요. 크레딧이 부족할 수 있어요."
    # 4) Whisper 실패
    if "whisper" in s.lower() or "whisper" in low:
        return step_no, ("voiceover.mp3 파일이 손상됐을 수 있어요. "
                         "voiceover_raw.mp3를 삭제하고 다시 시도해주세요.")
    # 5) ffmpeg 실패
    if "ffmpeg" in low or "ffprobe" in low or s in ("음성 후처리", "영상 합성", "오디오 길이 확인"):
        return step_no, "ffmpeg 설치 여부 확인: brew install ffmpeg"
    # 6) 기타 예외
    return step_no, f"에러 내용: {m}"


def fail(stepname: str, msg: str):
    """에러 출력(원인+해결방법+토픽) + (--telegram 시) 텔레그램 전송 후 중단."""
    step_no, hint = _err_step_and_hint(stepname, msg)
    out = (
        f"❌ [STEP {step_no}] 에러 발생\n"
        f"원인: {msg}\n"
        f"해결방법: {hint}\n"
        f"토픽: {_RUN_TOPIC}"
    )
    log(out)
    if _RUN_TELEGRAM:
        _send_telegram_warn(out)
    sys.exit(1)


# ---------------------------------------------------------------- 외부 도구
def _run(cmd: list, stepname: str, capture: bool = False) -> str:
    try:
        res = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            text=True,
        )
        return res.stdout if capture else ""
    except FileNotFoundError:
        fail(stepname, f"실행 파일을 찾을 수 없음: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-8:]
        fail(stepname, f"{cmd[0]} 오류:\n" + "\n".join(tail))
    return ""


def ffprobe_duration(path: Path) -> float:
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        "오디오 길이 확인", capture=True,
    )
    try:
        return float(out.strip())
    except ValueError:
        fail("오디오 길이 확인", f"duration 파싱 실패: {out!r}")
    return 0.0


# ---------------------------------------------------------------- 자막 유틸
def _fmt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def wrap_text(text: str, max_chars: int = SUB_MAX_CHARS) -> str:
    """max_chars 초과 시 줄바꿈. 공백 있으면 단어 경계 우선, 없으면 글자수 강제 분할."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        while len(w) > max_chars:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:max_chars])
            w = w[max_chars:]
        cand = w if not cur else cur + " " + w
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


# ---------------------------------------------------------------- STEP 1
_SLIDE_RE = re.compile(r"\*\*\[\s*슬라이드\s*(\d+)[^\]]*\]\*\*\s*(.*)")


def parse_slides(voiceover_txt: Path) -> list:
    """voiceover.txt 에서 '**[슬라이드 N - ...]**' 패턴으로 슬라이드별 텍스트 파싱.
    return [{'slide_num': int, 'text': str}, ...] (slide_num 오름차순)."""
    if not voiceover_txt.exists():
        fail("대본 파싱", f"파일 없음: {voiceover_txt}")
    slides = []
    for ln in voiceover_txt.read_text(encoding="utf-8").splitlines():
        m = _SLIDE_RE.search(ln)
        if not m:
            continue
        num = int(m.group(1))
        text = m.group(2).strip()
        if text:
            slides.append({"slide_num": num, "text": text})
    if not slides:
        fail("대본 파싱", "'**[슬라이드 N]**' 패턴을 찾지 못함")
    slides.sort(key=lambda s: s["slide_num"])
    return slides


def parse_srt_texts(srt_path: Path) -> list:
    """output.srt 에서 각 자막 블록의 텍스트만 순서대로 추출 → [str, ...]."""
    if not srt_path.exists():
        fail("대본 파싱", f"output.srt 없음: {srt_path}")
    raw = srt_path.read_text(encoding="utf-8").strip()
    texts = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        idx = 0
        if re.match(r"^\d+$", lines[0].strip()):
            idx = 1
        if idx < len(lines) and "-->" in lines[idx]:
            idx += 1
        txt = " ".join(ln.strip() for ln in lines[idx:]).strip()
        if txt:
            texts.append(txt)
    if not texts:
        fail("대본 파싱", f"output.srt 에서 텍스트를 추출하지 못함: {srt_path}")
    return texts


def build_full_script(slides: list) -> str:
    """ElevenLabs 전송용 전체 대본.
    첫 문장/마지막 문장 고정, 나머지 슬라이드는 소아과→소아꽈, 공백 한 칸 결합."""
    if len(slides) == 1:
        return FIXED_FIRST
    parts = [FIXED_FIRST]
    for s in slides[1:-1]:
        parts.append(to_voice(s["text"]))
    parts.append(FIXED_LAST)
    return " ".join(p.strip() for p in parts if p.strip())


SCRIPT_SECTION_MARKER = "## [전체 대본 - 이어 읽기]"


def voice_script_units(base: Path, reels: Path, voiceover_txt: Path) -> list:
    """음성 대본 문장 단위 리스트. 에러 없이 빈 리스트까지 폴백.
    1순위: script.txt(각 줄=문장) — base(output/{topic}) 우선, reels 보조
    2순위: voiceover.txt 의 '## [전체 대본 - 이어 읽기]' 섹션(문장 분리)."""
    for cand in (base / "script.txt", reels / "script.txt"):
        if cand.exists():
            lines = [ln.strip() for ln in cand.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                return lines
    if voiceover_txt.exists():
        buf = []
        in_sec = False
        for ln in voiceover_txt.read_text(encoding="utf-8").splitlines():
            if not in_sec:
                if SCRIPT_SECTION_MARKER in ln:
                    in_sec = True
                continue
            if ln.strip().startswith("---"):
                break
            if ln.strip():
                buf.append(ln.strip())
        para = " ".join(buf).strip()
        if para:
            return [s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]
    return []


def parse_slide_numbers(voiceover_txt: Path) -> list:
    """voiceover.txt 의 '**[슬라이드 N]**' 패턴에서 슬라이드 번호 목록 추출. 없으면 [] (에러 없음)."""
    if not voiceover_txt.exists():
        return []
    nums = []
    for ln in voiceover_txt.read_text(encoding="utf-8").splitlines():
        m = _SLIDE_RE.search(ln)
        if not m:
            continue
        # 'N-M' 보충 슬라이드 형식(예: 0-1, 3-1)은 제외
        if re.match(r"\s*-\s*\d", ln[m.end(1):]):
            continue
        n = int(m.group(1))
        # slide_num 은 1 이상 정수만 사용 (0 제외)
        if n < 1:
            continue
        nums.append(n)
    return nums


# ---------------------------------------------------------------- STEP 1.5 (검증)
def _send_telegram_warn(text: str) -> None:
    """텔레그램으로 경고 전송 (토큰/챗ID 없으면 조용히 스킵)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text}, timeout=30,
        )
    except Exception:  # noqa: BLE001
        pass


def _regenerate_script_from_slides(base: Path, n: int, client) -> None:
    """슬라이드 prompt 기반으로 script.txt 를 Claude(haiku)로 1:1 재생성한 뒤,
    voiceover.mp3 삭제 + 빌드 중단(재실행 유도). 정상 반환하지 않음(fail() 로 종료).

    줄=슬라이드 1:1 보장: 본문(슬라이드 2~n-1) N-2줄만 받아
    [FIXED_FIRST] + 본문 + [FIXED_LAST] 로 조립. 압축·병합·[1:-1] 추측 없음.
    Haiku 가 N-2줄과 다르게 주면 최대 2회 재요청, 그래도 안 맞으면 명시적 중단."""
    topic = base.name
    tpl_path = REPO_ROOT / "templates" / f"slides.{topic}.json"
    if not tpl_path.exists():
        fail("STEP 1.5 검증", f"슬라이드 템플릿 없음: templates/slides.{topic}.json")
    try:
        tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        fail("STEP 1.5 검증", f"슬라이드 템플릿 파싱 실패: {e}")
    tpl_slides = tpl.get("slides", []) if isinstance(tpl, dict) else []
    by_n = {s.get("n"): s for s in tpl_slides if isinstance(s, dict)}
    target = max(n - 2, 0)  # 본문 줄 수 = 슬라이드 2~n-1 (첫/끝은 고정 멘트)

    body: list = []
    if target > 0:
        # 본문 슬라이드(2~n-1) prompt 에서 핵심 한국어만 추출 → 순서 그대로 제시
        body_listing = "\n".join(
            "슬라이드 %d: %s" % (
                i,
                " ".join(re.findall(r'[가-힣]+[가-힣\s·%\d]*',
                                    str((by_n.get(i) or {}).get("prompt", "")))).strip(),
            )
            for i in range(2, n)
        )
        base_prompt = (
            f"아래 슬라이드 {target}개 각각에 대해 한 문장씩, "
            f"정확히 {target}줄만 작성해줘.\n"
            f"인트로·아웃트로·번호·설명 없이 본문 {target}줄만 출력. "
            f"한 줄 = 한 슬라이드, 순서 그대로.\n\n"
            f"{body_listing}"
        )

        def _request(extra: str) -> list:
            try:
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": base_prompt + extra}],
                )
                txt = "".join(getattr(b, "text", "") for b in resp.content).strip()
            except Exception as e:  # noqa: BLE001
                fail("STEP 1.5 검증", f"script.txt 재생성 API 실패: {e}")
            # Claude 가 붙인 줄 번호("1. ", "2. " 등) 제거
            return [
                re.sub(r'^\d+\.\s*', '', ln.strip())
                for ln in txt.split("\n") if ln.strip()
            ]

        body = _request("")
        attempts = 0
        while len(body) != target and attempts < 2:
            attempts += 1
            print(f"[WARNING] script.txt 본문 {len(body)}줄 (필요 {target}줄) — 재요청 {attempts}/2")
            body = _request(
                f"\n\n[중요] 정확히 {target}줄이어야 합니다. "
                f"직전 응답은 {len(body)}줄이었습니다. "
                f"인트로/아웃트로/번호 없이 본문 {target}줄만 다시 출력하세요."
            )
        if len(body) != target:
            # 조용한 압축·병합 금지 — 명확히 중단
            fail("STEP 1.5 검증",
                 f"script.txt 재생성 실패: 본문 {target}줄을 받지 못했습니다"
                 f"(최종 {len(body)}줄, 재요청 2회 포함). 조용한 압축·병합 없이 중단합니다.")

    final_lines = [FIXED_FIRST] + body + [FIXED_LAST]
    (base / "reels" / "script.txt").write_text("\n".join(final_lines) + "\n", encoding="utf-8")
    print("[INFO] script.txt 슬라이드 기반으로 자동 재생성됨")
    # voiceover.mp3 삭제 (음성 재생성 트리거)
    mp3 = base / "reels" / "voiceover.mp3"
    if mp3.exists():
        mp3.unlink()
    fail("STEP 1.5 검증",
         "script.txt를 슬라이드 기반으로 재생성했어요. 다시 실행하면 새 대본으로 음성을 만듭니다.")


def validate_script_vs_slides(base: Path, script_lines: list, telegram: bool = False) -> None:
    """script.txt 줄 수 vs 슬라이드 이미지 수 비교 + (sources.json 있으면) Claude 내용 검증.
    줄 수 불일치면 즉시 중단. 내용 불일치면 경고 후 사용자 확인(또는 telegram 자동 중단)."""
    # 0) 직전에 슬라이드 기반으로 재생성된 script.txt 면 검증 없이 통과(재생성 루프 방지)
    marker = base / ".script_regenerated"
    if marker.exists():
        marker.unlink()
        log("[INFO] 재생성된 script.txt — 검증 스킵")
        return

    # 1) script.txt 줄 수 vs 슬라이드 이미지(slide-*.png) 수
    n_lines = len(script_lines)
    m_imgs = len(sorted(base.glob("slide-*.png")))
    log(f"  script.txt {n_lines}줄 vs 슬라이드 이미지 {m_imgs}장")
    if n_lines != m_imgs:
        fail("STEP 1.5 검증", f"script.txt {n_lines}줄인데 슬라이드 이미지는 {m_imgs}장이에요.")

    # 2) sources.json 있으면 Claude(haiku)로 슬라이드별 내용 검증
    sources_path = base / "sources.json"
    if not sources_path.exists():
        log("  sources.json 없음 — 줄 수 검증만 통과")
        return
    try:
        data = json.loads(sources_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"  [경고] sources.json 파싱 실패({e}) — 내용 검증 스킵")
        return
    claims = data.get("claims", []) if isinstance(data, dict) else []
    by_slide = {}
    for c in claims:
        sn = c.get("slide_n")
        if sn is None:
            continue
        by_slide.setdefault(int(sn), []).append(str(c.get("claim_text", "")).strip())

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log("  [경고] ANTHROPIC_API_KEY 없음 — 내용 검증 스킵")
        return
    try:
        from anthropic import Anthropic
    except ImportError:
        log("  [경고] anthropic 미설치 — 내용 검증 스킵")
        return
    client = Anthropic(api_key=api_key)

    mismatches = []
    for i, line in enumerate(script_lines, start=1):
        claim = " ".join(t for t in by_slide.get(i, []) if t)
        if not claim:  # 해당 슬라이드 claim 없으면 스킵
            continue
        prompt = (
            "아래 script 줄이 해당 슬라이드 내용과 맞으면 OK, 맞지 않으면 MISMATCH라고만 답해.\n"
            f"slide {i} claim: {claim}\nscript: {line}"
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            ans = "".join(getattr(b, "text", "") for b in resp.content).strip().upper()
        except Exception as e:  # noqa: BLE001
            log(f"  [경고] 슬라이드 {i} 검증 API 실패({e}) — 스킵")
            continue
        if "MISMATCH" in ans:
            mismatches.append((i, line, claim))

    if not mismatches:
        log("  ✅ 내용 검증 통과 — 불일치 없음")
        return

    warn_text = "\n".join(
        f"[WARNING] 슬라이드 {i} 불일치 가능성:\n  script: {line}\n  슬라이드 내용: {claim}"
        for i, line, claim in mismatches
    )
    log(warn_text)

    # 내용 불일치는 몇 개든 경고만 — 슬라이드는 키워드, script.txt는 완성 문장이라
    # 표현 차이로 인한 오탐이 잦다. 자동 재생성은 무한루프를 유발하므로 하지 않는다.
    # (줄 수 검증은 위에서 이미 통과 — 줄 수만 맞으면 내용은 경고 후 계속 진행)
    if telegram:
        _send_telegram_warn("⚠️ 내용 검증 불일치(경고) — 계속 진행합니다:\n\n" + warn_text)
    log("  [계속] 내용 불일치 경고 — 중단 없이 진행합니다.")


# ---------------------------------------------------------------- STEP 2
def tts_elevenlabs(text: str, out_mp3: Path) -> None:
    try:
        import requests
    except ImportError:
        fail("음성 생성", "requests 미설치 — pip install requests")
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    model_id = os.getenv("ELEVENLABS_MODEL") or ELEVEN_DEFAULT_MODEL
    if not api_key:
        fail("음성 생성", ".env 의 ELEVENLABS_API_KEY 가 비어 있음")
    if not voice_id:
        fail("음성 생성", ".env 의 ELEVENLABS_VOICE_ID 가 비어 있음")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    log(f"  ElevenLabs 호출 (voice={voice_id[:6]}…, model={model_id}, {len(text)}자)")
    try:
        r = requests.post(url, headers=headers, json=body, timeout=180)
    except Exception as e:  # noqa: BLE001
        fail("음성 생성", f"요청 오류: {e}")
    if r.status_code != 200:
        fail("음성 생성", f"HTTP {r.status_code}: {r.text[:300]}")
    out_mp3.write_bytes(r.content)
    if out_mp3.stat().st_size < 1024:
        fail("음성 생성", "응답 mp3 가 너무 작음(생성 실패 의심)")
    log(f"  저장: {out_mp3.name} ({out_mp3.stat().st_size/1024:.0f}KB)")


# ---------------------------------------------------------------- STEP 3
def postprocess_audio(raw_mp3: Path, out_mp3: Path) -> None:
    af = (
        f"atempo={ATEMPO},"
        f"silenceremove=start_periods=1:start_threshold={SILENCE_THRESHOLD}:"
        f"stop_periods=-1:stop_threshold={SILENCE_THRESHOLD}:stop_duration={SILENCE_MIN_DUR}"
    )
    _run(["ffmpeg", "-y", "-i", str(raw_mp3), "-filter:a", af, str(out_mp3)],
         "음성 후처리")
    log(f"  저장: {out_mp3.name} (atempo={ATEMPO}, 무음제거 {SILENCE_THRESHOLD}/{SILENCE_MIN_DUR}s)")


# ---------------------------------------------------------------- STEP 4
def whisper_segments(mp3: Path, json_out: Path, n_slides: int) -> list:
    """voiceover.mp3 → whisper small (word_timestamps=True) → segment 리스트.
    return [{'start','end','text'}], whisper_segments.json 저장."""
    try:
        import whisper
    except ImportError:
        fail("Whisper 분석", "openai-whisper 미설치 — pip install openai-whisper")
    log("  whisper small 모델 로드 중... (최초 1회 다운로드 가능)")
    model = whisper.load_model("small")
    log("  음성 분석 중 (word_timestamps=True)...")
    result = model.transcribe(
        str(mp3), language="ko", word_timestamps=True, verbose=False,
        condition_on_previous_text=True,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )
    segs = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"]).strip()}
        for s in (result.get("segments", []) or [])
    ]
    # segment 수가 슬라이드 수보다 많으면, 마지막부터 역순으로
    # 너무 짧거나(0.5s 미만) 텍스트가 2글자 이하인 segment 를 슬라이드 수와 같아질 때까지 제거
    before = len(segs)
    if len(segs) > n_slides:
        i = len(segs) - 1
        while i >= 0 and len(segs) > n_slides:
            dur = segs[i]["end"] - segs[i]["start"]
            if dur < 0.5 or len(segs[i]["text"].strip()) <= 2:
                segs.pop(i)
            i -= 1
    after = len(segs)
    print(f"[STEP 4] segment 필터링: {before}개 → {after}개")

    json_out.write_text(json.dumps(segs, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  세그먼트 {len(segs)}개 → {json_out.name}")
    return segs


# ---------------------------------------------------------------- STEP 5
def map_slides_to_segments(slides: list, srt_texts: list, segments: list, total: float,
                           script_lines: list = None) -> list:
    """이미지 순서 = voiceover.txt 슬라이드 번호, 자막 텍스트 = output.srt(srt_texts),
    타임스탬프 = whisper segment.

    매핑: script_lines(=script.txt 줄) 각 줄을 difflib 유사도로 segment 에 정렬한다.
      - cursor 부터 연속 1~3개 segment 를 합친 텍스트를 script 줄과 비교해 가장 유사한 window 선택
        (이후 슬라이드가 최소 1개씩 갖도록 window 상한 제한). window start=첫 seg start, end=마지막 seg end.
        마지막 슬라이드는 남은 segment 전부.
      - script_lines 없음/부족하거나 segment < 슬라이드면 글자수 비례 폴백.
    슬라이드1 start=0.0, 마지막 슬라이드 end=전체 길이. text = srt_texts[i]."""
    script_lines = script_lines or []
    n = len(slides)
    m = len(segments)
    if script_lines and len(script_lines) >= n and m >= n:
        # 텍스트 유사도 기반 정렬 — cursor 부터 연속 1~3개 segment 를 합쳐 비교, 최적 window 선택
        starts, ends = [], []
        cursor = 0
        sizes = []
        for i in range(n):
            if i == n - 1:
                grp = segments[cursor:m] or [segments[m - 1]]  # 마지막 슬라이드 = 남은 전부
            else:
                remaining_after = n - 1 - i  # 이후 슬라이드 수 (각 ≥1 보장)
                max_w = max(1, min(3, (m - cursor) - remaining_after))
                best_w, best_r = 1, -1.0
                for w in range(1, max_w + 1):
                    combined = " ".join(
                        str(segments[cursor + k].get("text", "")) for k in range(w)
                    )
                    r = difflib.SequenceMatcher(None, script_lines[i], combined).ratio()
                    if r > best_r:
                        best_r, best_w = r, w
                grp = segments[cursor:cursor + best_w]
            starts.append(float(grp[0]["start"]))
            ends.append(float(grp[-1]["end"]))
            sizes.append(len(grp))
            cursor += len(grp)
        log(f"  유사도 매칭(윈도우): segment {m}개 → 슬라이드 {n}개 (window {sizes})")
    elif m >= n:
        # script_lines 없음 — segment.start 1:1 (end=다음 슬라이드 start)
        starts = [float(segments[i]["start"]) for i in range(n)]
        ends = [starts[i + 1] if i + 1 < n else total for i in range(n)]
    else:
        log(f"  [경고] segment({m}) < 슬라이드({n}) — 글자수 비례 폴백")
        weights = [max(1, len(to_voice(s["text"]))) for s in slides]
        tot = float(sum(weights))
        acc = 0.0
        starts = []
        for w in weights:
            starts.append(total * acc / tot)
            acc += w
        ends = [starts[i + 1] if i + 1 < n else total for i in range(n)]
    starts[0] = 0.0   # 첫 슬라이드는 0초부터 (합계 = 전체 길이 보장)
    ends[-1] = total  # 마지막 슬라이드 end = 전체 음성 길이

    mapped = []
    for i in range(n):
        st = starts[i]
        en = ends[i]
        if en < st:
            en = st
        text = srt_texts[i] if i < len(srt_texts) else slides[i]["text"]
        mapped.append({
            "slide_num": slides[i]["slide_num"],
            "text": text,
            "start": st,
            "end": en,
        })
    return mapped


# ---------------------------------------------------------------- STEP 6
def build_final_srt(mapped: list, srt_out: Path) -> list:
    """슬라이드 1번 포함(모든 슬라이드 동일 처리), 자막 텍스트는 mapped[N].text(output.srt 텍스트),
    13자 줄바꿈 → final.srt. 타임스탬프는 mapped[N].start/end 사용.
    return 자막 엔트리 [{'start','end','text'}]."""
    entries = []
    for m in mapped:
        entries.append({
            "start": m["start"],
            "end": m["end"],
            "text": wrap_text(m["text"]),
        })
    out = []
    for i, e in enumerate(entries, 1):
        out.append(str(i))
        out.append(f"{_fmt_ts(e['start'])} --> {_fmt_ts(e['end'])}")
        out.append(e["text"])
        out.append("")
    srt_out.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"  자막 {len(entries)}개 (슬라이드1 포함) → {srt_out.name}")
    return entries


# ---------------------------------------------------------------- STEP 7
_EXTRA_RE = re.compile(r"^(\d+)-(\d+)\.png$")


def collect_images(topic: str) -> dict:
    """slide-01~09 + reels 보충(prefix-suffix) 이미지 수집.
    return {'slides':{num:path}, 'extras':{prefix:[paths..]}}"""
    base = OUTPUT_DIR / topic
    reels = base / "reels"
    slides = {}
    for i in range(1, 10):
        p = base / f"slide-{i:02d}.png"
        if p.exists():
            slides[i] = p
    # 시작 슬라이드: slide-01.png 우선, 없을 때만 slide-00.png 로 폴백
    if 1 not in slides:
        p0 = base / "slide-00.png"
        if p0.exists():
            slides[1] = p0
    extras = {}
    if reels.exists():
        for p in sorted(reels.glob("*.png")):
            m = _EXTRA_RE.match(p.name)
            if m:
                extras.setdefault(int(m.group(1)), []).append((int(m.group(2)), p))
    extras = {k: [pp for _, pp in sorted(v)] for k, v in extras.items()}
    return {"slides": slides, "extras": extras}


def build_image_timeline(mapped: list, images: dict, total: float) -> list:
    """이미지 시퀀스 → [{'path','start','end','caption'}], 합계 = total.

    규칙:
      - slide-01 (+ 0-1.png 있으면 슬라이드1 윈도우 3:1 — 슬라이드 3/4, 보충 1/4)
      - slide-02~09 순서, prefix-N 보충은 slide-0N 윈도우 3:1 분할(슬라이드 3/4, 보충 1/4)
      - 마지막 슬라이드 윈도우는 1:2 분할 → 앞 1/3 slide-09, 뒤 2/3 promo
      - 자막: promo 만 없음, 슬라이드는 모두 해당 슬라이드 자막(역치환) 표시
    """
    slides = images["slides"]
    extras = images["extras"]
    timeline = []

    def emit(path, st, en, caption):
        if en - st <= 1e-4:
            return
        timeline.append({"path": path, "start": st, "end": en, "caption": caption})

    n = len(mapped)
    for i, m in enumerate(mapped):
        num = m["slide_num"]
        st = m["start"]
        # 슬라이드 표시 구간 = [자기 start, 다음 슬라이드 start) — 마지막은 음성 전체 길이.
        # segment 사이 공백은 이 슬라이드(=이전 이미지)의 마지막 이미지에 흡수돼 그대로 표시됨.
        en = mapped[i + 1]["start"] if i + 1 < n else total
        wdur = en - st
        is_last = (i == n - 1)
        slide_path = slides.get(num)
        if slide_path is None:
            fail("이미지 시퀀스", f"slide-{num:02d}.png 가 없음 (output/{mapped and ''}…)")
        caption = to_sub(m["text"])

        # 이 슬라이드 윈도우에 들어갈 보충 이미지
        ex = list(extras.get(num, []))
        if i == 0:  # 첫 슬라이드 윈도우엔 prefix-0 보충(0-1.png 등) 추가
            ex = list(extras.get(0, [])) + ex

        if is_last:
            # 1:2 분할 (보충이 있어도 promo 규칙 우선)
            third = wdur / 3.0
            emit(slide_path, st, st + third, caption)
            promo = PROMO_IMG if PROMO_IMG.exists() else slide_path
            if not PROMO_IMG.exists():
                log(f"  [경고] {PROMO_IMG} 없음 — 마지막 구간에 slide-{num:02d} 재사용")
            emit(promo, st + third, en, "")  # promo 자막 없음
            if extras.get(num):
                log(f"  [경고] 마지막 슬라이드 보충 {len(extras[num])}장은 promo 규칙으로 생략")
            continue

        if ex:
            # 3:1 분할 — 슬라이드 3/4, 보충 이미지가 나머지 1/4 를 균등 분배
            slide_dur = wdur * 3.0 / 4.0
            emit(slide_path, st, st + slide_dur, caption)
            rest = wdur - slide_dur  # = wdur/4
            per = rest / len(ex)
            t = st + slide_dur
            for e in ex:
                emit(e, t, t + per, caption)
                t += per
        else:
            emit(slide_path, st, en, caption)

    # segment 타임스탬프를 그대로 사용 — 마지막 이미지 end 만 음성 전체 길이로 보정
    if timeline and abs(timeline[-1]["end"] - total) > 1e-6:
        timeline[-1]["end"] = total
    return timeline


# ---------------------------------------------------------------- STEP 8
def _load_font():
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT_PATH, SUB_FONT_SIZE)
    except Exception as e:  # noqa: BLE001
        log(f"  [경고] 폰트 로드 실패({e}) — 기본 폰트 사용")
        return ImageFont.load_default()


def _place_top_image(canvas, img_path):
    """원본 이미지를 비율 유지로 1080x1350 상단 영역에 레터박스 배치."""
    from PIL import Image
    img = Image.open(img_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    sw, sh = img.size
    scale = min(CANVAS_W / sw, IMG_AREA_H / sh)
    nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas.paste(img, ((CANVAS_W - nw) // 2, (IMG_AREA_H - nh) // 2))


def render_frame(img_path, caption: str, out_png: Path, font) -> None:
    """1080x1920 캔버스(검정).
    - 자막: 상단 0~320px 검정 영역(세로 중앙 y=260, 가로 중앙, 46px 흰색+검정외곽 3px)
    - 이미지: 상단에서 320px 아래 ~ 1670px(320+1350) 영역에 비율 유지 배치
    - 하단 1670~1920px 검정 영역 유지(자막 없음)."""
    from PIL import Image, ImageDraw, ImageFont
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))

    # 이미지: 상단 320px 아래(여백 확보)부터 1350 높이로 배치 (320 ~ 1670)
    top_margin = 320
    region_h = IMG_AREA_H  # 이미지 높이 1350 유지 (320 ~ 1670)
    img = Image.open(img_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    sw, sh = img.size
    scale = min(CANVAS_W / sw, region_h / sh)
    nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas.paste(img, ((CANVAS_W - nw) // 2, top_margin + (region_h - nh) // 2))

    # 자막: 상단 0~120px 검정 영역, 세로 중앙 y=60, 가로 중앙, 38px
    if caption:
        draw = ImageDraw.Draw(canvas)
        text = wrap_text(caption, max_chars=20)  # 한 줄 최대 20자
        try:
            sub_font = ImageFont.truetype(FONT_PATH, 46)
        except Exception:
            sub_font = font
        try:
            bbox = draw.multiline_textbbox(
                (0, 0), text, font=sub_font, align="center",
                stroke_width=SUB_STROKE_W, spacing=6,
            )
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            ox, oy = bbox[0], bbox[1]
        except Exception:
            tw, th, ox, oy = CANVAS_W // 2, 46 * 2, 0, 0
        x = (CANVAS_W - tw) // 2 - ox
        y = 260 - th // 2 - oy   # 자막 세로 중앙 y=260 (이미지 바로 위쪽에 붙임)
        draw.multiline_text(
            (x, y), text, font=sub_font, fill=SUB_FILL, align="center",
            stroke_width=SUB_STROKE_W, stroke_fill=SUB_STROKE, spacing=6,
        )
    canvas.save(out_png, "PNG")


def pick_bgm():
    if not BGM_DIR.exists():
        return None
    mp3s = sorted(BGM_DIR.glob("*.mp3"))
    return random.choice(mp3s) if mp3s else None


def compose_video(timeline: list, voiceover: Path, out_mp4: Path, work_dir: Path, font) -> None:
    """타임라인 → Pillow 프레임 렌더 → ffmpeg concat + 음성(+BGM) 합성."""
    log(f"  프레임 {len(timeline)}개 렌더링 중...")
    frames = []
    for i, seg in enumerate(timeline):
        out_png = work_dir / f"frame-{i:04d}.png"
        render_frame(seg["path"], seg["caption"], out_png, font)
        frames.append((out_png, seg["end"] - seg["start"]))

    listfile = work_dir / "frames.txt"
    lines = []
    for p, d in frames:
        lines.append(f"file '{p.resolve()}'")
        lines.append(f"duration {d:.3f}")
    if frames:
        lines.append(f"file '{frames[-1][0].resolve()}'")  # 마지막 프레임 한 번 더
    listfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    bgm = pick_bgm()
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-i", str(voiceover)]
    if bgm:
        # BGM 무한 루프(-stream_loop -1) → amix duration=first 가 영상 길이에 맞춰 자동 컷
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
        fc = (
            "[0:v]fps=%d,format=yuv420p,setsar=1[vout];"
            "[1:a]anull[a0];[2:a]volume=%.3f[a1];"
            "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            % (FPS, BGM_VOLUME)
        )
        log(f"  BGM 믹스: {bgm.name} (volume={BGM_VOLUME})")
    else:
        fc = "[0:v]fps=%d,format=yuv420p,setsar=1[vout];[1:a]anull[aout]" % FPS
        log("  BGM 없음 — 음성만 사용")

    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_mp4),
    ]
    log("  ffmpeg 인코딩 중...")
    _run(cmd, "영상 합성")


# ---------------------------------------------------------------- dry-run
def fake_segments(slides: list):
    """드라이런용 가짜 segment (글자수 비례). return (segments, total)."""
    segs = []
    t = 0.0
    for s in slides:
        dur = max(1.2, len(to_voice(s["text"])) / 7.0)
        segs.append({"start": t, "end": t + dur, "text": to_voice(s["text"])})
        t += dur
    return segs, t


def print_timeline(timeline: list, total: float) -> None:
    log("\n=== 이미지 타임라인 ===")
    for seg in timeline:
        cap = (seg["caption"].replace("\n", " ")[:24] + "…") if seg["caption"] else "(자막없음)"
        log("  %6.2f - %6.2f  %-18s  %s"
            % (seg["start"], seg["end"], Path(seg["path"]).name, cap))
    log("  합계 %.3f s (목표 %.3f s)" % (sum(s["end"] - s["start"] for s in timeline), total))


def _sync_canonical_sources(base: Path, reels: Path) -> None:
    """script.txt·output.srt 가 base/reels 두 곳에 흩어져 옛 파일을 읽는 문제를 영구 차단.
    각 파일의 정본(mtime 최신)을 골라 두 경로를 동일하게 맞추고, 정본이 음성보다 최신이면
    음성·자막 캐시를 삭제한다. 파일이 없으면 조용히 넘어가고, 실패해도 빌드는 계속한다."""
    try:
        reels.mkdir(parents=True, exist_ok=True)
        newest_mtime = 0.0  # 정본(script.txt/output.srt) 중 가장 최신 mtime

        def _unify(name: str, unit: str) -> None:
            nonlocal newest_mtime
            paths = (base / name, reels / name)
            cands = [p for p in paths if p.exists()]
            if not cands:
                return  # 둘 다 없으면 조용히 넘어감
            canon = max(cands, key=lambda p: p.stat().st_mtime)
            canon_mtime = canon.stat().st_mtime  # 덮어쓰기 전 정본 mtime 보존
            content = canon.read_text(encoding="utf-8")
            for p in paths:
                if p != canon:
                    p.write_text(content, encoding="utf-8")  # 두 경로 동일화
            if unit == "줄":
                n = len([ln for ln in content.splitlines() if ln.strip()])
            else:  # 블록
                n = len([b for b in re.split(r"\n\s*\n", content.strip()) if b.strip()])
            log(f"[SYNC] {name} 통일 (정본: {canon.relative_to(base.parent)}, {n}{unit})")
            newest_mtime = max(newest_mtime, canon_mtime)

        _unify("script.txt", "줄")
        _unify("output.srt", "블록")

        # 정본 대본/자막이 voiceover.mp3 보다 최신이면 음성·자막 캐시 무효화
        final_mp3 = reels / "voiceover.mp3"
        if newest_mtime and final_mp3.exists() and newest_mtime > final_mp3.stat().st_mtime:
            removed = False
            for p in (reels / "voiceover_raw.mp3", reels / "voiceover.mp3",
                      reels / "whisper_segments.json", reels / "final.srt"):
                if p.exists():
                    p.unlink()
                    removed = True
            if removed:
                log("[SYNC] 대본/자막 변경 감지 → 음성·자막 캐시 삭제")
    except Exception as e:  # noqa: BLE001
        log(f"[SYNC] 단일화 실패(무시): {e}")


# ---------------------------------------------------------------- main
def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        log("  [경고] python-dotenv 미설치 — 환경변수는 셸에서 직접 읽음")

    ap = argparse.ArgumentParser(description="릴스 영상 빌더 (이미지+음성+자막→mp4)")
    ap.add_argument("--topic", required=True, help="토픽 슬러그 (output/{topic}/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Step1/5/6/7 로직만 실행 (API/Whisper 호출 없음)")
    ap.add_argument("--telegram", action="store_true",
                    help="텔레그램 봇 실행 모드 — 검증 경고를 텔레그램 전송 후 자동 중단")
    args = ap.parse_args()
    topic = args.topic
    # 에러 보고 컨텍스트 설정 (fail() 출력/텔레그램용)
    globals()["_RUN_TOPIC"] = topic
    globals()["_RUN_TELEGRAM"] = args.telegram

    base = OUTPUT_DIR / topic
    reels = base / "reels"
    if not base.exists():
        fail("입력 확인", f"토픽 폴더 없음: {base}")
    reels.mkdir(parents=True, exist_ok=True)

    voiceover_txt = reels / "voiceover.txt"
    output_srt = reels / "output.srt"
    raw_mp3 = reels / "voiceover_raw.mp3"
    final_mp3 = reels / "voiceover.mp3"
    seg_json = reels / "whisper_segments.json"
    final_srt = reels / "final.srt"
    out_mp4 = reels / "output.mp4"

    log("=" * 56)
    log(f"릴스 영상 빌더 — topic={topic}{'  [DRY-RUN]' if args.dry_run else ''}")
    log("=" * 56)

    # STEP 1 (공통)
    step(1, "음성 대본(script.txt/voiceover) + 자막(output.srt) + 슬라이드 순서")
    # 0) 대본/자막 단일화 — 어떤 파일을 읽기 전에 base/reels 정본을 맞춤
    _sync_canonical_sources(base, reels)
    # 1) 음성 대본: script.txt 1순위, voiceover.txt 전체대본 2순위 → 첫/마지막 문장 고정 + 소아과→소아꽈
    voice_units = voice_script_units(base, reels, voiceover_txt)
    full_script = build_full_script([{"text": s} for s in voice_units])
    # 2) 자막 텍스트: output.srt (기존 유지)
    # output.srt 없으면 voiceover.txt 로 estimate_srt.py 자동 실행 후 계속 진행
    if not output_srt.exists():
        if voiceover_txt.exists():
            _gen = subprocess.run(
                ["python3", "scripts/estimate_srt.py",
                 str(voiceover_txt), "-o", str(output_srt)],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
            )
            if _gen.returncode == 0 and output_srt.exists():
                log("[INFO] output.srt 자동 생성됨")
            else:
                fail("대본 파싱",
                     f"output.srt 자동 생성 실패: {_gen.stderr.strip() or _gen.stdout.strip()}")
        else:
            fail("대본 파싱", f"output.srt 없음 + voiceover.txt 없음: {output_srt}")
    srt_texts = parse_srt_texts(output_srt)
    # 3) 슬라이드 이미지 순서: voiceover.txt **[슬라이드 N]** 1순위, 없으면 script.txt 줄 수 기준
    slide_nums = parse_slide_numbers(voiceover_txt)
    if slide_nums:
        slides = [{"slide_num": n, "text": ""} for n in slide_nums]
        slide_src = "voiceover.txt [슬라이드 N]"
    else:
        n_lines = len(voice_units) if voice_units else len(srt_texts)
        slides = [{"slide_num": i, "text": ""} for i in range(1, n_lines + 1)]
        slide_src = f"script.txt 줄 수({n_lines})"
    log(f"  음성대본 {len(full_script)}자, 자막 {len(srt_texts)}줄, 슬라이드 {len(slides)}개 (순서: {slide_src})")
    log(f"  대본 미리보기: {full_script[:60]}…")
    # script.txt 줄 목록 (Step5 유사도 매칭용) — output/{topic}/reels/script.txt
    # script.txt 없으면 voiceover.txt 로 자동 생성 후 계속 진행
    _script_txt = reels / "script.txt"
    if not _script_txt.exists():
        if voiceover_txt.exists():
            _vo_lines = voiceover_txt.read_text(encoding="utf-8").splitlines()
            _gen_lines = []
            # 1) "## [전체 대본 ...]" 섹션 파싱(헤더 변형 허용) → 문장 분리
            _in_sec, _buf = False, []
            for ln in _vo_lines:
                s = ln.strip()
                if not _in_sec:
                    if s.startswith("## [전체 대본"):
                        _in_sec = True
                    continue
                if s.startswith("---") or s.startswith("## ") or s.startswith("**총"):
                    break
                if s:
                    _buf.append(s)
            if _buf:
                _gen_lines = [
                    x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(_buf)) if x.strip()
                ]
            # 2) 섹션 없으면 '[슬라이드 N]' 헤더(별표 유무 무관) + 같은 줄/다음 줄 본문
            if not _gen_lines:
                _hdr = re.compile(r"^\**\[\s*슬라이드[^\]]*\]\**\s*(.*)$")
                _pending = False
                for ln in _vo_lines:
                    s = ln.strip()
                    m = _hdr.match(s)
                    if m:
                        _same = m.group(1).strip()
                        if _same:
                            _gen_lines.append(_same)
                            _pending = False
                        else:
                            _pending = True
                    elif _pending and s:
                        _gen_lines.append(s)
                        _pending = False
            # 3) 그래도 없으면 평문(한 줄=한 슬라이드, '발음형 || 자막형'은 앞부분)
            if not _gen_lines:
                for ln in _vo_lines:
                    s = ln.strip()
                    if not s or s.startswith("#") or s.startswith("---") or s.startswith("["):
                        continue
                    if "||" in s:
                        s = s.split("||")[0].strip()
                    if s:
                        _gen_lines.append(s)
            # 4) 소아청소년꽈전문의 → 소아청소년꽈 전문의 (띄어쓰기 보정)
            _gen_lines = [
                l.replace("소아청소년꽈전문의", "소아청소년꽈 전문의") for l in _gen_lines
            ]
            if _gen_lines:
                _script_txt.write_text("\n".join(_gen_lines) + "\n", encoding="utf-8")
                log("[INFO] script.txt 자동 생성됨")
            else:
                fail("대본 파싱", "script.txt 자동 생성 실패: voiceover.txt 파싱 결과 없음")
        else:
            fail("대본 파싱", f"script.txt 없음 + voiceover.txt 없음: {_script_txt}")
    script_lines = []
    if _script_txt.exists():
        script_lines = [
            ln.strip() for ln in _script_txt.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    # output.srt 블록 수 != script.txt 줄 수 → 초과 블록 자동 삭제 후 계속 진행
    if script_lines and len(srt_texts) > len(script_lines):
        _m_blocks = len(srt_texts)
        _n_script = len(script_lines)
        _raw_srt = output_srt.read_text(encoding="utf-8").strip()
        _blocks = re.split(r"\n\s*\n", _raw_srt)
        output_srt.write_text("\n\n".join(_blocks[:_n_script]) + "\n", encoding="utf-8")
        srt_texts = parse_srt_texts(output_srt)
        log(f"[INFO] output.srt {_m_blocks}블록 → {_n_script}블록으로 자동 동기화")

    done(1, "Step1 입력 준비")

    if args.dry_run:
        # --- 드라이런: Step5/6/7 로직만 ---
        log("\n[DRY-RUN] Step2(음성)·Step3(후처리)·Step4(Whisper)·Step8(합성) 건너뜀")
        segments, total = fake_segments(slides)
        log(f"[DRY-RUN] 가짜 segment {len(segments)}개, 합계 {total:.1f}s 로 진행")

        step(5, "슬라이드별 타임스탬프 매핑")
        mapped = map_slides_to_segments(slides, srt_texts, segments, total, script_lines)
        for m in mapped:
            log("  슬라이드%d %5.2f~%5.2f  %s"
                % (m["slide_num"], m["start"], m["end"], m["text"][:24]))
        done(5, "타임스탬프 매핑")

        step(6, "final.srt 생성")
        build_final_srt(mapped, final_srt)
        done(6, "final.srt 생성")

        step(7, "이미지 시퀀스 구성")
        images = collect_images(topic)
        n_extra = sum(len(v) for v in images["extras"].values())
        log(f"  슬라이드 {len(images['slides'])}장, 보충 {n_extra}장 "
            f"(prefix={sorted(images['extras'].keys())})")
        timeline = build_image_timeline(mapped, images, total)
        done(7, "이미지 시퀀스 구성")

        print_timeline(timeline, total)
        log("\n[DRY-RUN] 완료 — 실제 영상은 --dry-run 없이 실행하세요.")
        return 0

    # --- 실제 빌드 ---
    # STEP 1.5: script.txt 줄 수 + 슬라이드 내용 검증
    step(1.5, "script.txt 검증 (줄 수 + 슬라이드 내용)")
    validate_script_vs_slides(base, script_lines, telegram=args.telegram)
    done(1.5, "script.txt 검증")

    # STEP 2~4: 음성 생성 — script.txt 변경 감지 후 재사용/재생성 결정
    _reuse_audio = False
    if final_mp3.exists():
        if _script_txt.exists() and _script_txt.stat().st_mtime > final_mp3.stat().st_mtime:
            # script.txt 가 voiceover.mp3 보다 최신 → 기존 음성/세그먼트 폐기 후 재생성
            log("[INFO] script.txt가 변경됨 → 음성 재생성")
            for _p in (raw_mp3, final_mp3, seg_json):
                if _p.exists():
                    _p.unlink()
        elif seg_json.exists():
            # 변경 없음 + 캐시 존재 → Step2~4 스킵, 기존 음성/세그먼트 재사용
            _reuse_audio = True

    if _reuse_audio:
        log("[INFO] script.txt 변경 없음 → 기존 음성 재사용 (Step2~4 스킵)")
        total = ffprobe_duration(final_mp3)
        if seg_json.stat().st_mtime < final_mp3.stat().st_mtime:
            # whisper 캐시가 voiceover.mp3 보다 오래됨 → 삭제 후 Whisper 재분석
            log("[INFO] 음성 변경됨 → Whisper 재분석")
            seg_json.unlink()
            segments = whisper_segments(final_mp3, seg_json, len(slides))
        else:
            segments = [
                {"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"])}
                for s in json.loads(seg_json.read_text(encoding="utf-8"))
            ]
        log(f"  재사용: {final_mp3.name} ({total:.1f}s), 세그먼트 {len(segments)}개")
    else:
        # STEP 2
        step(2, "ElevenLabs 음성 생성")
        tts_elevenlabs(full_script, raw_mp3)
        done(2, "음성 생성")

        # STEP 3
        step(3, "음성 후처리 (배속 + 무음제거)")
        postprocess_audio(raw_mp3, final_mp3)
        total = ffprobe_duration(final_mp3)
        log(f"  최종 음성 길이: {total:.1f}s")
        done(3, "음성 후처리")

        # STEP 4
        step(4, "Whisper word-level 타임스탬프 추출")
        segments = whisper_segments(final_mp3, seg_json, len(slides))
        done(4, "Whisper 분석")

    # STEP 5
    print("[DEBUG] 실제 segment 확인:")
    for i, seg in enumerate(segments):
        print(f"  seg{i}: {seg['start']:.2f}~{seg['end']:.2f} : {seg['text'][:20]}")
    step(5, "슬라이드별 타임스탬프 매핑")
    mapped = map_slides_to_segments(slides, srt_texts, segments, total, script_lines)
    for m in mapped:
        print(f"  슬라이드{m['slide_num']}: {m['start']:.2f}~{m['end']:.2f}")
    done(5, "타임스탬프 매핑")

    # STEP 6
    step(6, "final.srt 생성")
    build_final_srt(mapped, final_srt)
    done(6, "final.srt 생성")

    # STEP 7
    step(7, "이미지 시퀀스 구성")
    images = collect_images(topic)
    n_extra = sum(len(v) for v in images["extras"].values())
    log(f"  슬라이드 {len(images['slides'])}장, 보충 {n_extra}장")
    timeline = build_image_timeline(mapped, images, total)
    print_timeline(timeline, total)
    done(7, "이미지 시퀀스 구성")

    # STEP 8
    step(8, "Pillow 프레임 렌더 + ffmpeg 합성")
    font = _load_font()
    with tempfile.TemporaryDirectory(prefix=f"reels_{topic}_") as tmp:
        compose_video(timeline, final_mp3, out_mp4, Path(tmp), font)
    log(f"  결과물: {out_mp4}")
    done(8, "영상 합성")

    log("\n" + "=" * 56)
    log(f"🎬 완료! → {out_mp4.relative_to(REPO_ROOT)}")
    log("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
