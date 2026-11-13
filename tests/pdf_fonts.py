"""Marker for tests that need the DejaVu font files (not committed; see assets/fonts/MANIFEST.md)."""
import pytest

from src.bridge.fonts import fonts_available

needs_fonts = pytest.mark.skipif(not fonts_available(), reason="PDF fonts not installed: run python -m src.bridge.fonts --fetch")
