1) src/lg_oven_update.py -> 기존 파일 전체 덮어쓰기
2) .github/workflows/lg_oven_weekly.yml -> 기존 파일 전체 덮어쓰기
3) requirements.txt -> 기존 파일 전체 덮어쓰기
4) Actions 탭에서 'Run workflow' 실행 (Re-run jobs 아님)
5) 잘못 들어간 2026.04.23 오염 5행은 삭제 후 테스트

이 수정안은 검색결과 카드 텍스트를 바로 쓰지 않고,
LG 제품 상세페이지(PDP) 링크를 먼저 수집한 뒤,
각 상세페이지에서 P/N과 가격을 뽑아 List 형식으로 적재합니다.
