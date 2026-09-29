"""라이브러리 JSON 영속화와 이미지 포함 휴대용 .koreanlib 패키지."""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from .assets import AssetError, AssetStore
from .models import Library, MediaAsset

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
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            backup = self.path.with_suffix(".corrupt.json")
            shutil.copy2(self.path, backup)
            logger.error("라이브러리 파일 손상 → %s 로 백업 후 새로 시작합니다: %s", backup, exc)
            return Library()

    def save(self, library: Library) -> None:
        """임시 파일에 쓴 뒤 교체하는 원자적 저장."""
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(library.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def export_to(self, library: Library, target: Path) -> None:
        """이미지 없는 호환용 JSON 내보내기."""
        Path(target).write_text(
            json.dumps(library.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def export_bundle(self, library: Library, target: Path) -> tuple[Path, list[str]]:
        """library.json과 참조 이미지를 하나의 .koreanlib ZIP에 원자적으로 묶는다."""
        destination = Path(target)
        if destination.suffix.lower() != ".koreanlib":
            destination = destination.with_suffix(".koreanlib")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        warnings: list[str] = []
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "library.json",
                    json.dumps(library.to_dict(), ensure_ascii=False, indent=2),
                )
                written: set[str] = set()
                for asset in _library_assets(library):
                    if not asset.relative_path or asset.relative_path in written:
                        continue
                    source = _safe_asset_path(self.path.parent, asset.relative_path)
                    if source is None or not source.is_file():
                        warnings.append(f"누락된 그림을 패키지에 포함하지 못했습니다: {asset.alt}")
                        continue
                    archive.write(source, arcname=asset.relative_path)
                    written.add(asset.relative_path)
            os.replace(temporary, destination)
            return destination, warnings
        except (OSError, zipfile.BadZipFile) as exc:
            raise OSError(f"라이브러리 패키지를 만들지 못했습니다: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def import_bundle(self, source: Path) -> tuple[Library, list[str]]:
        """검증된 asset 항목만 현재 데이터 폴더로 가져온다."""
        warnings: list[str] = []
        store = AssetStore(self.path.parent)
        try:
            with zipfile.ZipFile(source, "r") as archive:
                info = archive.getinfo("library.json")
                if info.file_size > 50 * 1024 * 1024:
                    raise ValueError("library.json이 50MB 제한을 초과했습니다.")
                library = Library.from_dict(json.loads(archive.read(info).decode("utf-8")))
                names = set(archive.namelist())
                imported: dict[str, MediaAsset] = {}
                for asset in _library_assets(library):
                    relative = asset.relative_path
                    if not relative or relative in imported:
                        if relative in imported:
                            _copy_asset_location(imported[relative], asset)
                        continue
                    if relative not in names:
                        warnings.append(f"패키지에 그림이 없습니다: {asset.alt}")
                        continue
                    asset_info = archive.getinfo(relative)
                    if asset_info.file_size > AssetStore.MAX_INPUT_BYTES:
                        warnings.append(f"그림 용량이 커서 제외했습니다: {asset.alt}")
                        continue
                    try:
                        stored = store.import_bytes(
                            archive.read(asset_info),
                            source_page=asset.source_page,
                            bbox=asset.bbox,
                            anchor=asset.anchor,
                            offset=asset.offset,
                            alt=asset.alt,
                        )
                    except AssetError as exc:
                        warnings.append(f"그림을 가져오지 못했습니다({asset.alt}): {exc}")
                        continue
                    imported[relative] = stored
                    _copy_asset_location(stored, asset)
                return library, warnings
        except (OSError, KeyError, UnicodeDecodeError, ValueError, TypeError, OverflowError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ValueError(f"올바른 .koreanlib 파일이 아닙니다: {exc}") from exc

    @staticmethod
    def import_from(source: Path) -> Library:
        return Library.from_dict(json.loads(Path(source).read_text(encoding="utf-8")))


def _library_assets(library: Library):
    for passage in library.passages:
        yield from passage.images
        for question in passage.questions:
            yield from question.images


def _safe_asset_path(root: Path, relative: str) -> Path | None:
    try:
        assets_root = (Path(root) / "assets").resolve()
        candidate = (Path(root) / relative).resolve()
        candidate.relative_to(assets_root)
        return candidate
    except (OSError, ValueError):
        return None


def _copy_asset_location(source: MediaAsset, target: MediaAsset) -> None:
    target.relative_path = source.relative_path
    target.mime_type = source.mime_type
    target.width = source.width
    target.height = source.height
