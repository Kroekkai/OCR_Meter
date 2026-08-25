"""
Files are stored on disk using the original uploaded filename directly —
e.g. "e101_20260824_140000_01.jpg" — and the OCR-annotated result image
as "{stem}_ocr{suffix}" alongside it, e.g. "e101_20260824_140000_01_ocr.jpg".

No image_id prefix: meter_id + HHMMSS + seq is treated as guaranteed
unique by the device naming convention (confirmed choice). If two
uploads ever do produce the same filename, the second upload silently
overwrites the first file on disk — the DB rows themselves stay
separate either way, only the on-disk image would be lost.
"""
import os
from pathlib import Path

from app.config import get_settings


def _dir() -> Path:
    settings = get_settings()
    path = Path(settings.upload_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stem_and_suffix(original_filename: str | None, image_id: int) -> tuple[str, str]:
    if not original_filename:
        # Edge case only — a row can't reach here without a filename via
        # POST /images/upload (it requires one). This only fires for rows
        # inserted straight into the DB, bypassing the API. Falls back to
        # the image id so we still get a deterministic path instead of
        # crashing on Path(None).
        return str(image_id), ".jpg"
    p = Path(original_filename)
    return p.stem, (p.suffix or ".jpg")


def original_path(image_id: int, original_filename: str | None) -> Path:
    stem, suffix = _stem_and_suffix(original_filename, image_id)
    return _dir() / f"{stem}{suffix}"


def ocr_result_path(image_id: int, original_filename: str | None) -> Path:
    stem, suffix = _stem_and_suffix(original_filename, image_id)
    return _dir() / f"{stem}_ocr{suffix}"


async def save_upload(image_id: int, original_filename: str, data: bytes, *, is_ocr_result: bool = False) -> Path:
    path = (
        ocr_result_path(image_id, original_filename)
        if is_ocr_result
        else original_path(image_id, original_filename)
    )
    path.write_bytes(data)
    return path


def delete_files(image_id: int, original_filename: str | None) -> None:
    for path in (original_path(image_id, original_filename), ocr_result_path(image_id, original_filename)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
