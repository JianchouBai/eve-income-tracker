from __future__ import annotations

import argparse
import os
import struct
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

ICON_SIZES = (16, 32, 48, 256)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class IconGenerationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


def _default_renderer(**options) -> bytes:
    try:
        from cairosvg import svg2png
    except ImportError:
        raise IconGenerationError("icon_renderer_unavailable") from None
    rendered = svg2png(**options)
    if not isinstance(rendered, bytes):
        raise IconGenerationError("icon_render_failed")
    return rendered


def _validate_png(data: bytes, size: int) -> None:
    if (
        not isinstance(data, bytes)
        or len(data) < 24
        or not data.startswith(PNG_SIGNATURE)
        or data[12:16] != b"IHDR"
        or struct.unpack_from(">II", data, 16) != (size, size)
    ):
        raise IconGenerationError("icon_render_failed")


def _ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = []
    payloads = []
    for size, payload in images:
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        payloads.append(payload)
        offset += len(payload)
    return header + b"".join(entries) + b"".join(payloads)


def generate_windows_icon(
    source: Path,
    output: Path,
    *,
    renderer: Callable[..., bytes] | None = None,
) -> Path:
    try:
        source_bytes = Path(source).read_bytes()
        if b"<svg" not in source_bytes[:1024].lower():
            raise ValueError
    except (OSError, ValueError):
        raise IconGenerationError("icon_source_invalid") from None

    renderer = renderer or _default_renderer
    images = []
    try:
        for size in ICON_SIZES:
            png = renderer(
                bytestring=source_bytes,
                output_width=size,
                output_height=size,
                unsafe=False,
            )
            _validate_png(png, size)
            images.append((size, png))
        icon = _ico_bytes(images)
    except IconGenerationError:
        raise
    except Exception:  # noqa: BLE001 - do not expose renderer internals or source.
        raise IconGenerationError("icon_render_failed") from None

    output = Path(output)
    temporary: Path | None = None
    descriptor = -1
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as icon_file:
            descriptor = -1
            icon_file.write(icon)
            icon_file.flush()
            os.fsync(icon_file.fileno())
        os.replace(temporary, output)
        temporary = None
        return output
    except OSError:
        raise IconGenerationError("icon_write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic Windows ICO")
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("assets/app-icon.svg"),
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("build/generated/app-icon.ico"),
    )
    arguments = parser.parse_args(argv)
    generate_windows_icon(arguments.source, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
