# 지문·문제 재구성 스튜디오

PDF(텍스트/스캔본)에서 지문과 문제를 추출하고, Gemini API 로 난이도(1~5단계)를 자동 분류한 뒤,
직접 편집하여 **1지문-1페이지** 교재와 맞춤형 시험지를 PDF 로 출판하는 PyQt6 데스크톱 프로그램입니다.

## 실행

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   /   macOS·Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

- Python 3.10 이상
- Gemini API Key: [Google AI Studio](https://aistudio.google.com/apikey) 에서 무료 발급 → 앱의 **설정(Ctrl+,)** 에 입력
  (또는 환경변수 `GEMINI_API_KEY`)
- 스캔본 OCR(Step 2)에는 [Tesseract](https://github.com/tesseract-ocr/tesseract) 와 한국어 데이터(`kor`)가 필요합니다.

> PRD 에는 `google-generativeai` 로 되어 있으나 해당 패키지는 지원 종료(deprecated)되어,
> Google 이 권장하는 후속 SDK 인 `google-genai` 를 사용합니다.

## 프로젝트 구조

```
main.py                     # 진입점
app/
├── config.py               # 설정(JSON) · 난이도/유형 상수
├── models.py               # Passage(지문) 1:N Question(문제), ExamSet(시험지), Library
├── storage.py              # 라이브러리 JSON 저장/불러오기(원자적 저장)
├── services/               # ── Qt 비의존 비즈니스 로직
│   ├── gemini_client.py    # [Step 1] Gemini 연동 (재시도·RPM 제한·JSON 모드)
│   ├── pdf_parser.py       # [Step 2] 텍스트 추출 + OCR + 지문/문제 파싱   (인터페이스)
│   ├── difficulty.py       # [Step 3] AI 난이도 판별                       (인터페이스)
│   └── pdf_exporter.py     # [Step 5] 1지문-1페이지 PDF 출판               (인터페이스)
└── ui/                     # ── PyQt6 화면
    ├── main_window.py      # 내비게이션 · 메뉴 · 상태표시줄
    ├── dashboard.py        # 메인: PDF 업로드(드래그 앤 드롭) · 분석 시작 · API 상태 · 현황
    ├── settings_dialog.py  # API Key · 모델 · 연결 테스트 · OCR 설정
    ├── editor_view.py      # 편집·검수: 목록 | 지문/문제 편집 | 미리보기
    ├── test_creator.py     # 시험지 생성: 조건별 탐색기 | 작업 공간(드래그 정렬)
    ├── state.py            # 공유 상태(설정/라이브러리/스레드 풀) · 시그널
    ├── workers.py          # QThreadPool 백그라운드 작업 (GUI 멈춤 방지)
    ├── widgets.py, styles.py
```

데이터(설정·라이브러리)는 OS 사용자 데이터 폴더에 저장됩니다
(Windows `%APPDATA%\KoreanStudio`, macOS `~/Library/Application Support/KoreanStudio`,
Linux `~/.local/share/korean-studio`). `STUDIO_DATA_DIR` 환경변수로 변경할 수 있습니다.

## 마일스톤

| 단계 | 내용 | 상태 |
|---|---|---|
| Step 1 | GUI 기본 틀 + Gemini API 연동 모듈 | ✅ 완료 |
| Step 2 | PDF 텍스트 추출 + OCR + 지문/문제 파싱 | ⏳ 인터페이스만 |
| Step 3 | Gemini 난이도(1~5) 자동 판별 · JSON 구조화 | ⏳ 인터페이스만 |
| Step 4 | 편집기 · 필터링 · 시험지 구성 고도화 | 🔶 기본 동작 구현 |
| Step 5 | ReportLab 1지문-1페이지 PDF 내보내기 | ⏳ 인터페이스만 |

Step 1 에서 동작하는 것: 설정 저장, API Key 연결 테스트, 모델 목록 불러오기, PDF 파일 등록(분석은 Step 2),
지문/문제 수동 입력·수정·삭제, 난이도/유형 수동 지정, 조건별 필터, 시험지 구성(담기·드래그 정렬), JSON 저장/가져오기.
