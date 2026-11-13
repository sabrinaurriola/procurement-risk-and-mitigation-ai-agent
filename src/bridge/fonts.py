"""Locates and verifies the DejaVu fonts used by the PDF export (see assets/fonts/MANIFEST.md). Font binaries are not committed.

    python -m src.bridge.fonts            # check the installed fonts
    python -m src.bridge.fonts --fetch    # download the official release, verify SHA-256, install (explicit network action)"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
RELEASE_URL = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
ARCHIVE_SHA256 = "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"
FILES = {
    "DejaVuSans.ttf": "7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954",
    "DejaVuSans-Bold.ttf": "e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724",
    "DejaVuSans-Oblique.ttf": "4af75fa16ee6d3ad43e1ecec41862c24954af26a55c6bb1ebb27bd486a50f5f4",
    "DejaVuSans-BoldOblique.ttf": "eb436dca0c2594b73d8b603b892e374fdfd8d885d25ffb4f18df4c4c0b49e50f",
    "DejaVuSansMono.ttf": "b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582",
}


class FontError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def font_dir() -> Path:
    return Path(os.getenv("PDF_FONT_DIR") or DEFAULT_DIR)


def verified_fonts(directory: Path | None = None) -> dict[str, Path]:
    """Paths of the five font files, each checked against its pinned SHA-256. Raises FontError (fixed messages, no paths)."""
    base = directory or font_dir()
    out: dict[str, Path] = {}
    for name, digest in FILES.items():
        path = base / name
        if not path.is_file():
            raise FontError("PDF_FONT_MISSING", "The PDF fonts are not installed. Run: python -m src.bridge.fonts --fetch (see assets/fonts/MANIFEST.md).")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise FontError("PDF_FONT_INVALID", "A PDF font file does not match its pinned SHA-256. Reinstall it with: python -m src.bridge.fonts --fetch")
        out[name] = path
    return out


def fonts_available() -> bool:
    try:
        verified_fonts()
        return True
    except FontError:
        return False


def fetch(directory: Path | None = None) -> None:
    import httpx
    base = directory or DEFAULT_DIR
    data = httpx.get(RELEASE_URL, follow_redirects=True, timeout=60.0).content
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise FontError("PDF_FONT_INVALID", "The downloaded archive does not match the pinned SHA-256; nothing was installed.")
    base.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        staged = {name: z.read(f"dejavu-fonts-ttf-2.37/ttf/{name}") for name in FILES}
        license_text = z.read("dejavu-fonts-ttf-2.37/LICENSE")
    for name, digest in FILES.items():
        if hashlib.sha256(staged[name]).hexdigest() != digest:
            raise FontError("PDF_FONT_INVALID", "A font inside the archive does not match its pinned SHA-256; nothing was installed.")
    for name, blob in staged.items():
        (base / name).write_bytes(blob)
    (base / "DEJAVU-LICENSE.txt").write_bytes(license_text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download and install the pinned DejaVu release (network)")
    args = ap.parse_args(argv)
    try:
        if args.fetch:
            fetch()
        verified_fonts()
    except FontError as exc:
        print(f"{exc.code}: {exc.message}")
        return 1
    print("PDF fonts: installed and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
