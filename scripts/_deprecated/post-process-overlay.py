#!/usr/bin/env python3
"""
post-process-overlay.py
나노바나나 9장 PNG 에 시리즈 요소(좌측 액센트 바, footer 구분선,
✓ 소아청소년과 전문의 검수 배지, 페이지 번호)를 일괄 덧씌워
시리즈 일관성 100% 보장.

source 폴더 우선순위:
  1) output/<topic>/raw/slide-XX.png  (분리 보관 시)
  2) output/<topic>/slide-XX.png       (fallback — 기존 폴더 구조)

output:
  output/<topic>/final/slide-XX.png

usage:
  python scripts/post-process-overlay.py --topic example --palette A
  python scripts/post-process-overlay.py --topic vaccine-side-effects --palette B
"""

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ─────────────────────────────────────────────────────────────────────
# 디자인 DNA — knowledge/brand-facts.md 와 1:1 동기화
#   변경 시 brand-facts.md 도 함께 갱신할 것
# ─────────────────────────────────────────────────────────────────────
PALETTE = {
    "A": {  # Editorial Coral — 일상 케어·영양·성장발달·앱활용
        "name":      "Editorial Coral",
        "accent":    "#C44536",
        "secondary": "#5B7C99",
        "text":      "#1A1F36",
    },
    "B": {  # Medical Teal — 예방접종·응급·약물·위험 신호
        "name":      "Medical Teal",
        "accent":    "#2C6E63",
        "secondary": "#6B7B8C",
        "text":      "#0E1B2C",
    },
}

CANVAS_W       = 1080
CANVAS_H       = 1350

# 1단계 (지우기) — 옛 코랄 바·옛 footer·옛 검수 텍스트 흔적 완전 제거
ERASE_LEFT_W   = 80   # x = 0~80 흰색 강제 덮기 (옛 좌측 바 흔적 제거)
ERASE_BOTTOM_H = 120  # y = 1230~1350 흰색 강제 덮기 (옛 footer·핸들 흔적 제거)

# 2단계 (그리기) — 정확한 시리즈 요소
LEFT_BAR_W     = 14
FOOTER_H       = 90
FOOTER_BG      = "#FFFFFF"
FOOTER_DIVIDER = "#DDDDDD"
TRUST_BADGE    = "✓ 소아청소년과 전문의 검수"
BADGE_FONT_PX  = 32
PAGE_FONT_PX   = 32

# 한글 폰트 후보 (앞에서부터 존재하는 것 사용)
#   Pretendard 가 시스템에 설치돼 있으면 우선,
#   없으면 macOS 기본 한글 폰트 AppleSDGothicNeo.ttc Bold(인덱스 6) 로 fallback
FONT_CANDIDATES = [
    ("/Library/Fonts/Pretendard-Bold.ttf", None),
    ("/Library/Fonts/Pretendard-Bold.otf", None),
    (str(Path.home() / "Library/Fonts/Pretendard-Bold.ttf"), None),
    (str(Path.home() / "Library/Fonts/Pretendard-Bold.otf"), None),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 6),
    ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", None),
]


def load_font(size: int):
    """첫 번째 사용 가능한 한글 굵은 폰트 로드. 모두 실패 시 PIL 기본 폰트."""
    for path, index in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            if index is not None:
                return ImageFont.truetype(path, size, index=index)
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def overlay_one(src: Path, dst: Path, palette_key: str, page_num: int, total: int) -> None:
    """단일 슬라이드에 시리즈 요소 덧씌우기."""
    pal = PALETTE[palette_key]

    img = Image.open(src).convert("RGB")
    if img.size != (CANVAS_W, CANVAS_H):
        img = img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    draw = ImageDraw.Draw(img)

    # ─── 1단계: 지우기 (옛 흔적 완전 제거) ───────────────────────────
    # 좌측 80px 흰색 강제 덮기 — 옛 좌측 바 흔적 제거
    draw.rectangle(
        [(0, 0), (ERASE_LEFT_W, CANVAS_H)],
        fill="#FFFFFF",
    )
    # 하단 120px 흰색 강제 덮기 — 옛 footer·핸들·검수 텍스트 흔적 제거
    draw.rectangle(
        [(0, CANVAS_H - ERASE_BOTTOM_H), (CANVAS_W, CANVAS_H)],
        fill="#FFFFFF",
    )

    # ─── 2단계: 그리기 (정확한 시리즈 요소) ──────────────────────────
    # 1) 좌측 14px 액센트 바 (위 → 아래 끝까지 균일)
    draw.rectangle(
        [(0, 0), (LEFT_BAR_W, CANVAS_H)],
        fill=pal["accent"],
    )

    # 2) 하단 footer 영역 (높이 90px, 흰 배경) — 일러스트가 있더라도 흰색으로 덮음
    footer_top = CANVAS_H - FOOTER_H
    draw.rectangle(
        [(0, footer_top), (CANVAS_W, CANVAS_H)],
        fill=FOOTER_BG,
    )

    # 3) footer 위쪽 1px 회색 구분선
    draw.line(
        [(0, footer_top), (CANVAS_W, footer_top)],
        fill=FOOTER_DIVIDER,
        width=1,
    )

    # 4) ✓ 소아청소년과 전문의 검수 (좌측 60px, Bold 32px, 메인 텍스트 컬러)
    badge_font = load_font(BADGE_FONT_PX)
    bbox = draw.textbbox((0, 0), TRUST_BADGE, font=badge_font)
    text_h = bbox[3] - bbox[1]
    badge_y = footer_top + (FOOTER_H - text_h) // 2 - bbox[1]
    draw.text(
        (60, badge_y),
        TRUST_BADGE,
        font=badge_font,
        fill=pal["text"],
    )

    # 5) "01 / 09" 페이지 번호 (우측 60px, Bold 32px, secondary)
    page_text = f"{page_num:02d} / {total:02d}"
    page_font = load_font(PAGE_FONT_PX)
    pbox = draw.textbbox((0, 0), page_text, font=page_font)
    page_w = pbox[2] - pbox[0]
    page_h = pbox[3] - pbox[1]
    page_x = CANVAS_W - 60 - page_w
    page_y = footer_top + (FOOTER_H - page_h) // 2 - pbox[1]
    draw.text(
        (page_x, page_y),
        page_text,
        font=page_font,
        fill=pal["secondary"],
    )

    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="나노바나나 PNG 9장에 시리즈 요소(액센트 바·footer·페이지 번호) 덧씌우기"
    )
    parser.add_argument(
        "--topic",
        required=True,
        help="output/<topic>/ 폴더 이름 (예: example, 2026-05-08_fever-clothing)",
    )
    parser.add_argument(
        "--palette",
        required=True,
        choices=["A", "B"],
        help="A=Editorial Coral, B=Medical Teal",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=9,
        help="총 슬라이드 수 (기본 9)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    topic_dir = repo_root / "output" / args.topic

    if not topic_dir.exists():
        print(f"[ERROR] 토픽 폴더가 없습니다: {topic_dir}", file=sys.stderr)
        return 1

    raw_dir = topic_dir / "raw"
    src_dir = raw_dir if raw_dir.exists() else topic_dir
    final_dir = topic_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)

    print(f"📂 source : {src_dir.relative_to(repo_root)}")
    print(f"📂 output : {final_dir.relative_to(repo_root)}")
    print(f"🎨 palette: {args.palette} ({PALETTE[args.palette]['name']})")
    print()

    success = 0
    missing: list[str] = []
    for n in range(1, args.total + 1):
        src = src_dir / f"slide-{n:02d}.png"
        dst = final_dir / f"slide-{n:02d}.png"
        if not src.exists():
            missing.append(src.name)
            print(f"  ✗ {src.name} 없음")
            continue
        overlay_one(src, dst, args.palette, n, args.total)
        print(f"  ✓ slide-{n:02d}.png → final/slide-{n:02d}.png")
        success += 1

    print()
    print(f"✅ {success}/{args.total} 완료")
    if missing:
        print(f"⚠️  누락된 원본: {', '.join(missing)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
