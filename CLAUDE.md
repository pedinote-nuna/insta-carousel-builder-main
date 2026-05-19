# 소아과언니 카드뉴스 시스템 — Claude Code 지시서

> 이 레포는 **소아과언니(@dr.soa_unnie)** 브랜드의 인스타 카드뉴스를 Claude Code에서 직접 제작하는 도구입니다.
> 운영자가 "이 주제로 카드뉴스 만들어줘"라고 요청하면 **리서치 → 9슬라이드 프롬프트 설계 → 이미지 생성 → 품질·출처 검증**까지 자동 수행합니다.

---

## 🚫 절대 위반 금지 (모든 작업의 최상위 규칙)

이 다섯 가지는 **어떤 상황에서도 예외 없음**. 위반 시 reviewer 에이전트가 발행을 막아야 함.

1. **출처**: 모든 사실 주장은 [knowledge/source-policy.md](knowledge/source-policy.md) 의 **Tier 1·2 출처에서만** 인용.
   - 일반 블로그·네이버 지식인·비전문가 SNS·일반 기사 인용 절대 금지.
   - 출처가 불분명하면 그 슬라이드 자체를 만들지 않음.
2. **개인정보**: 실명·병원명·거주 시·구·가족 정보(배우자·자녀 이름) 일체 노출 금지.
3. **정식 명칭**: "소아청소년과 전문의" 사용. ❌ "소아과 전문의" / ❌ "소아과의사".
4. **독자 호칭**: "부모님들"(1순위) / "보호자분들"(2순위). ❌ "엄마들" / ❌ "어머님들" / ❌ "여러분".
5. **인스타 핸들**: 정확히 `@dr.soa_unnie` (dr 다음에 점, soa 다음에 밑줄). ❌ `drsoa_unnie` / `dr_soa_unnie` / `dr.soaunnie` 등 전부 오타.

---

## 📚 작업 전 반드시 Read (생략 금지)

**매 작업마다** 다음 4개 파일을 정책 기준으로 삼는다:

1. [knowledge/brand-facts.md](knowledge/brand-facts.md) — 브랜드 SSOT (정체성·톤·디자인 DNA 팔레트 A/B). **모든 수치·명칭은 이 파일에서만 인용**.
2. [knowledge/source-policy.md](knowledge/source-policy.md) — 정보 출처 정책 (Tier 1·2·3 + 금지 출처 + 인용 형식).
3. [knowledge/topic-selection.md](knowledge/topic-selection.md) — 주제 선정 6필터·월별 캘린더·카테고리 비율.
4. [knowledge/banned-words.json](knowledge/banned-words.json) — 금지 표현·표준 명칭·핸들 오타 패턴.

**구조 공식**:
- [knowledge/patterns/carousel-structure.md](knowledge/patterns/carousel-structure.md) — 9슬라이드 구조 + 5종 본문 패턴 + reviewer 거부 사유.

**참고 자산**(있을 때):
- `knowledge/tone/` — 어투 예시 모음 (현재 비어 있음, 추후 채울 예정)
- `knowledge/reference/` — 자주 인용하는 학회 가이드라인 PDF·링크 (현재 비어 있음, 추후 채울 예정)

---

## 즉시 실행 원칙

운영자가 아래처럼 요청하면 `/carousel-new <주제>` 파이프라인을 실행합니다:

- "열나는 아이 옷 벗기지 말라는 주제로 카드뉴스 만들어줘"
- "예방접종 후 미열, 정상 vs 위험 카드뉴스 써줘"
- "수족구 격리 9장으로 만들자"

수동 호출: `/carousel-quality` (이미 만들어진 폴더의 9장 PNG 품질 재검증).

---

## 🔀 듀얼 엔진 — 어느 쪽으로 갈 것인가

이 레포는 **두 가지 이미지 생성 엔진**을 지원합니다. 운영자 의도에 따라 자동 판단:

| 엔진 | 사용 영역 | 스크립트 | 서브에이전트 |
|:---|:---|:---|:---|
| 🍌 **나노바나나** (기본 메인) | 일상 케어·영양·성장발달·앱 활용 등 일반 교육 주제 | `scripts/nanobanana-gen.py` | `carousel-prompt-writer` (JSON) |
| 🎨 **HTML/Puppeteer** (정확성 트랙) | **응급·약물 용량·예방접종 시기** 등 한 글자 오류가 의료 위험을 초래하는 주제 한정 | `scripts/html-carousel-gen.js` | `carousel-html-writer` (HTML) |

**운영자가 명시 안 하면 기본값**: 🍌 **나노바나나** (본방 메인).
- 부모 친화적 일러스트 자동 생성 + 디자인 다양성으로 인스타 알고리즘에 유리.
- 단, 본문에 **수치(체온·체중당 mg/kg)·약물명·예방접종 시기·응급 신호** 가 포함되면 → HTML 트랙으로 명시 전환.

**자동 판단 애매할 때 — 운영자에게 1줄 질문**:
> "🍌 나노바나나(시각 다양성) vs 🎨 HTML(수치 정확) 중 어느 엔진?"

### 엔진별 Phase 3 분기

```
Phase 3a (나노바나나, 본방 production):
  python scripts/nanobanana-gen.py --topic <키워드> --slides templates/slides.<topic>.json
  # → 인스타 업로드용 최종본: output/<키워드>/slide-01~09.png

Phase 3b (HTML):
  # 1. carousel-html-writer 가 output/<topic>/slides/slide-01~09.html 작성
  # 2. node scripts/html-carousel-gen.js --topic <keyword>
```

### 본방 production 방침 — 가벼운 일관성 모델
나노바나나 트랙은 **모델 자율성을 보장**하여 일러스트를 풍성하게 만들고,
시리즈 일관성은 **톤·컬러·폰트·시그니처 텍스트** 4가지로 형성한다:

1. **톤**: 모던 의학 에디토리얼
2. **컬러**: 팔레트 A(coral) 또는 B(teal) 중 하나, 주의 머스타드 #B8860B 공통
3. **폰트**: Pretendard 같은 한글 sans-serif, 헤드라인 마지막 단어 액센트
4. **시그니처**: "소아과언니" 한글 매 슬라이드 작게 노출,
   slide-1(Cover) 상단에 `✓ 소아청소년과 전문의` 강조 배지 의무(모든 커버 공통 — 첫 장 신뢰 신호),
   slide-9 에 `@dr.soa_unnie` + 소아청소년과 전문의 + 소아과수첩 앱 + 출처 박스

좌측 14px 액센트 바·하단 footer·페이지 번호 같은 strict 시각 룰과
후처리 스크립트(`scripts/post-process-overlay.py`)는 **폐기**.
좌표·픽셀 단위 강제 룰이 모델 자율성을 침해해 일러스트를 단조롭게
만든다는 결론(2026-05-08 옵션 C 검증 4회차).

폐기된 후처리 스크립트는 `scripts/_deprecated/` 에 보존 (참조용).
HTML 트랙은 영향 없음 — 응급·약물·예방접종 등 정확성이 결정적인 주제 한정.

---

## 기본 산출물 — 9장 풀세트

**모든 카드뉴스 요청은 기본적으로 9장 세트를 생산합니다:**
- Cover 1장 + 본문 7장 + Outro 1장 (구조 공식: [knowledge/patterns/carousel-structure.md](knowledge/patterns/carousel-structure.md))
- 슬라이드 9(Outro)에는 **출처 박스가 의무** — Tier 1·2 출처를 명시
- 각 슬라이드는 `templates/slides.{topic}.json` 의 프롬프트로 정의

9장이 아닌 개수 지정은 운영자가 명시한 경우만 수용.

---

## 디자인 DNA — 팔레트 A + B 하이브리드

상세 명세는 [knowledge/brand-facts.md](knowledge/brand-facts.md) 참조. 핵심 요약:

| 팔레트 | 액센트 | 적용 카테고리 |
|:---|:---|:---|
| **A — Editorial Coral** | `#C44536` | 일상 케어·영양·성장발달·소아심장 일반 교육·앱 활용 (~60%) |
| **B — Medical Teal** | `#2C6E63` | 예방접종·응급·약물 안전·위험 신호·질환관리 (~40%) |

**팔레트 결정 한 줄 룰**:
> "이 정보를 잘못 적용하면 아이 건강에 즉각적 위험이 있는가?"
> YES → B / NO → A / 애매 → A(기본값)

**공통**: 1080×1350, Pretendard 같은 한글 sans-serif, 좌측 정렬, 잡지 에디토리얼 톤, 헤드라인 마지막 단어를 액센트 컬러로 강조. 시리즈 일관성은 **톤·컬러·폰트·시그니처 텍스트("소아과언니" 한글 매 슬라이드 노출)** 로 형성 — 좌측 액센트 바·하단 footer·페이지 번호 같은 strict 시각 룰은 사용하지 않음 (모델 자율성 보장).

---

## 품질 킬라인 (9장 각각에 적용)

| # | 기준 | 허용 범위 |
|:---:|:---|:---|
| 1 | **해상도** | 1080×1350 (4:5 인스타 권장) |
| 2 | **한글 렌더링** | 오타 없어야 함 (`scripts/quality-check.js` 자동 검사) |
| 3 | **디자인 DNA 일관성** | 9장 배경색·폰트·액센트 컬러 통일 (한 캐러셀 = 한 팔레트) |
| 4 | **Cover 후킹** | [carousel-structure.md](knowledge/patterns/carousel-structure.md) 의 4가지 패턴(통념 깨기·기준 제시·위험 신호·궁금증 해소) 중 하나 + 상단 `✓ 소아청소년과 전문의` 강조 배지 필수 |
| 5 | **1장당 정보 밀도** | 한 슬라이드 = 한 메시지 |
| 6 | **출처 매핑** | 본문 사실 주장이 슬라이드 9 출처와 1:1 매핑 |
| 7 | **Outro CTA** | "저장 / 공유 / 팔로우" 중 1개 + 소아과수첩 앱 안내 |
| 8 | **금칙어** | [banned-words.json](knowledge/banned-words.json) 위반 0건 |
| 9 | **톤** | 전문의의 신뢰 + 동네 언니의 친근함. 의학용어는 즉시 일상어로 풀이 |
| 10 | **개인정보** | 실명·병원·지역·가족 정보 0건 |

→ `scripts/quality-check.js` 가 생성 직후 자동 검사. 한글 오타 감지 시 해당 슬라이드만 재생성.

---

## 3-Phase Pipeline (`/carousel-new` 풀 파이프라인)

```
Phase 1 [리서치/기획]   → carousel-researcher 서브에이전트
                           Tier 1·2 출처 수집, 슬라이드별 출처 매핑 초안,
                           9슬라이드 구조 초안, hook 후보 생성
Phase 2 [프롬프트 설계]  → carousel-prompt-writer (또는 carousel-html-writer)
                           9장 JSON 또는 HTML 작성 (templates/slides.{topic}.json)
                           brand-facts 의 톤·금지표현·디자인 DNA 준수
Phase 3 [이미지 생성]    → nanobanana-gen.py 또는 html-carousel-gen.js
                           1080×1350 PNG 9장 생성
Phase 4 [품질·출처 검증] → scripts/quality-check.js + carousel-reviewer
                           오타 + 금칙어 + 출처 매핑 + 6필터 + 개인정보 검증
                           실패 시 해당 슬라이드 재생성
```

**서브에이전트 원칙**:
- 작성자(writer)와 검증자(reviewer) 분리 — 자기채점 편향 방지
- reviewer 는 출처 1:1 매핑이 안 되거나 banned-words 위반이면 **무조건 거부**
- 메인 Claude 는 오케스트레이터, 프롬프트 수정은 writer 가 함

---

## 출력 구조

```
output/{YYYY-MM-DD}_{주제압축}/
├── slide-01.png ~ slide-09.png   # 1080×1350 PNG 9장
├── prompts.json                  # 사용된 프롬프트 (재현용)
├── sources.json                  # 슬라이드별 인용 출처 매핑
├── metadata.json                 # 생성 시간·비용·품질 리포트
└── README.md                     # 주제 요약 + 인스타 캡션 초안 + 출처
```

**명명 규칙**: 공백·특수문자 금지. 예) `2026-05-08_fever-clothing`.

---

## Zero-Inference 원칙

- 수치·명칭은 반드시 `brand-facts.md` 또는 Tier 1·2 출처에서만 인용
- 이미지의 한글 오타 판정은 **`scripts/quality-check.js`** 가 수행 (LLM 판단 금지)
- 출처가 부족하면 콘텐츠를 만들지 말고 운영자에게 보고

---

## 환경 설정

`.env` 파일 (git 추적 금지):
```
GEMINI_API_KEY=AIza...         # 나노바나나 엔진 사용 시만 필수 (https://aistudio.google.com/apikey)
ANTHROPIC_API_KEY=              # (옵션) reviewer 서브에이전트 API 호출용
```

### 의존성

```bash
# 나노바나나 엔진
pip install python-dotenv google-genai

# HTML/Puppeteer 엔진 (기본값)
npm install   # puppeteer 자동 설치
```

- Python ≥ 3.10 (나노바나나)
- Node.js ≥ 20 (HTML/Puppeteer + 품질 체크 훅)
- (권장) Pretendard 폰트 시스템 설치 — HTML 트랙 한글 렌더링 품질 향상

---

## 운영 페이스 (참고)

- 발행: **주 3개** (월 12개)
- 카테고리 비율은 [topic-selection.md](knowledge/topic-selection.md) 참조 (월 12개 기준 A 7 / B 5)
- 매주 수요일에 다음 주 3개 주제 확정 (6필터 통과 후보만)
- 자동 업로드 없음 — 운영자가 직접 인스타 업로드 (알고리즘 패턴 탐지 회피)
- 생성된 이미지는 **반드시 육안 검토 후 업로드** — AI 이미지 미세 왜곡 가능

---

## 운영자 응대 원칙

운영자는 **비개발자**입니다. 모든 보고는 한국어로:
1. 무엇을 했는지 (변경된 파일 목록)
2. 발견한 이슈 (있다면)
3. 운영자 확인이 필요한 결정 사항
4. 다음 단계로 넘어가도 되는지

기술 용어는 한국어로 풀어서 설명. 코드 보여줄 때 무엇을 의미하는지 같이 알려줄 것.

---

## 참고

- 이 레포는 AINOW 의 오픈소스 `insta-carousel-builder` 를 클론하여 소아과언니 브랜드용으로 커스터마이즈한 버전
- **정보 신뢰도가 이 브랜드의 핵심 자산** — 출처 정책을 절대 양보하지 말 것
