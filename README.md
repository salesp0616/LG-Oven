# LG Oven Weekly Auto Update - GitHub Actions + Playwright

## 필요한 GitHub Secrets
- `SHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `KNOB_OVERRIDES_JSON` (선택)

## 빠른 설정
1. 이 폴더를 GitHub 저장소에 업로드
2. Google Cloud 서비스 계정 생성
3. 서비스 계정 JSON 전체를 `GOOGLE_SERVICE_ACCOUNT_JSON` Secret에 저장
4. 대상 Google Sheet를 서비스 계정 이메일에 편집 권한으로 공유
5. 시트 ID를 `SHEET_ID` Secret에 저장
6. GitHub Actions 활성화
7. `LG Oven Weekly Update` 워크플로 수동 1회 실행
8. 이후 매일 08:00 KST 자동 실행

## 시트 구조
- `List`: 주간 Top 1~5 누적
- `Raw_Last_Run`: 마지막 수집 원문
- `Run_Log`: 성공/실패 로그
- `Overrides`: P/N별 Knob O/X 강제값
- `Control`: 런타임 설정
- `Guide`: 설명

## 참고
- 수집은 Apps Script가 아니라 Playwright 브라우저 자동화로 수행
- 저장은 Google Sheets API 사용
- LG 페이지 구조 변경 시 `src/lg_oven_update.py`의 selector/파싱 로직 수정
