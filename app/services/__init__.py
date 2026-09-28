"""비즈니스 로직 서비스 (Qt 비의존).

- gemini_client : Gemini API 연동 (Step 1)
- pdf_parser    : PDF 텍스트 추출/OCR/지문-문제 파싱 (Step 2)
- difficulty    : Gemini 난이도 판별 (Step 3)
- pdf_exporter  : 1지문-1페이지 PDF 출판 (Step 5)
"""
