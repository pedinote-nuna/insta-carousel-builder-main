output/$ARGUMENTS/script.txt 를 읽고 아래 작업을 순서대로 실행해줘.

## 1단계: voiceover.txt 생성

output/$ARGUMENTS/voiceover.txt 를 만들고 script.txt 내용을 그대로 넣되,
숫자가 포함된 줄에는 끝에 `|| 자막형` 을 추가해줘.

자막형 변환 규칙:
- 한글 숫자 → 아라비아 숫자: 오분→5분, 이십분→20분, 삼십분→30분
- 한글 숫자 → 아라비아 숫자: 육개월→6개월, 구개월→9개월, 십팔개월→18개월
- 한글 숫자 → 아라비아 숫자: 이백→200, 사백→400, 삼십팔→38
- 일일구→119, 일일이→112
- 에스피에프→SPF, 에이에이피→AAP, 디트→DEET, 히파→HEPA
- 삼십팔도→38℃, 이십육도→26℃
- 소아꽈→소아과, 소아청소년꽈→소아청소년과

숫자 없는 줄은 `||` 없이 그대로.

예시:
```
소아청소년꽈 전문의가 알려드립니다.
오분 이상 경련이면 일일구에 전화하세요. || 5분 이상 경련이면 119에 전화하세요.
바닥에 눕혀 옆으로 돌려주세요.
소아꽈언니의 소아꽈수첩입니다.
```

## 2단계: SRT 생성

터미널에서 아래 명령어를 실행해줘:

```
python scripts/estimate_srt.py output/$ARGUMENTS/voiceover.txt -o output/$ARGUMENTS/output.srt
```

## 3단계: 결과 확인

output/$ARGUMENTS/output.srt 내용을 출력해줘.
