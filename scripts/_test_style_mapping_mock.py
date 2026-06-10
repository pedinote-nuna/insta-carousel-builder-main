"""styleSelector(성격 기반 decide_tone) + /batch 수동지정 Mock 테스트 — $0.

telegram-bot.py 의 decide_tone / classify_nature / _parse_batch_topics 를
실제 호출해 '주제 성격'으로 스타일이 분산되는지, 수동지정 | 스타일이 먹는지 검증.
실행: python3 scripts/_test_style_mapping_mock.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("telegram_bot", REPO / "scripts" / "telegram-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def tone(topic, slug=""):
    return bot.decide_tone(topic, slug)


def check(label, got, expected, ok=None):
    if ok is None:
        ok = (got == expected) if isinstance(expected, str) else (got in expected)
    print(f"  {'✅' if ok else '❌'} {label}\n     → {got} (기대: {expected})")
    return ok


results = []

print("=" * 64)
print("styleSelector 성격기반 매핑 + 수동지정 Mock ($0)")
print("=" * 64)

# 1. 순서=실천형 → clean-infographic
print("\n1) '신생아 배꼽 소독 순서' → clean-infographic (실천형)")
results.append(check("배꼽 소독 순서", tone("신생아 배꼽 소독 순서"), "clean-infographic"))

# 2. 위험판단형 → emergency-alert
print("\n2) '머리 부딪혔을 때 응급실 기준' → emergency-alert (위험판단형)")
results.append(check("응급실 기준", tone("머리 부딪혔을 때 응급실 기준"), "emergency-alert"))

# 3. 일상형 → handdrawn 또는 character
print("\n3) '아기 딸꾹질 멈추는 방법' → handdrawn 또는 character (일상형)")
results.append(check("딸꾹질", tone("아기 딸꾹질 멈추는 방법"),
                     {"handdrawn-notebook", "character-illustration"}))

# 4. 비교형 → editorial-modern
print("\n4) '수족구 vs 수두 비교' → editorial-modern (비교형)")
results.append(check("수족구 vs 수두", tone("수족구 vs 수두 비교"), "editorial-modern"))

# 5. 신생아 5개 연속 → 최소 3종 이상 분산 (핵심)
print("\n5) 신생아 주제 5개 연속 → 최소 3종 분산")
newborn = [
    "신생아 배꼽 소독 순서",            # clean-infographic
    "신생아 머리 부딪힘 응급실 가는 기준",  # emergency-alert
    "신생아 딸꾹질 달래는 법",           # handdrawn-notebook
    "신생아 황달 vs 정상 피부색 구별",     # editorial-modern
    "신생아 이유식 시작 준비물",          # character-illustration
]
mapped = [tone(t) for t in newborn]
distinct = set(mapped)
for t, m in zip(newborn, mapped):
    print(f"     • {t}  →  {m}")
ok5 = len(distinct) >= 3
print(f"     분산 종류 수: {len(distinct)}종 {sorted(distinct)}")
results.append(check("5개 신생아 ≥3종 분산", len(distinct), "≥3", ok=ok5))

# 6. 수동지정 (정식명) — | emergency-alert
print("\n6) '아기 손톱 깎는 법 | emergency-alert' → 수동지정 적용")
topics6, notices6 = bot._parse_batch_topics("아기 손톱 깎는 법 | emergency-alert")
got6 = topics6[0]["tone"] if topics6 else None
results.append(check("수동지정 emergency-alert", got6, "emergency-alert"))

# 7. 수동지정 없는 스타일 → 폴백 + 안내
print("\n7) '아기 손톱 깎는 법 | 없는스타일' → 폴백(tone='') + 안내")
topics7, notices7 = bot._parse_batch_topics("아기 손톱 깎는 법 | 없는스타일")
got7_tone = topics7[0]["tone"] if topics7 else None
got7_note = any("없는 스타일" in n for n in notices7)
print(f"     tone={got7_tone!r}, 안내={got7_note}")
results.append(check("없는 스타일 폴백+안내", (got7_tone == "" and got7_note), True, ok=(got7_tone == "" and got7_note)))

# 8. 수동지정 한글 별칭 — | 경고긴급
print("\n8) '아기 손톱 깎는 법 | 경고긴급' → 한글별칭 → emergency-alert")
topics8, notices8 = bot._parse_batch_topics("아기 손톱 깎는 법 | 경고긴급")
got8 = topics8[0]["tone"] if topics8 else None
results.append(check("한글별칭 경고긴급", got8, "emergency-alert"))

print("\n" + "=" * 64)
passed = sum(1 for r in results if r)
print(f"  결과: {passed}/{len(results)} 통과")
print("=" * 64)
sys.exit(0 if passed == len(results) else 1)
