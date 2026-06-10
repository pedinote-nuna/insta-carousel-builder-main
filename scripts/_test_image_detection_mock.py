"""이미지 생성 무음 실패 감지 Mock 테스트 — 실제 API·이미지 생성 0회 ($0).

telegram-bot.py 를 import 한 뒤 run_generator / send_generator 류를 가짜로 교체해
"종료코드 0이어도 PNG 없으면 실패" 감지 + batch 실패 집계 + 정상 성공 유지
+ /regen 이미지만 재생성 흐름을 검증한다.

실행: python3 scripts/_test_image_detection_mock.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT_PATH = REPO / "scripts" / "telegram-bot.py"

spec = importlib.util.spec_from_file_location("telegram_bot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

# 운영 세션/토픽 파일 보호 + API 키 더미
bot._save_session = lambda: None
bot.session = {}
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")


# --- 가짜 텔레그램 객체 ---
class FakeMessage:
    def __init__(self, sink):
        self.sink = sink
        self.text = ""

    async def reply_text(self, text, **kw):
        self.sink.append(("text", text))

    async def reply_photo(self, photo=None, caption=None, **kw):
        self.sink.append(("photo", caption))

    class _Chat:
        async def send_action(self, *a, **k):
            pass

    @property
    def chat(self):
        return FakeMessage._Chat()


class FakeUpdate:
    def __init__(self, text, sink):
        self.message = FakeMessage(sink)
        self.message.text = text


def make(sink, text=""):
    return FakeUpdate(text, sink)


def dump(sink, label):
    print(f"\n── {label} ──")
    for kind, t in sink:
        first = (t or "").split("\n")[0]
        print(f"  [{kind}] {first}")


def _use_tmp_output():
    """OUTPUT_DIR/TEMPLATES_DIR 를 임시폴더로 교체하고 경로 반환."""
    tmp = Path(tempfile.mkdtemp(prefix="img_mock_"))
    bot.OUTPUT_DIR = tmp / "output"
    bot.TEMPLATES_DIR = tmp / "templates"
    bot.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bot.TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    return tmp


def _make_pngs(slug, count):
    folder = bot.OUTPUT_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        (folder / f"slide-{i:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n_mock_")


def _reset_downstream(tracker):
    """trigger_generation_direct 의 다운스트림(완료기록·블로그·릴스)을 가짜로 교체."""
    bot.title_for_slug = lambda slug: slug
    bot.add_done = lambda slug, title="": tracker.append(("add_done", slug))

    async def fake_record(slug):
        tracker.append(("record", slug))

    async def fake_blog(update, slug):
        tracker.append(("blog", slug))

    def fake_reels(slug):
        tracker.append(("reels", slug))

    bot.record_used_topic_on_done = fake_record
    bot.run_blog_generator = fake_blog
    bot.run_reels_supplement = fake_reels


# ---------------------------------------------------------------- 시나리오들
async def scenario_A_missing_detection():
    """_missing_slides: 0장→9개 누락, 9장→[], 7장→2개 누락."""
    _use_tmp_output()
    slug = "topic-a"
    (bot.OUTPUT_DIR / slug).mkdir(parents=True, exist_ok=True)

    miss0 = bot._missing_slides(slug)
    assert len(miss0) == bot.SLIDE_COUNT, f"0장인데 누락 {len(miss0)}개"

    _make_pngs(slug, bot.SLIDE_COUNT)
    miss_full = bot._missing_slides(slug)
    assert miss_full == [], f"9장인데 누락: {miss_full}"

    (bot.OUTPUT_DIR / slug / "slide-03.png").unlink()
    (bot.OUTPUT_DIR / slug / "slide-09.png").unlink()
    miss_partial = bot._missing_slides(slug)
    assert miss_partial == ["slide-03.png", "slide-09.png"], f"부분 누락 오류: {miss_partial}"
    print(f"  ✅ _missing_slides: 0장→9, 9장→0, 7장→{miss_partial}")
    return True


async def scenario_B_silent_fail():
    """run_generator 가 (False, 로그) 반환 시(무음 실패 감지됨)
    trigger_generation_direct 는 False 반환 + 완료기록·릴스 미실행 + 실패 메시지."""
    _use_tmp_output()
    slug = "topic-b"
    sink = []
    tracker = []
    _reset_downstream(tracker)

    async def fake_run_generator(s):
        return (False, "이미지 생성 실패 — PNG 0/9장만 생성됨. 누락: slide-01.png ...")
    bot.run_generator = fake_run_generator

    res = await bot.trigger_generation_direct(make(sink), slug)
    dump(sink, "B) 무음 실패 → trigger_generation_direct False")
    print(f"  반환={res}  다운스트림={tracker}")
    assert res is False, "실패인데 True 반환"
    assert tracker == [], f"실패인데 다운스트림 실행됨: {tracker}"
    assert any("생성 실패" in t for k, t in sink if k == "text"), "실패 메시지 누락"
    assert not any("✅ 완료" in t for k, t in sink if k == "text"), "실패인데 완료 메시지 나감"
    print("  ✅ False 반환 + add_done/블로그/릴스 미실행 + 실패 보고")
    return True


async def scenario_C_success_preserved():
    """run_generator (True,'') + PNG 9장 존재 → True 반환 + 완료기록·전송 정상."""
    _use_tmp_output()
    slug = "topic-c"
    sink = []
    tracker = []
    _reset_downstream(tracker)
    _make_pngs(slug, bot.SLIDE_COUNT)

    async def fake_run_generator(s):
        return (True, "all OK")
    bot.run_generator = fake_run_generator

    sent = []

    async def fake_send_slides(update, s):
        sent.append(s)
    bot.send_slides = fake_send_slides

    res = await bot.trigger_generation_direct(make(sink), slug)
    dump(sink, "C) 정상(9장) → trigger_generation_direct True")
    print(f"  반환={res}  전송={sent}  다운스트림={[t[0] for t in tracker]}")
    assert res is True, "정상인데 True 아님"
    assert sent == [slug], "9장 전송 안 함"
    assert ("add_done", slug) in tracker, "완료 기록 누락"
    assert any("✅ 완료" in t for k, t in sink if k == "text"), "완료 메시지 누락"
    print("  ✅ True 반환 + 전송 + 완료기록 정상 유지")
    return True


async def scenario_D_batch_counts_fail():
    """이미지 실패 시 auto_pipeline 이 예외를 올려 run_batch 가 '실패'로 집계."""
    _use_tmp_output()
    sink = []

    # STEP 1~3 가짜 (API 0회)
    bot.generate_sources = lambda topic_kr, slug, today, api_key: {"claims": []}
    bot.verify_sources = lambda sources, topic_kr, api_key: {"claims": []}

    async def fake_generate_template(vs, topic_kr, slug, api_key, forced_tone=""):
        return ({"slides": [{"n": 1}]}, "editorial-modern")
    bot.generate_template = fake_generate_template

    async def fake_korean_to_slug(topic_kr):
        return "foreign-object-ingestion-emergency"
    bot.korean_to_slug = fake_korean_to_slug

    # 이미지 생성은 '실패'(무음 실패 감지됨) 시뮬
    async def fake_trigger_false(update, slug):
        await update.message.reply_text("❌ 생성 실패. 마지막 로그: PNG 0/9")
        return False
    bot.trigger_generation_direct = fake_trigger_false

    # run_batch 로 1개 주제 처리 → 실패 1 집계 기대
    bot.session = {}
    await bot.run_batch(make(sink), [{"topic": "이물질 삼킴 응급 처치", "tone": ""}])
    dump(sink, "D) 이미지 실패 → batch 실패 집계")
    summary = [t for k, t in sink if k == "text" and "배치 완료" in t]
    assert summary, "배치 완료 요약 누락"
    assert "성공 0개 / 실패 1개" in summary[0], f"실패 집계 오류: {summary[0]!r}"
    print(f"  ✅ batch 가 이미지 실패를 '실패'로 집계: {summary[0].splitlines()[0]}")
    return True


async def scenario_E_regen_images():
    """/regen: 이미지만 재생성 — 성공 시 전송, 실패 시 보고. sources/template 재생성 안 함."""
    tmp = _use_tmp_output()
    slug = "topic-e"
    # 템플릿은 존재해야 진행됨
    (bot.TEMPLATES_DIR / f"slides.{slug}.json").write_text("{}", encoding="utf-8")

    # STEP1~3 함수가 호출되면 안 됨 → 호출 시 터지도록 감시
    called_pipeline = []
    bot.generate_sources = lambda *a, **k: called_pipeline.append("sources")
    bot.generate_template = lambda *a, **k: called_pipeline.append("template")

    # E-1) 재생성 성공
    sink1 = []

    async def fake_run_ok(s):
        return (True, "OK")
    bot.run_generator = fake_run_ok
    sent = []

    async def fake_send_slides(update, s):
        sent.append(s)
    bot.send_slides = fake_send_slides

    await bot.regen_images_only(make(sink1), slug)
    dump(sink1, "E-1) /regen 성공 → 이미지만 전송")
    assert sent == [slug], "재생성 후 전송 안 함"
    assert any("재생성 완료" in t for k, t in sink1 if k == "text"), "완료 메시지 누락"
    assert called_pipeline == [], f"이미지만 재생성인데 글/템플릿 재생성됨: {called_pipeline}"

    # E-2) 재생성 실패(무음 실패 감지)
    sink2 = []

    async def fake_run_fail(s):
        return (False, "PNG 0/9장만 생성됨")
    bot.run_generator = fake_run_fail
    sent.clear()
    await bot.regen_images_only(make(sink2), slug)
    dump(sink2, "E-2) /regen 실패 → 보고, 전송 안 함")
    assert sent == [], "실패인데 전송함"
    assert any("재생성 실패" in t for k, t in sink2 if k == "text"), "실패 메시지 누락"
    print("  ✅ /regen: 성공=이미지만 전송, 실패=보고, 글/템플릿 무변경")
    return True


async def main():
    print("=" * 60)
    print("이미지 무음 실패 감지 Mock 테스트 — 실제 API/이미지 0회 ($0)")
    print("=" * 60)
    scenarios = [
        scenario_A_missing_detection,
        scenario_B_silent_fail,
        scenario_C_success_preserved,
        scenario_D_batch_counts_fail,
        scenario_E_regen_images,
    ]
    results = []
    for fn in scenarios:
        try:
            await fn()
            results.append((fn.__name__, True))
        except AssertionError as e:
            print(f"  ❌ 실패: {e}")
            results.append((fn.__name__, False))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            results.append((fn.__name__, False))

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  결과: {passed}/{len(results)} 통과")
    print("=" * 60)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
