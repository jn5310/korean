"""라이브러리(지문/문제/시험지) JSON 영속화."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from .models import Library

logger = logging.getLogger(__name__)


class LibraryRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Library:
        if not self.path.exists():
            return Library()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Library.from_dict(data)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # 손상된 파일은 백업해 두고 빈 라이브러리로 시작 (데이터 유실 방지)
            backup = self.path.with_suffix(".corrupt.json")
            shutil.copy2(self.path, backup)
            logger.error("라이브러리 파일 손상 → %s 로 백업 후 새로 시작합니다: %s", backup, exc)
            return Library()

    def save(self, library: Library) -> None:
        """임시 파일에 쓴 뒤 교체하는 원자적 저장."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(library.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def export_to(self, library: Library, target: Path) -> None:
        Path(target).write_text(
            json.dumps(library.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def import_from(source: Path) -> Library:
        return Library.from_dict(json.loads(Path(source).read_text(encoding="utf-8")))
