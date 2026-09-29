"""Windows용 GUI 실행기.

배치 창을 남기지 않고 앱을 시작하며, 시작 단계에서 예외가 발생하면
사용자가 확인할 수 있도록 오류 대화상자와 로그 파일을 남긴다.
"""
from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ERROR_LOG = ROOT / "실행오류.txt"


def show_error(message: str) -> None:
    """PyQt 자체를 불러오지 못한 상황에서도 Windows 대화상자를 표시한다."""
    ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
        0,
        message,
        "지문·문제 재구성 스튜디오 - 실행 오류",
        0x10,
    )


def run() -> int:
    os.chdir(ROOT)
    try:
        from main import main

        return main()
    except Exception:  # noqa: BLE001 - 시작 오류를 사용자에게 보여 주기 위한 최상위 처리
        details = traceback.format_exc()
        try:
            ERROR_LOG.write_text(details, encoding="utf-8")
        except OSError:
            pass
        show_error(
            "앱을 시작하지 못했습니다.\n\n"
            "프로젝트 폴더에 생성된 '실행오류.txt'를 보내 주세요.\n\n"
            + details[-1800:]
        )
        return 1


if __name__ == "__main__":
    sys.exit(run())
