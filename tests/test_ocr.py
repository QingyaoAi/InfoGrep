"""OCR fallback test: an image-only PDF yields no text until OCR is enabled."""

import os
import shutil

import pytest

from infogrep.ingest.extract.registry import extract

pytestmark = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def _scanned_pdf(path):
    """Render text to an image and embed it as a PDF page (no selectable text layer)."""
    import fitz

    src = fitz.open()
    sp = src.new_page()
    sp.insert_text((72, 120), "HELLO OCR WORLD", fontsize=44)
    pix = sp.get_pixmap(dpi=200)

    doc = fitz.open()
    page = doc.new_page(width=pix.width, height=pix.height)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    src.close()


def _ensure_tessdata():
    if os.environ.get("TESSDATA_PREFIX"):
        return
    for cand in ("/opt/homebrew/share/tessdata", "/usr/local/share/tessdata", "/usr/share/tessdata"):
        if os.path.isdir(cand):
            os.environ["TESSDATA_PREFIX"] = cand
            return


def test_ocr_recovers_image_text(tmp_path):
    _ensure_tessdata()
    pdf = tmp_path / "scanned.pdf"
    _scanned_pdf(pdf)

    # Without OCR, the image page has no extractable text.
    assert extract(pdf, ocr=False) == []

    # With OCR, the rendered words are recovered.
    pages = extract(pdf, ocr=True, ocr_min_chars=4)
    text = " ".join(p.text for p in pages).upper()
    assert "HELLO" in text or "OCR" in text or "WORLD" in text
