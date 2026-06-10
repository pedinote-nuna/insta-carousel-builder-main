"""/batch Mock 테스트 — 실제 API·이미지 생성 0회 ($0).

telegram-bot.py 를 import 한 뒤 auto_pipeline / korean_to_slug 를 가짜로 교체해
파싱 → 확인게이트 → 순차처리 → 에러무시 → 취소 → 요약 전 흐름을 검증한다.

실행: python3 scripts/_test_batch_mock.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT_PATH = REPO / "scripts" / "telegram-bot.py"

# --- 모듈 로드 (파일명에 하이픈이 있어 importlib 사용) ---
spec = importlib.util.spec_from_file_location("telegram_bot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

# 테스트가 운영 세션 파일(data/session.json)을 건드리지 않도록 저장 무력화
bot._save_session = lambda: None
bot.session = {}


# --- 가짜 텔레그램 객체 ---
class FakeMessage:
    def __init__(self, sink):
        self.sink = sink

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


def make(text, sink):
    return FakeUpdate(text, sink)


def dump(sink, label):
    print(f"\n── {label} ──")
    for kind, t in sink:
        first = (t or "").split("\n")[0]
        print(f"  [{kind}] {first}")


# --- 가짜 파이프라인 ---
PIPELINE_CALLS = []


def install_mock_pipeline(fail_on=None):
    """auto_pipeline / korean_to_slug 를 가짜로 교체. fail_on 주제는 예외 발생."""
    PIPELINE_CALLS.clear()

    async def fake_korean_to_slug(topic_kr):
        return topic_kr.replace(" ", "-")[:20] or "card-x"

    async def fake_auto_pipeline(update, topic_kr, slug, forced_tone=""):
        PIPELINE_CALLS.append(topic_kr)
        await update.message.reply_text(f"[mock] 파이프라인 실행: {topic_kr} ({slug})")
        if fail_on and topic_kr == fail_on:
            raise RuntimeError("강제 주입 에러")

    bot.korean_to_slug = fake_korean_to_slug
    bot.auto_pipeline = fake_auto_pipeline


# ---------------------------------------------------------------- 시나리오들
async def scenario_1():
    sink = []
    bot.session = {}
    install_mock_pipeline()
    await bot.cmd_batch(make("/batch", sink), None)
    dump(sink, "1) /batch → 주제 입력 안내")
    ok = bot.session.get("batch", {}).get("stage") == "awaiting_topics"
    assert any("줄바꿈" in t for k, t in sink if k == "text"), "안내 누락"
    print(f"  ✅ stage=awaiting_topics: {ok}")
    return ok


async def scenario_2():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_topics"}}
    install_mock_pipeline()
    await bot.handle_text(make("신생아 트림\n수족구 격리 기준\n해열제 용량", sink), None)
    dump(sink, "2) 3줄 입력 → 파싱 + 확인게이트")
    q = bot.session.get("batch", {}).get("queue", [])
    confirm = [t for k, t in sink if k == "text" and "처리할 주제 3개" in t]
    print(f"  큐={q}")
    assert len(q) == 3, f"큐 3개 아님: {q}"
    assert confirm and "$5.64" in confirm[0], "비용 계산 오류"
    assert bot.session["batch"]["stage"] == "awaiting_confirm"
    print("  ✅ 3개 파싱 + 확인게이트(이미지 3×14, $5.64)")
    return True


async def scenario_3():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_topics"}}
    install_mock_pipeline()
    # 2번째 줄 '트림'(2자)은 5자 미만 → 제외
    await bot.handle_text(make("신생아 황달 관리\n트림\n영유아 수면 교육", sink), None)
    dump(sink, "3) 짧은 줄 포함 → 제외 안내")
    q = bot.session["batch"]["queue"]
    excluded = [t for k, t in sink if k == "text" and "너무 짧아" in t]
    print(f"  큐={q}")
    assert [i["topic"] for i in q] == ["신생아 황달 관리", "영유아 수면 교육"], f"제외 실패: {q}"
    assert excluded, "제외 안내 누락"
    print(f"  ✅ 짧은 줄 제외 + 안내: {excluded[0]}")
    return True


async def scenario_4():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_topics"}}
    install_mock_pipeline()
    lines = "\n".join(f"테스트 주제 번호 {i}" for i in range(1, 12))  # 11개
    await bot.handle_text(make(lines, sink), None)
    dump(sink, "4) 11개 입력 → 앞 10개만")
    q = bot.session["batch"]["queue"]
    capped = [t for k, t in sink if k == "text" and "최대" in t]
    print(f"  큐 길이={len(q)}")
    assert len(q) == 10, f"10개 cap 실패: {len(q)}"
    assert capped, "초과 안내 누락"
    print(f"  ✅ 10개 cap + 안내: {capped[0]}")
    return True


async def scenario_5():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_confirm",
                             "queue": [{"topic": "주제 가나다", "tone": ""},
                                       {"topic": "주제 라마바", "tone": ""}]}}
    install_mock_pipeline()
    await bot.handle_text(make("확인", sink), None)
    dump(sink, "5) '확인' → 순차 처리(Mock)")
    print(f"  파이프라인 호출={PIPELINE_CALLS}")
    assert PIPELINE_CALLS == ["주제 가나다", "주제 라마바"], "순차 처리 실패"
    assert any("배치 완료" in t for k, t in sink if k == "text"), "완료 요약 누락"
    assert bot.session.get("batch") is None, "batch 미정리"
    print("  ✅ 2개 순차 처리 + 완료 요약")
    return True


async def scenario_6():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_confirm",
                             "queue": [{"topic": "첫번째 주제", "tone": ""},
                                       {"topic": "두번째 주제", "tone": ""},
                                       {"topic": "세번째 주제", "tone": ""}]}}
    install_mock_pipeline(fail_on="두번째 주제")  # 2번째 강제 에러
    await bot.handle_text(make("확인", sink), None)
    dump(sink, "6) 2번째 에러 → 멈추지 않고 3번째 진행")
    print(f"  파이프라인 호출={PIPELINE_CALLS}")
    summary = [t for k, t in sink if k == "text" and "배치 완료" in t]
    assert PIPELINE_CALLS == ["첫번째 주제", "두번째 주제", "세번째 주제"], "3개 모두 시도 안 함"
    assert summary and "성공 2개 / 실패 1개" in summary[0], f"요약 오류: {summary}"
    assert "두번째 주제" in summary[0], "실패 목록 누락"
    print(f"  ✅ 에러 무시 진행 + 실패기록: {summary[0].splitlines()[0]}")
    return True


async def scenario_7():
    sink = []
    bot.session = {"batch": {"stage": "awaiting_confirm",
                             "queue": [{"topic": "주제 하나둘", "tone": ""},
                                       {"topic": "주제 셋넷", "tone": ""}]}}
    install_mock_pipeline()

    # 1번째 파이프라인 실행 직후 취소 플래그가 켜지도록 가짜 교체
    async def fake_pipeline_then_cancel(update, topic_kr, slug, forced_tone=""):
        PIPELINE_CALLS.append(topic_kr)
        await update.message.reply_text(f"[mock] 실행: {topic_kr}")
        bot.request_cancel()  # 진행 중 /cancel 들어온 상황 모사

    bot.auto_pipeline = fake_pipeline_then_cancel
    await bot.handle_text(make("확인", sink), None)
    dump(sink, "7) 진행 중 /cancel → 정상 중단 + IDLE")
    print(f"  파이프라인 호출={PIPELINE_CALLS}")
    cancelled = [t for k, t in sink if k == "text" and "중단됨" in t]
    assert PIPELINE_CALLS == ["주제 하나둘"], "취소 후에도 계속 실행됨"
    assert cancelled, "중단 메시지 누락"
    assert bot.session.get("batch") is None, "IDLE 복귀 실패"
    print(f"  ✅ 즉시 중단 + IDLE: {cancelled[0]}")
    bot.reset_cancel_flag()
    return True


async def scenario_8():
    # /cancel 커맨드 자체 동작 (대기 상태에서)
    sink = []
    bot.session = {"batch": {"stage": "awaiting_confirm",
                             "queue": [{"topic": "주제 가나다", "tone": ""}]}}
    install_mock_pipeline()
    await bot.cmd_cancel(make("/cancel", sink), None)
    dump(sink, "8) 대기 중 /cancel → 큐 비우고 IDLE")
    assert bot.session.get("batch") is None, "batch 미정리"
    assert any("취소" in t for k, t in sink if k == "text"), "취소 메시지 누락"
    print("  ✅ /cancel → 큐 비움 + IDLE")
    bot.reset_cancel_flag()
    return True


async def scenario_9():
    """버그 가드: batch 상태가 풀린 채 여러 줄 주제 목록이 들어와도
    추천기(cmd_topics) 호출 0회 + 바로 확인게이트 + 입력 주제 그대로 표시."""
    sink = []
    bot.session = {}  # batch 상태 없음 (= 봇 재시작 등으로 풀린 상황)
    install_mock_pipeline()

    # intent_router 가 이 입력을 'topics'(추천 요청)로 오분류하는 상황 모사 ($0)
    async def fake_intent_router(text):
        return {"intent": "topics", "params": {}}
    bot.intent_router = fake_intent_router

    # 추천기 호출 여부 추적 — 호출되면 안 됨
    recommender_calls = []

    async def fake_cmd_topics(update, context):
        recommender_calls.append(True)
        await update.message.reply_text("[mock] 추천기 호출됨(있으면 안 됨)")
    bot.cmd_topics = fake_cmd_topics

    topics_in = ["신생아 트림 시키는 법", "수족구 격리 기준", "해열제 적정 용량"]
    await bot.handle_text(make("\n".join(topics_in), sink), None)
    dump(sink, "9) batch 상태 풀림 + 3줄 입력 → 추천기 0회 + 확인게이트")

    q = bot.session.get("batch", {}).get("queue", [])
    confirm = [t for k, t in sink if k == "text" and "처리할 주제 3개" in t]
    print(f"  추천기 호출수={len(recommender_calls)}  큐={[i['topic'] for i in q]}")
    # ① 추천기 호출 0회
    assert recommender_calls == [], "추천기가 호출됨 — 가드 실패"
    # ② 확인게이트로 진입
    assert bot.session.get("batch", {}).get("stage") == "awaiting_confirm", "확인게이트 미진입"
    assert len(q) == 3, f"큐 3개 아님: {q}"
    # ③ 입력 주제가 게이트에 그대로 표시
    assert confirm, "확인게이트 메시지 누락"
    for t in topics_in:
        assert t in confirm[0], f"입력 주제 '{t}' 가 게이트에 없음"
    print("  ✅ 추천기 0회 + 확인게이트 + 입력 주제 3개 그대로 표시")
    return True


async def scenario_10():
    """역검증: 진짜 1줄 추천 요청은 가드에 안 걸리고 추천기로 정상 전달."""
    sink = []
    bot.session = {}
    install_mock_pipeline()

    async def fake_intent_router(text):
        return {"intent": "topics", "params": {}}
    bot.intent_router = fake_intent_router

    recommender_calls = []

    async def fake_cmd_topics(update, context):
        recommender_calls.append(True)
        await update.message.reply_text("[mock] 추천기 정상 호출")
    bot.cmd_topics = fake_cmd_topics

    await bot.handle_text(make("주제 추천해줘", sink), None)
    dump(sink, "10) 1줄 추천 요청 → 추천기 정상 호출(가드 미발동)")
    print(f"  추천기 호출수={len(recommender_calls)}")
    assert recommender_calls == [True], "추천기가 호출되지 않음 — 가드 오작동"
    assert not bot.session.get("batch"), "batch 가 잘못 설정됨"
    print("  ✅ 1줄 추천 요청은 정상적으로 추천기로 감")
    return True


async def main():
    print("=" * 60)
    print("/batch Mock 테스트 — 실제 API/이미지 생성 0회 ($0)")
    print("=" * 60)
    results = []
    for fn in (scenario_1, scenario_2, scenario_3, scenario_4,
               scenario_5, scenario_6, scenario_7, scenario_8,
               scenario_9, scenario_10):
        try:
            await fn()
            results.append((fn.__name__, True))
        except AssertionError as e:
            print(f"  ❌ 실패: {e}")
            results.append((fn.__name__, False))
        except Exception as e:  # noqa: BLE001
            print(f"  💥 예외: {e}")
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
