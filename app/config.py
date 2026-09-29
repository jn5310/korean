"""앱 설정 관리 (Gemini API Key, 모델, OCR 옵션 등).

Qt 에 의존하지 않도록 순수 Python(JSON)으로 구현한다.
설정 파일 위치:
  - 환경변수 STUDIO_DATA_DIR 가 있으면 그 경로
  - Windows : %APPDATA%/KoreanStudio
  - macOS   : ~/Library/Application Support/KoreanStudio
  - Linux   : ~/.local/share/korean-studio
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# Gemini 모델 기본값. 모델 ID 는 수시로 바뀌므로 설정 화면에서
# 직접 입력하거나 "모델 목록 불러오기"로 갱신할 수 있게 한다.
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
SUGGESTED_GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
    "gemini-2.5-flash",
]

# 난이도 체계 (1 = 최하, 5 = 최상)
DIFFICULTY_LEVELS = {
    1: "1단계 (최하)",
    2: "2단계 (하)",
    3: "3단계 (중)",
    4: "4단계 (상)",
    5: "5단계 (최상)",
}

# 유형 기본 제안값 — UI 에서는 자유 입력도 허용한다.
DEFAULT_PASSAGE_TYPES = [
    "인문", "사회", "과학", "기술", "예술",
    "현대시", "현대소설", "고전시가", "고전산문", "기타",
]
DEFAULT_QUESTION_TYPES = [
    "주제/요지", "제목", "내용 일치", "세부 정보", "추론",
    "빈칸", "순서 배열", "문장 삽입", "어휘", "어법/문법",
    "감상/비판", "기타",
]

ENV_API_KEY = "GEMINI_API_KEY"
ENV_DATA_DIR = "STUDIO_DATA_DIR"


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def default_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "KoreanStudio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "KoreanStudio"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "korean-studio"


@dataclass
class AppConfig:
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_timeout_sec: int = 60
    gemini_max_retries: int = 3
    gemini_requests_per_minute: int = 10
    # OCR (Step 2)
    tesseract_cmd: str = ""          # 비어 있으면 PATH 에서 탐색
    ocr_languages: str = "kor+eng"
    ocr_dpi: int = 300
    ocr_timeout_sec: int = 90
    # UI
    last_open_dir: str = ""
    recent_files: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def __post_init__(self) -> None:
        self.gemini_api_key = self.gemini_api_key if isinstance(self.gemini_api_key, str) else ""
        self.gemini_model = self.gemini_model if isinstance(self.gemini_model, str) and self.gemini_model else DEFAULT_GEMINI_MODEL
        self.gemini_timeout_sec = _bounded_int(self.gemini_timeout_sec, 60, 10, 600)
        self.gemini_max_retries = _bounded_int(self.gemini_max_retries, 3, 0, 10)
        self.gemini_requests_per_minute = _bounded_int(self.gemini_requests_per_minute, 10, 1, 60)
        self.tesseract_cmd = self.tesseract_cmd if isinstance(self.tesseract_cmd, str) else ""
        self.ocr_languages = self.ocr_languages if isinstance(self.ocr_languages, str) and self.ocr_languages else "kor+eng"
        self.ocr_dpi = _bounded_int(self.ocr_dpi, 300, 150, 600)
        self.ocr_timeout_sec = _bounded_int(self.ocr_timeout_sec, 90, 10, 600)
        self.last_open_dir = self.last_open_dir if isinstance(self.last_open_dir, str) else ""
        self.recent_files = [str(path) for path in self.recent_files] if isinstance(self.recent_files, list) else []

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigManager:
    """설정 파일 로드/저장."""

    FILE_NAME = "config.json"

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / self.FILE_NAME
        self.config = self._load()

    def _load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            return AppConfig.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("설정 파일을 읽지 못해 기본값을 사용합니다: %s", exc)
            return AppConfig()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)  # API 키가 들어 있으므로 소유자만 읽기
        except OSError:
            pass

    @property
    def effective_api_key(self) -> str:
        """설정에 저장된 키 → 환경변수 GEMINI_API_KEY 순으로 사용."""
        return self.config.gemini_api_key.strip() or os.environ.get(ENV_API_KEY, "").strip()

    def add_recent_file(self, path: str, limit: int = 10) -> None:
        recent = [p for p in self.config.recent_files if p != path]
        recent.insert(0, path)
        self.config.recent_files = recent[:limit]
