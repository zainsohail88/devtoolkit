"""Zip downloads of converted files."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from fastapi.responses import StreamingResponse

from messages import NOTES_INTRO, NOTES_NONE, NOTES_TITLE
from settings import NOTES_FILE_NAME, SOURCE_LABELS


def zip_download(files: list[dict[str, Any]], name: str) -> StreamingResponse:
    """A response that downloads `files` (each with a path and content) as a zip named `name`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.writestr(file["path"], file["content"])
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def notes_file(notes: list[str], source: str) -> dict[str, str]:
    """The conversion notes as a file for the zip, so they travel with the code."""
    return {"path": NOTES_FILE_NAME, "content": notes_markdown(notes, source)}


def notes_markdown(notes: list[str], source: str) -> str:
    """The conversion notes as Markdown."""
    lines = [NOTES_TITLE, "", NOTES_INTRO.format(source=SOURCE_LABELS.get(source, source)), ""]
    lines += [f"- {note}" for note in notes] or [NOTES_NONE]
    return "\n".join(lines) + "\n"
