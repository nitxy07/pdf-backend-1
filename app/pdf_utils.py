"""
pdf_utils.py
All the actual PDF logic lives here, separate from the API layer (main.py).
This makes it easy to test, reuse in a CLI tool, or swap libraries later.

Libraries used:
- pypdf      -> merge, split, rotate, encrypt/decrypt, metadata
- PyMuPDF    -> render pages to images, watermark, compress, text extraction
- pytesseract + pdf2image -> OCR for scanned PDFs
"""

import os
import io
from typing import List, Tuple

from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from pdf2image import convert_from_path


# ---------- MERGE ----------
def merge_pdfs(input_paths: List[str], output_path: str) -> None:
    writer = PdfWriter()
    for path in input_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)


# ---------- SPLIT ----------
def split_pdf(input_path: str, page_ranges: List[Tuple[int, int]], output_path: str) -> None:
    """
    page_ranges: list of (start, end) tuples, 1-indexed, inclusive.
    Produces a single PDF containing just those pages, in order.
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()
    for start, end in page_ranges:
        for i in range(start - 1, end):
            if 0 <= i < len(reader.pages):
                writer.add_page(reader.pages[i])
    with open(output_path, "wb") as f:
        writer.write(f)


def get_page_count(input_path: str) -> int:
    return len(PdfReader(input_path).pages)


# ---------- ROTATE ----------
def rotate_pages(input_path: str, angle: int, output_path: str, pages: List[int] = None) -> None:
    """
    angle: 90, 180, or 270 (clockwise)
    pages: 1-indexed page numbers to rotate; None = rotate all pages
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if pages is None or (i + 1) in pages:
            page.rotate(angle)
        writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)


# ---------- COMPRESS ----------
def compress_pdf(input_path: str, output_path: str, image_quality: int = 60) -> None:
    """
    Recompresses embedded images to shrink file size.
    """
    doc = fitz.open(input_path)
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
                img_bytes = base["image"]
                pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=image_quality)
                doc.update_stream(xref, buf.getvalue())
            except Exception:
                continue  # skip images that can't be recompressed safely
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


# ---------- WATERMARK ----------
def add_watermark(input_path: str, output_path: str, text: str,
                   opacity: float = 0.3, font_size: int = 40) -> None:
    """
    Stamps a diagonal, semi-transparent watermark across every page.
    Uses TextWriter + a rotation matrix since insert_text() only supports
    axis-aligned (0/90/180/270) rotation, not arbitrary angles like 45.
    """
    doc = fitz.open(input_path)
    for page in doc:
        rect = page.rect
        center = fitz.Point(rect.width / 2, rect.height / 2)

        tw = fitz.TextWriter(rect, color=(0.5, 0.5, 0.5), opacity=opacity)
        # Roughly center the text on the page before rotating around it
        text_len_estimate = font_size * 0.5 * len(text)
        start = fitz.Point(center.x - text_len_estimate / 2, center.y)
        tw.append(start, text, fontsize=font_size)

        rotation_matrix = fitz.Matrix(1, 1).prerotate(45)
        tw.write_text(page, morph=(center, rotation_matrix))
    doc.save(output_path)
    doc.close()


# ---------- PASSWORD PROTECT / UNLOCK ----------
def protect_pdf(input_path: str, output_path: str, password: str) -> None:
    reader = PdfReader(input_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with open(output_path, "wb") as f:
        writer.write(f)


def unlock_pdf(input_path: str, output_path: str, password: str) -> None:
    reader = PdfReader(input_path)
    if reader.is_encrypted:
        reader.decrypt(password)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)


# ---------- TEXT EXTRACTION ----------
def extract_text(input_path: str) -> str:
    doc = fitz.open(input_path)
    text = []
    for page in doc:
        text.append(page.get_text())
    doc.close()
    return "\n".join(text)


# ---------- PDF -> IMAGES ----------
def pdf_to_images(input_path: str, output_dir: str, dpi: int = 150) -> List[str]:
    doc = fitz.open(input_path)
    paths = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat)
        out_path = os.path.join(output_dir, f"page_{i+1}.png")
        pix.save(out_path)
        paths.append(out_path)
    doc.close()
    return paths


# ---------- OCR (for scanned PDFs with no selectable text) ----------
def ocr_pdf(input_path: str, language: str = "eng") -> str:
    """
    Converts each page to an image, then runs Tesseract OCR on it.
    Use this when extract_text() returns empty/garbage (i.e. the PDF is
    just scanned images, not real text).
    """
    images = convert_from_path(input_path)
    text_parts = []
    for img in images:
        text_parts.append(pytesseract.image_to_string(img, lang=language))
    return "\n".join(text_parts)
