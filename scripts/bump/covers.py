"""Render local originals using the byrdocs-check v6.5.1 cover policy.

Reference: src/bin/upload-metadata.rs, generate_pdf_covers and reduce_webp_size.
PDFium renders the first page; Pillow supplies JPEG and libwebp encoding.
Encoding follows the same policy, without requiring byte-identical image files.
"""

import io
from contextlib import closing

import pypdfium2 as pdfium


def generate(pdf):
    """Write the original's JPG/WebP covers beside it before storage upload."""
    # PDFium is not thread-safe; the importer calls this serially for each paper.
    with pdfium.PdfDocument(str(pdf)) as document, closing(document[0]) as page:
        width, height = page.get_size()
        with closing(page.render(scale=min(2000 / width, 2000 / height))) as bitmap:
            image = bitmap.to_pil().convert("RGB")
    with image:
        image.save(pdf.with_suffix(".jpg"), format="JPEG", quality=75)
        encoded = io.BytesIO()
        image.save(encoded, format="WEBP", quality=100, method=4)
        if encoded.tell() > 50 * 1024:
            quality = max(int(48 * 1024 / encoded.tell() * 100), 5)
            encoded = io.BytesIO()
            image.save(encoded, format="WEBP", quality=quality, method=4)
        pdf.with_suffix(".webp").write_bytes(encoded.getvalue())
