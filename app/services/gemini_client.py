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
from collections.abc import Mapping
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
    model_changed: bool = False
    original_model: str = ""


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


def _validate_response_schema(value: Any, schema: Optional[dict], path: str = "$") -> None:
    """후보 모델 확정 전에 required/type 중심의 최소 JSON Schema를 검증."""
    if not schema:
        return
    expected = schema.get("type")
    valid = True
    if expected == "object":
        valid = isinstance(value, dict)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    if not valid:
        raise GeminiError(f"모델 응답 형식이 스키마와 다릅니다: {path}는 {expected}여야 합니다.")
    if expected == "array" and len(value) < int(schema.get("minItems", 0) or 0):
        raise GeminiError(f"모델 응답 형식의 항목 수가 부족합니다: {path}")
    if expected == "array" and schema.get("maxItems") is not None and len(value) > int(schema["maxItems"]):
        raise GeminiError(f"모델 응답 형식의 항목 수가 너무 많습니다: {path}")
    if expected == "object":
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise GeminiError(f"모델 응답 형식에 필수 항목이 없습니다: {path}.{', '.join(missing)}")
        for key, child_schema in schema.get("properties", {}).items():
            if key in value:
                _validate_response_schema(value[key], child_schema, f"{path}.{key}")
    elif expected == "array" and schema.get("items"):
        for index, item in enumerate(value):
            _validate_response_schema(item, schema["items"], f"{path}[{index}]")


def _extract_status_code(exc: Exception) -> Optional[int]:
    """google-genai/httpx 버전별 예외 형태에서 HTTP 상태 코드를 찾는다."""
    candidates = [
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ]
    for value in candidates:
        if hasattr(value, "value"):
            value = value.value
        try:
            code = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 100 <= code <= 599:
            return code
    return None


def _friendly_error(exc: Exception) -> GeminiError:
    """SDK/네트워크 예외 → 한국어 GeminiError 변환."""
    if isinstance(exc, GeminiError):
        return exc
    code = _extract_status_code(exc)
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


def _value(model, *names):
    for name in names:
        if isinstance(model, Mapping) and name in model:
            return model[name]
        value = getattr(model, name, None)
        if value is not None:
            return value
    return None


def _model_capability(model) -> Optional[bool]:
    actions = _value(
        model,
        "supported_actions",
        "supported_generation_methods",
        "supportedActions",
        "supportedGenerationMethods",
    )
    if actions is None:
        return None
    if isinstance(actions, Mapping):
        actions = [key for key, enabled in actions.items() if enabled]
    elif isinstance(actions, str):
        actions = [actions]
    try:
        values = list(actions)
    except TypeError:
        values = [actions]
    normalized = set()
    for action in values:
        value = getattr(action, "value", None) or getattr(action, "name", None) or action
        normalized.add(re.sub(r"[^a-z]", "", str(value).lower()))
    return "generatecontent" in normalized


def _model_name(model) -> str:
    value = _value(model, "name", "model", "id")
    return str(value or "").removeprefix("models/")


def _is_text_model(name: str) -> bool:
    lower = name.lower()
    excluded = ("embedding", "imagen", "image", "tts", "audio", "live", "robotics", "aqa")
    return lower.startswith("gemini") and not any(token in lower for token in excluded)


def rank_model_names(names: list[str], preferred: str = "") -> list[str]:
    """사용자 선호 → stable Flash → Flash-Lite → Pro → preview 순으로 정렬."""
    unique = list(dict.fromkeys(name.removeprefix("models/") for name in names if _is_text_model(name)))

    def score(name: str):
        lower = name.lower()
        if name == preferred or lower == preferred.lower():
            family = 0
        elif "flash" in lower and "lite" not in lower:
            family = 10
        elif "flash" in lower and "lite" in lower:
            family = 20
        elif "pro" in lower:
            family = 30
        else:
            family = 40
        stability = 100 if any(token in lower for token in ("preview", "experimental", "exp")) else 0
        latest = -20 if "latest" in lower else 0
        versions = [int(value) for value in re.findall(r"\d+", lower)]
        version_score = -sum(value * (100 ** (len(versions) - index)) for index, value in enumerate(versions)) if versions else 0
        return family + stability + latest, version_score, lower

    return sorted(unique, key=score)


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

    def _call_without_limiter(self, fn: Callable[[], Any]) -> Any:
        """모델 목록 같은 비생성 제어 요청은 generation RPM 대기와 분리한다."""
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                err = _friendly_error(exc)
                if not err.retryable or attempt >= self.max_retries:
                    raise err from exc
                self._sleep(min(2 ** attempt * 2, 30))
                attempt += 1

    def _call_generation(self, fn: Callable[[str], Any]) -> Any:
        """현재 모델 404 시 후보마다 원래 요청 자체를 실행하고 성공 모델만 확정."""
        failed_model = self.model
        try:
            return self._call_with_retry(lambda: fn(failed_model))
        except GeminiError as exc:
            if exc.code != 404:
                raise
            first_error = exc

        candidates = [
            name for name in rank_model_names(self.list_models(), failed_model)
            if name != failed_model
        ]
        failures: list[str] = []
        for candidate in candidates[:10]:
            try:
                result = self._call_with_retry(lambda candidate=candidate: fn(candidate))
            except GeminiError as exc:
                response_format_failure = exc.code is None and any(
                    token in str(exc) for token in ("JSON", "응답이 비어", "응답 형식")
                )
                if (
                    (exc.code in {400, 403, 404} and "API Key가 올바르지" not in str(exc))
                    or response_format_failure
                ):
                    failures.append(candidate)
                    continue
                raise
            self.model = candidate  # 실제 triggering operation이 성공한 뒤에만 commit
            return result
        detail = f" 확인 실패 후보: {', '.join(failures)}" if failures else ""
        raise GeminiError(
            f"기존 모델({failed_model})을 찾을 수 없고 대체 모델도 사용할 수 없습니다.{detail}",
            code=first_error.code,
        )

    # ------------------------------------------------------------------
    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        config = self._make_config(system_instruction=system_instruction, temperature=temperature)

        def call(model: str):
            resp = self._client.models.generate_content(model=model, contents=prompt, config=config)
            text = resp.text or ""
            if not text.strip():
                raise GeminiError("모델 응답이 비어 있습니다.")
            return text

        return self._call_generation(call)

    def generate_json(
        self,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = 0.2,
        validator: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        """JSON 모드로 호출하고 파싱된 객체를 반환한다."""
        return self.generate_json_from_contents(
            prompt,
            schema=schema,
            system_instruction=system_instruction,
            temperature=temperature,
            validator=validator,
        )

    def generate_json_from_contents(
        self,
        contents: Any,
        *,
        schema: Optional[dict] = None,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = 0.2,
        validator: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        """텍스트 또는 멀티모달 contents를 구조화 JSON으로 생성한다."""
        config = self._make_config(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )

        def call(model: str):
            resp = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            data = extract_json(resp.text)
            _validate_response_schema(data, schema)
            if validator:
                validator(data)
            return data

        return self._call_generation(call)

    def generate_pdf_json(
        self,
        pdf_path,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = 0.1,
        max_inline_bytes: int = 13 * 1024 * 1024,
        validator: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        """Gemini 네이티브 문서 비전으로 PDF를 직접 분석한다.

        Base64 변환과 요청 메타데이터 여유를 두기 위해 원본을 13MB로 제한한다.
        그보다 큰 스캔 PDF는 로컬 Tesseract OCR 또는 파일 분할이 필요하다.
        """
        from pathlib import Path

        path = Path(pdf_path)
        if not path.exists():
            raise GeminiError(f"PDF 파일을 찾을 수 없습니다: {path}")
        size = path.stat().st_size
        if size > max_inline_bytes:
            raise GeminiError(
                f"PDF가 {size / 1024 / 1024:.1f}MB로 너무 큽니다. "
                "13MB 이하로 나누거나 Tesseract OCR을 설정해 주세요."
            )
        data = path.read_bytes()
        if self._types is None:
            part = {"inline_data": {"mime_type": "application/pdf", "data": data}}
        else:
            part = self._types.Part.from_bytes(data=data, mime_type="application/pdf")
        return self.generate_json_from_contents(
            [part, prompt],
            schema=schema,
            system_instruction=system_instruction,
            temperature=temperature,
            validator=validator,
        )

    def list_models(self) -> list[str]:
        """generateContent 를 지원하는 모델 ID 목록."""

        def call():
            names = []
            for model in self._client.models.list():
                capability = _model_capability(model)
                if capability is False:
                    continue
                name = _model_name(model)
                if _is_text_model(name):
                    names.append(name)
            return rank_model_names(names, self.model)

        return self._call_without_limiter(call)

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        original_model = self.model
        try:
            text = self.generate_text("연결 테스트입니다. 'OK' 한 단어로만 답하세요.", temperature=0)
        except GeminiError as exc:
            return ConnectionTestResult(
                ok=False,
                model=self.model,
                message=str(exc),
                original_model=original_model,
            )
        latency = int((time.monotonic() - started) * 1000)
        changed = self.model != original_model
        prefix = f"기존 모델을 사용할 수 없어 {self.model}로 자동 변경했습니다. " if changed else ""
        return ConnectionTestResult(
            ok=True,
            model=self.model,
            latency_ms=latency,
            model_changed=changed,
            original_model=original_model,
            message=f"{prefix}연결 성공 ({self.model}, {latency}ms) — 응답: {text.strip()[:40]}",
        )
