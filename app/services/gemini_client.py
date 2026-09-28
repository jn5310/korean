"""Google Gemini API 연동 모듈.

- 공식 Google Gen AI SDK(`google-genai`) 사용. 구 `google-generativeai` 패키지는
  지원 종료(deprecated)되어 신규 SDK 로 작성했다.
- 무료 tier 의 분당 요청 제한(RPM)을 고려한 간단한 Rate Limiter 와
  429/5xx 에 대한 지수 백오프 재시도를 내장한다.
- GUI 스레드를 막지 않도록 호출은 반드시 워커 스레드(app/ui/workers.py)에서 한다.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..config import DEFAULT_GEMINI_MODEL

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GeminiError(Exception):
    """사용자에게 보여줄 수 있는 한국어 메시지를 담은 예외."""

    def __init__(self, message: str, *, code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class GeminiNotInstalledError(GeminiError):
    pass


@dataclass
class ConnectionTestResult:
    ok: bool
    model: str
    message: str
    latency_ms: int = 0


class RateLimiter:
    """호출 간 최소 간격을 보장하는 스레드 안전 리미터 (RPM 기반)."""

    def __init__(self, requests_per_minute: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next_allowed = now + self.min_interval


def extract_json(text: str) -> Any:
    """모델 응답에서 JSON 을 꺼낸다. ```json 코드펜스나 앞뒤 설명문이 섞여도 처리."""
    if text is None:
        raise GeminiError("모델 응답이 비어 있습니다.")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 본문 중 첫 번째 JSON 객체/배열 구간을 찾아 재시도
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if starts:
        start = min(starts)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise GeminiError("모델 응답을 JSON 으로 해석할 수 없습니다.")


def _friendly_error(exc: Exception) -> GeminiError:
    """SDK/네트워크 예외 → 한국어 GeminiError 변환."""
    if isinstance(exc, GeminiError):
        return exc
    code = getattr(exc, "code", None)
    code = code if isinstance(code, int) else None
    raw = str(exc)
    lowered = raw.lower()

    if code == 400 and ("api key" in lowered or "api_key" in lowered):
        return GeminiError("API Key 가 올바르지 않습니다. 설정에서 다시 확인해 주세요.", code=code)
    if code in (401, 403):
        return GeminiError("API Key 권한이 없거나 해당 모델에 접근할 수 없습니다.", code=code)
    if code == 404:
        return GeminiError("모델을 찾을 수 없습니다. 설정에서 모델 이름을 확인해 주세요.", code=code)
    if code == 429:
        return GeminiError("요청 한도(무료 tier RPM/일일 한도)를 초과했습니다. 잠시 후 다시 시도해 주세요.",
                           code=code, retryable=True)
    if code is not None and code >= 500:
        return GeminiError(f"Gemini 서버 오류({code})가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                           code=code, retryable=True)
    name = type(exc).__name__.lower()
    if "timeout" in name or "connect" in name or "network" in name:
        return GeminiError("네트워크 연결 또는 응답 시간 초과 문제입니다.", retryable=True)
    return GeminiError(f"Gemini 호출 실패: {raw}", code=code)


class GeminiClient:
    """Gemini 호출 래퍼.

    사용 예:
        client = GeminiClient(api_key="...", model="gemini-2.5-flash")
        client.test_connection()
        data = client.generate_json("...", schema={...})
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        timeout_sec: int = 60,
        max_retries: int = 3,
        requests_per_minute: float = 10,
        sdk_client: Any = None,               # 테스트용 주입
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key and sdk_client is None:
            raise GeminiError("Gemini API Key 가 설정되지 않았습니다.")
        self.model = model or DEFAULT_GEMINI_MODEL
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        self._limiter = RateLimiter(requests_per_minute, sleep=sleep)
        self._types = None
        self._client = sdk_client if sdk_client is not None else self._build_sdk_client(api_key, timeout_sec)

    # ------------------------------------------------------------------
    def _build_sdk_client(self, api_key: str, timeout_sec: int):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiNotInstalledError(
                "google-genai 패키지가 설치되어 있지 않습니다. `pip install google-genai` 를 실행해 주세요."
            ) from exc
        self._types = types
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_sec * 1000)),
        )

    def _make_config(self, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if self._types is None:
            return kwargs or None  # 주입된 테스트 클라이언트는 dict 설정을 받는다
        return self._types.GenerateContentConfig(**kwargs) if kwargs else None

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            self._limiter.acquire()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - SDK 예외 타입이 다양함
                err = _friendly_error(exc)
                if not err.retryable or attempt >= self.max_retries:
                    raise err from exc
                delay = min(2 ** attempt * 2, 30)
                logger.warning("Gemini 재시도 %d/%d (%.0fs 후): %s", attempt + 1, self.max_retries, delay, err)
                self._sleep(delay)
                attempt += 1

    # ------------------------------------------------------------------
    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        config = self._make_config(system_instruction=system_instruction, temperature=temperature)

        def call():
            resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
            return resp.text or ""

        return self._call_with_retry(call)

    def generate_json(
        self,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = 0.2,
    ) -> Any:
        """JSON 모드로 호출하고 파싱된 객체를 반환한다 (Step 3 난이도 판별에서 사용)."""
        config = self._make_config(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )

        def call():
            resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
            return extract_json(resp.text)

        return self._call_with_retry(call)

    def list_models(self) -> list[str]:
        """generateContent 를 지원하는 모델 ID 목록."""

        def call():
            names = []
            for m in self._client.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                name = getattr(m, "name", "") or ""
                names.append(name.removeprefix("models/"))
            return sorted(set(n for n in names if n.startswith("gemini")))

        return self._call_with_retry(call)

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            text = self.generate_text("연결 테스트입니다. 'OK' 한 단어로만 답하세요.", temperature=0)
        except GeminiError as exc:
            return ConnectionTestResult(ok=False, model=self.model, message=str(exc))
        latency = int((time.monotonic() - started) * 1000)
        return ConnectionTestResult(
            ok=True, model=self.model, latency_ms=latency,
            message=f"연결 성공 ({self.model}, {latency}ms) — 응답: {text.strip()[:40]}",
        )
