---
description: 소아과언니 카드뉴스 9장 풀세트 생성 (리서치 → 출처 매핑 → 본문 → 이미지 → 검증)
argument-hint: <주제/키워드>
---

운영자가 **"$ARGUMENTS"** 주제로 소아과언니 카드뉴스 9장 풀세트를 요청했습니다.

`CLAUDE.md` 의 절대 위반 금지 5종 + 품질 킬라인 10항목 + 4-Phase Pipeline 을 따라 **9장 PNG + sources.json 출처 매핑 + 검수 리포트**를 생산합니다.

---

## 0. 사전 로드 (생략 금지)

다음 6개 파일을 모두 Read 한 뒤 시작:

1. `knowledge/brand-facts.md` — 브랜드 SSOT + 디자인 DNA 팔레트 A/B
2. `knowledge/source-policy.md` — Tier 1·2 출처 + 인용 형식
3. `knowledge/topic-selection.md` — 6필터·월별 캘린더·카테고리 비율
4. `knowledge/banned-words.json` — 금칙어·표준 명칭
5. `knowledge/patterns/carousel-structure.md` — 9장 구조 + 5종 본문 패턴
6. `knowledge/patterns/sources-schema.md` — sources.json 규약

**출력 폴더**: `output/$(date +%Y-%m-%d)_<topic_slug>` (예: `output/2026-05-08_fever-clothing`)

---

## 엔진 결정 — 시작 전 1회만

- 운영자가 명시 안 했으면 **기본값: 🎨 HTML/Puppeteer** (정확도 우선, 비용 0원)
- 운영자가 "AI 이미지", "다양한 시안", "빠르게" 등 명시 시 → 🍌 나노바나나
- 애매하면 운영자에게 1줄 질문: `"🍌 나노바나나(빠른 실험) vs 🎨 HTML(정확/0원) 중 어느 엔진?"`

---

## Phase 1 — 리서치 + 출처 매핑

**`carousel-researcher` 서브에이전트 dispatch**:

> "$ARGUMENTS" 주제로 소아과언니 카드뉴스 리서치 브리프를 작성해줘.
>
> 출력 두 개:
> 1. `output/<폴더>/brief.json`
>    - topic·topic_kr·category·palette (A 또는 B 결정 근거 포함)
>    - filter_check (6필터 통과 여부, 5/6 미만이면 작업 중단·운영자 보고)
>    - selected_pattern (5종 본문 패턴 A~E 중 1개)
>    - cover_hook (4가지 후킹 패턴 중 하나)
>    - nine_slide_outline (1~9 각각 role + core_message)
>    - saturated_patterns (피해야 할 진부한 표현)
>
> 2. `output/<폴더>/sources.json`
>    - `knowledge/patterns/sources-schema.md` 스키마 그대로
>    - 슬라이드별 사실 주장(claim) 추출 + Tier 1·2 출처 매핑
>    - 모든 claim 에 publication_date, applicable_age, last_accessed_at 포함
>    - 출처 부족하면 그 슬라이드를 outline 에서 빼고 운영자 보고
>
> 절대 위반 금지: Tier 3 이하 출처 / 블로그 / 지식인 / 일반 기사 인용 금지.

→ 두 파일 생성 후 운영자에게 "Phase 1 완료, brief 와 sources 검토하시겠습니까?" 보고.

---

## Phase 2 — 본문 작성 (엔진별 분기)

### Phase 2a (기본) — HTML 트랙

**`carousel-html-writer` 서브에이전트 dispatch**:

> brief.json 과 sources.json 을 읽고 9장 HTML 슬라이드를 작성해줘.
>
> - 출력 위치: `output/<topic>/slides/slide-01.html ~ slide-09.html`
> - 팔레트는 brief.json 의 `palette` 필드(A 또는 B)에 따라 CSS `:root` 변수 분기
> - 각 본문 슬라이드 `<body data-claim-ids="...">` 어트리뷰트로 sources.json 매핑
> - claim_text 의 의학적 사실은 100% 보존, 표현만 부모 친화적으로 풀기
> - 슬라이드 9 출처 박스 의무 + 소아과수첩 앱 CTA + `@dr.soa_unnie`
> - 사용한 claim 의 `writer_used: true` 마킹
> - 새 사실 주장 만들지 말 것 (sources.json 에 없으면 본문 금지)

### Phase 2b (옵션) — 나노바나나 트랙

**`carousel-prompt-writer` 서브에이전트 dispatch**:

> brief.json 과 sources.json 을 읽고 Gemini 3.0 Pro Image 용 9장 프롬프트 JSON 을 작성해줘.
>
> - 출력 위치: `templates/slides.<topic>.json`
> - common_palette 는 brief.json palette 필드에 따라 A 또는 B
> - 각 슬라이드 `claim_ids` 필드로 sources.json 매핑
> - Cover + Outro 한글 spelling 강조 명시
> - 슬라이드 9 출처 박스 + 소아과수첩 앱 CTA + `@dr.soa_unnie`
> - 사용한 claim 의 `writer_used: true` 마킹

---

## Phase 3 — 이미지 생성

### Phase 3a — HTML 트랙 (기본)

```bash
node scripts/html-carousel-gen.js --topic <topic_slug>
```

- Puppeteer 가 9개 HTML 을 1080×1350 PNG 로 캡처
- 비용 0원, 약 1~2분
- 실패 시 해당 slide-XX.html 만 수정 후 재캡처

### Phase 3b — 나노바나나 트랙

```bash
python scripts/nanobanana-gen.py --topic <topic_slug> --slides templates/slides.<topic>.json
```

- Gemini 3.0 Pro Image 9장 생성 (약 4분, 약 500~1000원)
- 출력: `output/<topic>/slide-01.png ~ slide-09.png`
- 시리즈 일관성은 **톤·컬러·폰트·시그니처 텍스트** 로 자연스럽게 형성됨
  (좌측 액센트 바·하단 footer 같은 strict 시각 룰 없음 — 모델 자율성 최대화)
- 매 슬라이드에 "소아과언니" 한글 작게 노출, slide-9 에 핸들·앱·출처 박스
- 실패한 슬라이드만 재실행

→ 인스타 업로드용 최종본은 `output/<topic>/slide-01.png ~ slide-09.png`.

> 참고: 이전에 사용하던 후처리 시리즈 요소 덧씌우기 스크립트
> (`scripts/post-process-overlay.py`)는 폐기되어 `scripts/_deprecated/`
> 로 이동되었습니다. strict 시각 일관성은 모델 자율성을 침해해
> 일러스트를 단조롭게 만든다는 결론에 따라, 톤·컬러·폰트·시그니처
> 텍스트 기반의 가벼운 일관성 모델로 전환되었습니다.

---

## Phase 4 — 품질·출처 검증

**`carousel-reviewer` 서브에이전트 dispatch**:

> output/<topic>/ 의 9장 PNG 와 sources.json 을 검증해줘.
>
> 1. 자동 검사: `node scripts/quality-check.js --dir output/<topic>`
> 2. 자동 FAIL 8가지 점검 (sources.json 미존재·Tier 위반·orphan claim·출처 박스 미표기·banned-words·개인정보·핸들 오타 등)
> 3. 경고 4가지 점검 (publication_date 5년 이상·applicable_age 본문 누락·UpToDate last_accessed_at 누락·연령 표기 불일치)
> 4. 6필터 재확인 (brief.json filter_check)
> 5. 10항목 점수 채점 + 한글 오타 slide별 육안 확인
> 6. 본문 ↔ sources.json claims 1:1 매핑 검증 (의학적 사실 주장 정의 기준)
> 7. sources.json `verification` 섹션 + 각 claim 의 `reviewer_pass` 마킹
> 8. 판정 (PASS / HOLD / FAIL) — 자동 FAIL 1건이라도 있으면 FAIL
>
> 직접 고치지 말고 지적만. 재생성·수정은 메인 Claude / writer / researcher 권한.

→ `output/<topic>/quality-report.md` 저장.

---

## 5. 마무리

- `output/<topic>/README.md` 자동 작성:
  - 주제 요약 + 인스타 캡션 초안 + 출처 정리 + 사용 가이드
- `output/<topic>/metadata.json` 에 생성 메타 (비용·시간·품질 점수·자동 FAIL 건수) 기록

---

## 완료 후 운영자에게 보고할 것

```
✅ "$ARGUMENTS" 카드뉴스 9장 완성
📁 output/<topic>/
🎨 엔진: HTML/Puppeteer (또는 나노바나나)
🎨 팔레트: A (Coral) 또는 B (Teal)

## 생성 결과
- 9/9 성공 (또는 N/9 + 실패 사유)
- 비용: 0원 (HTML) / 약 NNN원 (나노바나나)
- 시간: N분 N초
- 평균 파일 크기: NNN KB

## 출처 매핑 (sources.json)
- 총 claim 수: NN개
- Tier 1: NN / Tier 2: NN
- writer_used: NN/NN (모두 true 가 정상)
- reviewer_pass: NN/NN

## 자동 FAIL 점검
- 거부 사유: 0건 (또는 N건 — 사유 나열)

## 경고 (WARN)
- 출처 노후화: 0건 / N건 (claim_id 나열)
- 연령 명시 누락: 0건 / N건
- UpToDate 접근일 누락: 0건 / N건

## 품질 리뷰 (carousel-reviewer)
- 총점: NN/100
- 판정: PASS / HOLD / FAIL
- 한글 오타: slide-XX (N건)
- 상위 개선 포인트 3개: [...]

## 운영자 판단 필요
- 재생성 필요 슬라이드: slide-XX (이유)
- 업로드 추천: YES / NO / 보류
```
