"""기존 카드뉴스 일괄 블로그 변환.

output/ 의 슬러그 중 pediatric-blog-bot-main/posts/ 에 없는 것을 모두 변환.
- templates/slides.<slug>.json 누락 → 자동 제외
- 테스트·재실행 슬러그(이름에 'example', 'rerun') → 제외
- 매 슬러그마다 텔레그램 '진행 중 X/Y' 알림, 마지막에 완료 요약
- TELEGRAM_BOT_TOKEN/CHAT_ID 없으면 콘솔 출력만

사용:
    python scripts/bulk-blog-convert.py            # 자동 스캔 후 일괄 변환
    python scripts/bulk-blog-convert.py --dry-run  # 변환 안 하고 대상만 출력
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(_path):
        return None

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

OUTPUT_DIR = REPO_ROOT / "output"
TEMPLATES_DIR = REPO_ROOT / "templates"
BLOG_BOT_ROOT = Path(
    os.environ.get("BLOG_BOT_ROOT")
    or REPO_ROOT.parent / "pediatric-blog-bot-main"
)
BLOG_POSTS = BLOG_BOT_ROOT / "posts"
GENERATE_BLOG_JS = BLOG_BOT_ROOT / "generate-blog.js"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SKIP_NAME_PATTERNS = ("example", "rerun")
PER_SLUG_TIMEOUT_SEC = 300


def telegram_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": TG_CHAT_ID, "text": msg}
        ).encode("utf-8")
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 텔레그램 전송 실패: {e}")


def collect_targets() -> tuple[list[str], list[str], list[str]]:
    """변환 대상·테스트제외·템플릿누락 리스트 반환."""
    if not OUTPUT_DIR.exists():
        return [], [], []
    output_slugs = {
        p.name for p in OUTPUT_DIR.iterdir()
        if p.is_dir() and (p / "slide-09.png").exists()
    }
    published: set[str] = set()
    if BLOG_POSTS.exists():
        for date_dir in BLOG_POSTS.iterdir():
            if date_dir.is_dir() and date_dir.name != "images":
                for slug_dir in date_dir.iterdir():
                    # post.html 이 있어야 진짜 변환 완료 — 실패한 빈 폴더 제외
                    if slug_dir.is_dir() and (slug_dir / "blog" / "post.html").exists():
                        published.add(slug_dir.name)

    candidates = sorted(output_slugs - published)
    targets: list[str] = []
    skipped_test: list[str] = []
    skipped_no_tpl: list[str] = []
    for s in candidates:
        if any(pat in s for pat in SKIP_NAME_PATTERNS):
            skipped_test.append(s)
            continue
        if not (TEMPLATES_DIR / f"slides.{s}.json").exists():
            skipped_no_tpl.append(s)
            continue
        targets.append(s)
    return targets, skipped_test, skipped_no_tpl


def convert_one(slug: str) -> tuple[bool, str]:
    if not GENERATE_BLOG_JS.exists():
        return False, f"generate-blog.js 없음: {GENERATE_BLOG_JS}"
    cmd = ["node", str(GENERATE_BLOG_JS), slug, f"--insta-root={REPO_ROOT}"]
    try:
        result = subprocess.run(
            cmd, cwd=str(BLOG_BOT_ROOT),
            capture_output=True, text=True, timeout=PER_SLUG_TIMEOUT_SEC,
        )
        if result.returncode == 0:
            return True, ""
        tail = (result.stderr or result.stdout).strip().splitlines()
        return False, (tail[-1] if tail else "(no output)")[:200]
    except subprocess.TimeoutExpired:
        return False, f"타임아웃 ({PER_SLUG_TIMEOUT_SEC}s)"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description="기존 카드뉴스 일괄 블로그 변환")
    ap.add_argument("--dry-run", action="store_true", help="변환 대상만 출력")
    ap.add_argument("--exclude", default="", help="콤마로 구분한 슬러그 (이 슬러그들 제외)")
    args = ap.parse_args()

    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}
    targets, skipped_test, skipped_no_tpl = collect_targets()
    skipped_excluded = [s for s in targets if s in excluded]
    targets = [s for s in targets if s not in excluded]
    print(f"=== 변환 대상: {len(targets)}개 ===")
    for s in targets:
        print(f"  {s}")
    if skipped_test:
        print(f"\n--- 테스트·재실행 슬러그 제외 ({len(skipped_test)}개) ---")
        for s in skipped_test:
            print(f"  {s}")
    if skipped_no_tpl:
        print(f"\n--- templates 누락 제외 ({len(skipped_no_tpl)}개) ---")
        for s in skipped_no_tpl:
            print(f"  {s}")
    if skipped_excluded:
        print(f"\n--- --exclude 로 제외 ({len(skipped_excluded)}개) ---")
        for s in skipped_excluded:
            print(f"  {s}")

    if args.dry_run or not targets:
        if not targets:
            telegram_send("✅ 변환 대상 0개 — 이미 모두 블로그로 변환됨")
        return 0

    total = len(targets)
    telegram_send(f"📋 일괄 변환 시작: 총 {total}개")
    print(f"\n=== 변환 시작 (총 {total}개) ===")

    ok = 0
    fail: list[tuple[str, str]] = []
    for i, slug in enumerate(targets, 1):
        print(f"[{i}/{total}] {slug} ...", end=" ", flush=True)
        t0 = time.time()
        success, err = convert_one(slug)
        dt = time.time() - t0
        if success:
            print(f"OK ({dt:.0f}s)")
            ok += 1
        else:
            print(f"FAIL ({dt:.0f}s) — {err}")
            fail.append((slug, err))
        telegram_send(f"📋 일괄 변환 진행 중: {i}/{total} 완료")

    print(f"\n=== 결과: 성공 {ok}/{total}, 실패 {len(fail)} ===")
    for s, e in fail:
        print(f"  ❌ {s} — {e}")

    summary = f"✅ 일괄 변환 완료! 총 {ok}개 블로그 글 생성됐어요"
    if fail:
        names = ", ".join(s for s, _ in fail[:5])
        more = f" 외 {len(fail)-5}" if len(fail) > 5 else ""
        summary += f"\n❌ 실패 {len(fail)}개: {names}{more}"
    telegram_send(summary)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
