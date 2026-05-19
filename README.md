# 소아과언니 카드뉴스 시스템

> 권위 출처(Tier 1·2)에 기반한 의료 정보 카드뉴스 자동 생성 시스템.
> Claude Code 하네스 + 듀얼 엔진 (HTML/Puppeteer 기본 · 나노바나나 옵션).

[@dr.soa_unnie](https://www.instagram.com/dr.soa_unnie/) 인스타 카드뉴스 production 도구. 모든 사실은 학회 가이드라인·교과서·peer-reviewed 논문에서만 인용하며, 출처 매핑이 누락된 슬라이드는 reviewer 에이전트가 발행을 차단합니다.

---

## 정책 4파일 (모든 작업의 기준)

- [knowledge/brand-facts.md](knowledge/brand-facts.md) — 브랜드 SSOT + 디자인 DNA
- [knowledge/source-policy.md](knowledge/source-policy.md) — Tier 1·2 출처 + 인용 형식
- [knowledge/topic-selection.md](knowledge/topic-selection.md) — 6필터·월별 캘린더·카테고리 비율
- [knowledge/banned-words.json](knowledge/banned-words.json) — 금지 표현·표준 명칭

구조 공식: [knowledge/patterns/carousel-structure.md](knowledge/patterns/carousel-structure.md)
출처 매핑 규약: [knowledge/patterns/sources-schema.md](knowledge/patterns/sources-schema.md)

---

## 첫 토픽 만들기

Claude Code 안에서:

```
/carousel-new "열나는 아이, 옷 벗기지 마세요"
```

자동 4-Phase: **리서치 → 출처 매핑 → 본문 작성 → 품질·출처 검증**.
산출물은 `output/<topic>/` 안에 9장 HTML(또는 PNG) + `brief.json` + `sources.json` + `quality-report.md`.

기존 토픽 재검증:

```
/carousel-quality fever-clothing
```

---

## 디자인 DNA — 1080×1350, A + B 하이브리드

| 팔레트 | 액센트 | 사용 |
|:---|:---|:---|
| **A — Editorial Coral** | `#C44536` | 일상 케어·영양·성장발달·소아심장 일반·앱 (~60%) |
| **B — Medical Teal** | `#2C6E63` | 예방접종·응급·약물 안전·위험 신호·질환관리 (~40%) |

판정 한 줄: **"잘못 적용 시 즉각적 위험이 있는가?"** → YES = B / NO = A.

공통: Pretendard 폰트, 좌측 14px 액센트 바, 하단 "✓ 소아청소년과 전문의 검수" + "0X / 09" 인디케이터.

견본 HTML: [docs/sample-html-paletteA/](docs/sample-html-paletteA/) · [docs/sample-html-paletteB/](docs/sample-html-paletteB/)

---

## 절대 위반 금지 5종

1. Tier 3 이하 출처 인용 금지 (블로그·지식인·일반 기사 절대 X)
2. 개인정보 노출 금지 (실명·병원·지역·가족 정보)
3. "소아청소년과 전문의" 정식 명칭 (소아과 전문의 X)
4. 독자 호칭 "부모님들" / "보호자분들" (엄마들·여러분 X)
5. 인스타 핸들 정확히 `@dr.soa_unnie`

상세는 [CLAUDE.md](CLAUDE.md) 참조.

---

## 환경 설정

```bash
# API 키
cp .env.example .env
# .env 열어서 GEMINI_API_KEY, ANTHROPIC_API_KEY 채우기

# 의존성
npm install                              # HTML/Puppeteer 엔진 (기본)
pip install python-dotenv google-genai   # 나노바나나 엔진 (옵션)
```

git commit 전 보안 점검:

```bash
bash scripts/precommit-check.sh
```

---

## 발행 페이스

주 3개 / 월 12개. 카테고리 비율 A 7 / B 5 (`knowledge/topic-selection.md`).
자동 업로드 없음 — 운영자가 직접 인스타 업로드 (알고리즘 패턴 회피).

---

## 라이선스

이 레포는 AINOW 의 오픈소스 `insta-carousel-builder` 를 클론하여 소아과언니 브랜드용으로 커스터마이즈한 버전입니다. 원본 라이선스(MIT) 그대로 따릅니다.
