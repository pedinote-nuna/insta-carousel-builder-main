# HANDOFF.md — 소아과언니 카드뉴스 시스템 작업 지시서

> ⚠️ 이 문서는 초기 시스템 세팅(2026-05-08 완료) 시 사용한 작업 지시서입니다.
> 시스템은 이미 구축되어 있으며, 이 문서는 참조용으로 보존됩니다.

## 컨텍스트
- 이 레포는 AINOW의 `insta-carousel-builder` 오픈소스를 클론한 것
- 소아과언니 브랜드(@dr.soa_unnie)용으로 커스터마이즈할 것
- 운영자는 **비개발자**. 친절하게, 단계적으로, 매 단계 끝날 때마다 한국어로 보고할 것

---

## 먼저 읽어야 할 정책 파일 (`knowledge/`)

다음 4개 파일을 먼저 읽고 정책을 완전히 숙지할 것. **모든 작업은 이 정책 파일을 따른다**:

1. `knowledge/brand-facts.md` — 브랜드 SSOT (정체성, 톤, 디자인 DNA)
2. `knowledge/source-policy.md` — 정보 출처 정책 (Tier 1·2·3 + 금지)
3. `knowledge/topic-selection.md` — 주제 선정 정책 (6필터, 월별 캘린더)
4. `knowledge/banned-words.json` — 금지 표현, 표준 명칭

---

## 절대 위반 금지
- 사실은 `source-policy.md` 의 **Tier 1·2 출처에서만** 인용 (블로그·지식인·일반 기사 등 금지)
- 개인정보(실명·병원명·거주지·가족 정보) 노출 금지
- **"소아청소년과 전문의"** 정식 명칭 사용 ("소아과 전문의" 금지)
- 독자 호칭은 **"부모님들"·"보호자분들"** ("엄마들"/"여러분" 금지)
- 인스타 핸들은 정확히 `@dr.soa_unnie` (dr 다음에 점, soa 다음에 밑줄)

---

## 작업 단계 (차례대로 진행, 각 단계 끝나면 사용자 확인 받고 다음으로)

### 1단계: knowledge/ 폴더 점검
- 위 4개 파일 외에 어떤 파일이 있는지 사용자에게 보고
- 원본 레포의 파일(`tone.md`, `patterns.md`, `EXAMPLE-ainow.md` 등)이 있으면, 우리 브랜드와 충돌하는지 평가하고 처리 방안 제안 (삭제 / 재작성 / 유지 중 어느 것이 좋은지)
- 사용자 결정 받고 처리

### 2단계: CLAUDE.md 업데이트
- 루트의 `CLAUDE.md` 를 소아과언니 브랜드용으로 다시 작성
- `knowledge/` 정책 파일 4개를 모든 작업의 기준으로 명시
- AINOW 브랜드 흔적(이름·핸들·예시)은 모두 제거하거나 소아과언니로 교체
- 절대 위반 금지 룰을 최상단에 박음

### 3단계: 에이전트 프롬프트 강화 (`.claude/agents/`)
존재하는 에이전트들(researcher / writer / reviewer 등)에 다음 책임 부여:
- **researcher**: 자료 수집 시 `source-policy.md` 의 Tier 1·2 출처만 사용. 각 사실에 출처를 명시 인용 형식(예: "AAP Clinical Practice Guideline, 2024")으로 기록.
- **writer**: `brand-facts.md` 의 톤·금지표현·디자인 DNA 준수. 9슬라이드 구조 따름.
- **reviewer**: 모든 사실 주장이 출처와 1:1 매핑됐는지 검증. `banned-words.json` 위반 검출. `topic-selection.md` 의 6필터 통과 여부 점검.

### 4단계: 슬래시 커맨드 정비 (`.claude/commands/`)
- 우리 워크플로에 맞게 인자·동작 다듬기
- 사용 예시를 한국어로 정리

### 5단계: 디자인 시스템 적용 (`templates/`)
- `slides.example.json` 의 `common_style` 필드를 **팔레트 A (Editorial Coral)** 값으로 교체
- `slides.example-paletteB.json` 을 새로 만들어 **팔레트 B (Medical Teal)** 시안 보존
- 양쪽 모두 `brand-facts.md` 의 디자인 DNA 명세와 정확히 일치해야 함
- 1080×1350 해상도, Pretendard 폰트, 좌측 정렬 + 좌측 14px 액센트 바

### 6단계: 첫 예시 토픽 만들기
**주제**: "열나는 아이, 옷 벗기지 마세요" (5월 시의성 + 응급 카테고리, 팔레트 B)
- `templates/slides.fever-clothing.json` 생성
- 9슬라이드 프롬프트 작성 + **슬라이드별 출처 매핑**
- 인용 우선순위:
  - AAP Clinical Practice Guideline (Fever in Children)
  - 대한소아청소년과학회 발열 관련 지침
  - UpToDate "Fever in children: Pathophysiology and management"
  - Nelson Textbook of Pediatrics (현행판)
- 6필터 모두 통과 확인 (topic-selection.md 기준)

### 7단계: 보안
- `.gitignore` 에 다음 추가:
  - `knowledge/banned-words.json` (개인정보 포함)
  - `output/` (생성물)
  - `.tmp-prompt.txt` (있다면)
- 커밋 전 점검

---

## 보고 형식 (매 단계마다)
1. **무엇을 했는지** (변경된 파일 목록)
2. **발견한 이슈** (있다면)
3. **사용자 확인이 필요한 결정 사항**
4. **다음 단계로 넘어가도 되는지**

운영자가 비개발자이므로 기술적 결정도 한국어로 풀어서 설명할 것. 코드를 보여줄 때는 무엇을 의미하는지 같이 알려줄 것.

---

## 참고
- 발행 페이스: 주 3개 (월 12개)
- 디자인은 A + B 하이브리드 (주제 무게에 따라 팔레트 전환)
- 정보 신뢰도가 이 브랜드의 핵심 자산. 출처 정책을 절대 양보하지 말 것.
