"""
insta-carousel-builder — 나노바나나 Pro(Gemini 3.0 Pro Image) 인스타 캐러셀 생성기

사용법:
    # 1. .env 에 GEMINI_API_KEY=... 추가 (.env.example 참고)
    # 2. slides.json 에 9장 프롬프트 수정 (또는 기본 예제 그대로 사용)
    # 3. 실행:
    python scripts/nanobanana-gen.py --topic my-topic

결과:
    output/{topic}/slide-01.png ~ slide-09.png

참고:
    - 모델: gemini-3-pro-image-preview (별칭 nano-banana-pro-preview)
    - 한글 정확도 실측: 97.8% (에이나우 2026-04-15 9장 테스트 기준)
    - 1장당 약 25초 / 9장 약 4분 / 비용 약 500~1000원
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types
except ImportError as e:
    print(f"[ERROR] 의존성 누락: {e}")
    print("        pip install python-dotenv google-genai")
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLIDES_JSON = REPO_ROOT / "templates" / "slides.example.json"


def load_slides(slides_path: Path) -> list[dict]:
    if not slides_path.exists():
        print(f"[ERROR] slides 파일 없음: {slides_path}")
        print(f"        templates/slides.example.json 을 복사해서 수정하세요.")
        sys.exit(1)
    with slides_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    slides = data.get("slides", [])
    if len(slides) != 9:
        print(f"[WARN] 슬라이드 개수 {len(slides)} (권장: 9장 — 인스타 캐러셀 최대)")
    return slides


def render_prompt(slide: dict, common_style: str) -> str:
    prompt = slide['prompt']
    # cover(slide-01) 슬라이드에 "소아청소년과 전문의" 텍스트 자동 추가
    if str(slide.get('role', '')).lower() == 'cover':
        prompt += "\n\nIMPORTANT: At the bottom center of the image, render Korean text '소아청소년과 전문의' in a clear, readable font. The text should be prominent and visible against the background."
    return f"{common_style}\n\n{prompt}"


def main():
    ap = argparse.ArgumentParser(description="나노바나나 Pro 인스타 캐러셀 생성기")
    ap.add_argument("--topic", default="default", help="출력 폴더명. output/{topic}/ 에 저장")
    ap.add_argument("--slides", default=str(DEFAULT_SLIDES_JSON), help="slides.json 경로")
    ap.add_argument("--model", default="gemini-3-pro-image-preview", help="Gemini 이미지 모델 ID")
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 프롬프트만 출력")
    ap.add_argument("--slide-n", type=int, default=None, help="해당 번호 슬라이드 1장만 재생성")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key and not args.dry_run:
        print("[ERROR] GEMINI_API_KEY not found in .env or environment.")
        print("        .env.example 을 복사해서 .env 를 만들고 키를 추가하세요.")
        print("        키 발급: https://aistudio.google.com/apikey")
        sys.exit(1)

    slides_path = Path(args.slides)
    data = json.loads(slides_path.read_text(encoding="utf-8"))
    common_style = data.get("common_style", "")
    slides = data.get("slides", [])

    if args.slide_n is not None:
        slides = [s for s in slides if s.get("n") == args.slide_n]
        if not slides:
            print(f"[ERROR] slide-{args.slide_n:02d} not found in {slides_path}")
            sys.exit(1)

    out_dir = REPO_ROOT / "output" / args.topic
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print(f"[DRY RUN] {len(slides)} slides, output: {out_dir}")
        for slide in slides:
            print(f"\n--- slide-{slide['n']:02d} ({slide.get('role','?')}) ---")
            print(render_prompt(slide, common_style)[:500])
        return

    client = genai.Client(api_key=api_key)
    summary = []

    for slide in slides:
        n = slide["n"]
        out_path = out_dir / f"slide-{n:02d}.png"
        prompt = render_prompt(slide, common_style)

        print(f"[{n}/{len(slides)}] generating {slide.get('role','?')}...", end=" ", flush=True)
        t0 = time.time()
        try:
            resp = client.models.generate_content(model=args.model, contents=[prompt])
            image_saved = False
            for part in resp.candidates[0].content.parts:
                if getattr(part, "inline_data", None) and part.inline_data.data:
                    blob = part.inline_data.data
                    if isinstance(blob, str):
                        blob = base64.b64decode(blob)
                    with open(out_path, "wb") as f:
                        f.write(blob)
                    image_saved = True
                    break
            dt = time.time() - t0
            if image_saved:
                kb = out_path.stat().st_size / 1024
                print(f"OK ({dt:.1f}s, {kb:.0f}KB) -> {out_path.name}")
                summary.append((n, "OK", f"{dt:.1f}s", f"{kb:.0f}KB"))
            else:
                print(f"FAIL ({dt:.1f}s, no image in response)")
                summary.append((n, "FAIL_NO_IMG", f"{dt:.1f}s", "-"))
        except Exception as e:
            dt = time.time() - t0
            print(f"ERR ({dt:.1f}s): {e}")
            summary.append((n, "ERR", f"{dt:.1f}s", str(e)[:60]))

    print("\n=== SUMMARY ===")
    for n, status, dt, info in summary:
        print(f"  slide-{n:02d}  {status:12}  {dt:>7}  {info}")
    print(f"\nOutput: {out_dir}")


if __name__ == "__main__":
    main()
