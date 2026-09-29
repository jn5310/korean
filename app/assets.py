"""콘텐츠 이미지의 검증·재인코딩·해시 기반 영구 저장소."""
from __future__ import annotations

import hashlib
import io
import os
import uuid
import warnings
from pathlib import Path

from .models import MediaAsset


class AssetError(Exception):
    pass


class AssetStore:
    MAX_INPUT_BYTES = 25 * 1024 * 1024
    MAX_PIXELS = 40_000_000

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.assets_dir = self.data_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def import_bytes(
        self,
        data: bytes,
        *,
        source_page: int = 0,
        bbox: list[float] | None = None,
        anchor: str = "after",
        offset: int = 0,
        alt: str = "문제 그림",
    ) -> MediaAsset:
        if not data or len(data) > self.MAX_INPUT_BYTES:
            raise AssetError("이미지가 비어 있거나 25MB 제한을 초과했습니다.")
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise AssetError("Pillow가 설치되어 있지 않습니다.") from exc

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as probe:
                    width, height = probe.size
                    if width <= 0 or height <= 0 or width * height > self.MAX_PIXELS:
                        raise AssetError("이미지 크기가 올바르지 않거나 픽셀 제한을 초과했습니다.")
                    if width * height * 4 > self.MAX_PIXELS * 4:
                        raise AssetError("이미지 압축 해제 크기가 제한을 초과했습니다.")
                    probe.verify()
                with Image.open(io.BytesIO(data)) as original:
                    transposed = ImageOps.exif_transpose(original)
                    transposed.load()
                    width, height = transposed.size
                    if transposed.mode in {"RGBA", "LA"} or "transparency" in transposed.info:
                        converted = transposed.convert("RGBA")
                    else:
                        converted = transposed.convert("RGB")
                    output = io.BytesIO()
                    converted.save(output, format="PNG", optimize=True)
                    normalized = output.getvalue()
        except AssetError:
            raise
        except Exception as exc:  # noqa: BLE001 - Pillow decoder 예외가 다양함
            raise AssetError(f"지원하지 않거나 손상된 이미지입니다: {exc}") from exc

        digest = hashlib.sha256(normalized).hexdigest()
        relative = Path("assets") / digest[:2] / f"{digest}.png"
        target = self.data_dir / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AssetError(f"이미지 자산 폴더를 만들지 못했습니다: {exc}") from exc
        if not target.exists():
            temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(normalized)
                os.replace(temporary, target)
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                if not target.is_file():
                    raise AssetError(f"이미지 자산을 저장하지 못했습니다: {exc}") from exc
        return MediaAsset(
            relative_path=relative.as_posix(),
            mime_type="image/png",
            width=width,
            height=height,
            source_page=source_page,
            bbox=bbox or [],
            anchor=anchor,
            offset=offset,
            alt=alt,
        )

    def import_file(self, path: Path, **metadata) -> MediaAsset:
        source = Path(path)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise AssetError(f"이미지 파일을 읽지 못했습니다: {exc}") from exc
        return self.import_bytes(data, alt=metadata.pop("alt", source.stem), **metadata)

    def resolve(self, asset: MediaAsset | str) -> Path | None:
        relative = asset.relative_path if isinstance(asset, MediaAsset) else str(asset)
        if not relative:
            return None
        try:
            candidate = (self.data_dir / relative).resolve()
            candidate.relative_to(self.assets_dir)
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_file() else None
