"""Qt 비의존 비즈니스 로직 서비스.

- analysis_pipeline : 추출→구조 복원→AI 교차 검토 통합
- pdf_parser        : PDF 텍스트·서식·그림 추출, 품질 판정, 스캔 OCR
- structure_parser  : 지문 1:N 오지선다 규칙 기반 구조화
- rich_content      : 굵게·색상·밑줄·이미지 sidecar 투영
- document_analyzer : Gemini 문서 비전 및 구조 교차 검토
- difficulty        : Gemini 난이도(1~5)·유형 판별
- gemini_client     : Gemini API 재시도·모델 자동 복구·JSON/PDF 입력
- pdf_exporter      : 리치 서식·그림을 포함한 실제 시험지 PDF 출판
"""
