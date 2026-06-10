#!/usr/bin/env python3
"""
estimate_srt.py — 대본만으로 SRT 자동 생성 (음성 생성 불필요)

지은님 ElevenLabs(+1.1배속, 무음제거) 파이프라인으로 실제 측정한
대본 17건을 역분석한 결과:  재생시간 ≈ 0.149초 × 한글 음절 수
(평균오차 ±1.1초, 최대 ±2.9초)

이 스크립트는 대본의 음절 수로 각 줄의 길이를 추정해 SRT를 만듭니다.
음성을 만들어 측정할 필요 없이, 대본이 나오면 바로 SRT가 나옵니다.

────────────────────────────────────────────────────────────
입력(.txt) — 한 줄 = 자막 한 큐:
    음성용텍스트 || 자막용텍스트
  · 길이 추정은 '음성용텍스트'의 음절로 계산(실제 읽는 말이므로).
  · '||' 가 없으면 자막형을 자동 도출(브랜드·약어만 안전 치환).
  · 숫자가 있는 줄은 정확도를 위해 '||' 로 자막형을 직접 적으세요.
  · '#' 으로 시작하는 줄과 빈 줄은 무시.

사용:
    python estimate_srt.py script.txt -o out.srt
    python estimate_srt.py script.txt -o out.srt --total 28.5   # 실제 총초를 알면 그 길이에 맞춰 스케일
    python estimate_srt.py script.txt --rate 0.149              # 보정값 변경
────────────────────────────────────────────────────────────
"""

import argparse
import re
import sys

# 보정 상수 (데이터 17건 기준). 새 데이터가 쌓이면 RECALIBRATE 참고.
DEFAULT_RATE = 0.149          # 초 / 한글음절
DEFAULT_GAP = 0.0             # 큐 사이 간격(초)

# ── 음성형 → 자막형 안전 치환 (긴 토큰 우선) ────────────────
REPLACEMENTS = [
    ("소아청소년꽈", "소아청소년과"),
    ("소아꽈수첩", "소아과수첩"),
    ("소아꽈언니", "소아과언니"),
    ("소아꽈", "소아과"),
    ("에스피에프", "SPF"),
    ("에이에이피", "AAP"),
    ("세계보건기구", "WHO"),
    ("디트", "DEET"),
    ("일일구", "119"),
]
# 참고: 음성형의 한글 숫자(오분, 다섯 번 등)는 자동 변환하지 않는다.
# 한국어 어미(–세요, –십시오 등)가 숫자·단위 글자와 겹쳐 오탐이 잦으므로,
# 숫자가 있는 줄은 '|| 자막형'으로 직접 명시하는 방식만 신뢰한다.


def syllables(text):
    """한글 음절 수 (공백·문장부호·숫자·영문 제외)."""
    return sum(1 for ch in text if "\uac00" <= ch <= "\ud7a3")


def to_subtitle(voice_text):
    """브랜드·약어만 안전 변환. 숫자는 건드리지 않음."""
    text = voice_text
    for a, b in REPLACEMENTS:
        text = text.replace(a, b)
    return text


def parse_script(path):
    cues = []
    has_auto = False
    src = sys.stdin if path == "-" else open(path, encoding="utf-8")
    with src as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "||" in line:
                voice, sub = [x.strip() for x in line.split("||", 1)]
            else:
                voice = line
                sub = to_subtitle(voice)
                has_auto = True
            cues.append({"voice": voice, "sub": sub, "syl": syllables(voice)})
    return cues, has_auto


def fmt_ts(sec):
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues, rate, gap, total=None):
    durs = [max(c["syl"], 1) * rate for c in cues]
    if total is not None:
        # 실제 총초를 알면 모양은 음절비율, 합은 total 에 정확히 맞춤
        scale = total / sum(durs)
        durs = [d * scale for d in durs]
    # 끝점을 10ms 격자에 스냅하고 마지막을 total(or 합)로 강제
    target = total if total is not None else sum(durs)
    ends, acc = [], 0.0
    for d in durs:
        acc += d
        ends.append(round(acc, 2))
    ends[-1] = round(target, 3)
    lines, prev = [], 0.0
    for i, (cue, end) in enumerate(zip(cues, ends), 1):
        start = prev
        lines += [str(i), f"{fmt_ts(start)} --> {fmt_ts(end)}", cue["sub"], ""]
        prev = end + gap
    return "\n".join(lines).rstrip() + "\n", target


def main():
    ap = argparse.ArgumentParser(description="대본 → 예측 SRT")
    ap.add_argument("script", help="입력 대본(.txt) 또는 '-' (stdin)")
    ap.add_argument("-o", "--out", default=None, help="출력 SRT 경로(미지정 시 stdout)")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE, help="초/음절 (기본 0.149)")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP, help="큐 간격(초)")
    ap.add_argument("--total", type=float, default=None, help="실제 총 길이(초)를 알면 그에 맞춰 스케일")
    args = ap.parse_args()

    cues, has_auto = parse_script(args.script)
    if not cues:
        sys.exit("큐가 없습니다.")
    if has_auto:
        sys.stderr.write(
            "안내: '||' 없는 줄은 브랜드·약어만 자동 변환했습니다. "
            "숫자(오분→5분 등)는 자동 변환하지 않으니, 숫자 있는 줄은 '|| 자막형'으로 적어주세요.\n"
        )

    srt, total = build_srt(cues, args.rate, args.gap, args.total)
    total_syl = sum(c["syl"] for c in cues)
    mode = "실제총초 스케일" if args.total is not None else f"예측(±1.1초)"
    sys.stderr.write(f"큐 {len(cues)}개 · 음절 {total_syl} · 총 {total:.2f}s · {mode}\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(srt)
        sys.stderr.write(f"저장: {args.out}\n")
    else:
        sys.stdout.write(srt)


if __name__ == "__main__":
    main()
