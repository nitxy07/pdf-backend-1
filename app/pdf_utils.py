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


# ---------- TEXT BLOCKS (for real text editing, like Acrobat) ----------
def extract_text_blocks(input_path: str) -> List[dict]:
    """
    Returns every piece of text in the PDF with its exact position, so a
    client can show it as a tappable/editable overlay on top of the page.
    Each block is one "span" (a run of text with uniform font/size/color) -
    this is finer-grained than a whole paragraph, which keeps edits precise.
    """
    doc = fitz.open(input_path)
    blocks = []
    block_id = 0
    for page_index, page in enumerate(doc):
        raw = page.get_text("dict")
        for block in raw["blocks"]:
            if block.get("type") != 0:  # skip images/non-text blocks
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"]
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    color_int = span.get("color", 0)
                    blocks.append({
                        "id": block_id,
                        "page": page_index,
                        "text": text,
                        "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                        "font_size": round(span.get("size", 12), 1),
                        "font_name": span.get("font", "helv"),
                        "color_rgb": [
                            (color_int >> 16) & 255,
                            (color_int >> 8) & 255,
                            color_int & 255,
                        ],
                    })
                    block_id += 1
    doc.close()
    return blocks


def apply_text_edits(input_path: str, edits: List[dict], output_path: str) -> None:
    """
    edits: list of dicts, each shaped like one item from extract_text_blocks
    but with "new_text" instead of/alongside "text":
        {"page": 0, "bbox": [x0,y0,x1,y1], "new_text": "...", "font_size": 12}
    For every edit: the original text's area is redacted (erased) and the
    new text is drawn in its place, on the correct page.
    """
    doc = fitz.open(input_path)

    # Group edits by page so redactions on a page are all applied together
    edits_by_page: dict = {}
    for edit in edits:
        edits_by_page.setdefault(edit["page"], []).append(edit)

    for page_index, page_edits in edits_by_page.items():
        if page_index < 0 or page_index >= len(doc):
            continue
        page = doc[page_index]

        # Step 1: mark the original text areas for removal
        for edit in page_edits:
            rect = fitz.Rect(edit["bbox"])
            page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions()

        # Step 2: draw the new text into the same positions
        for edit in page_edits:
            x0, y0, x1, y1 = edit["bbox"]
            font_size = edit.get("font_size", 12)
            color_rgb = edit.get("color_rgb", [0, 0, 0])
            color = tuple(c / 255 for c in color_rgb)
            baseline_y = y1 - (font_size * 0.2)
            page.insert_text(
                (x0, baseline_y),
                edit["new_text"],
                fontsize=font_size,
                color=color,
            )

    doc.save(output_path)
    doc.close()


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
