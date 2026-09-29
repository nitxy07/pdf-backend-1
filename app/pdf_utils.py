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

def _map_font(font_name: str, flags: int) -> str:
    """Map a PDF font name + flags to a PyMuPDF base-14 fontname.

    Preserves family (helvetica/arial -> helv, times/serif -> tiro,
    courier/mono -> cour) and bold/italic so the export keeps the
    original look instead of falling back to plain helvetica.
    """
    fn = (font_name or "").lower()
    try:
        f = int(flags or 0)
    except Exception:
        f = 0
    bold = bool(f & 16) or ("bold" in fn or "black" in fn or "heavy" in fn or "demi" in fn)
    italic = bool(f & 2) or ("italic" in fn or "oblique" in fn)

    if "courier" in fn or "mono" in fn or "consol" in fn or fn.startswith("cour"):
        if bold and italic:
            return "cobi"
        if bold:
            return "cobo"
        if italic:
            return "coit"
        return "cour"
    if ("times" in fn or "serif" in fn or "georg" in fn or "garamond" in fn
            or "roman" in fn or "tiro" in fn or "tibo" in fn):
        if bold and italic:
            return "tibi"
        if bold:
            return "tibo"
        if italic:
            return "tiit"
        return "tiro"
    # default: helvetica / arial / sans
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


def _detect_block_align(line_bboxes, page_width, block_rect=None):
    """Guess PyMuPDF textbox align for a paragraph block.

    Returns 0=left, 1=center, 2=right, 3=justify.
    Keeps an edited paragraph looking like the original instead of
    everything collapsing to left-aligned.

    NOTE: callers must NEVER pass align=3 straight into a single
    insert_textbox() for the whole paragraph, because PyMuPDF stretches
    the LAST (often short) line across the full width with huge word
    gaps (the bug in the user screenshot). The writer below uses 3 only
    for full body lines and 0/1 for the last line.
    """
    try:
        pw = float(page_width)
    except Exception:
        pw = 595.0
    if not line_bboxes:
        return 0
    if len(line_bboxes) == 1:
        try:
            lx0, _, lx1, _ = line_bboxes[0]
            left_margin = float(lx0)
            right_margin = pw - float(lx1)
            line_w = float(lx1) - float(lx0)
        except Exception:
            return 0
        if line_w < pw * 0.8 and abs(left_margin - right_margin) < 12:
            return 1
        if left_margin > pw * 0.25 and right_margin < 25:
            return 2
        return 0
    try:
        lefts = [float(b[0]) for b in line_bboxes]
        rights = [float(b[2]) for b in line_bboxes]
    except Exception:
        return 0
    rights_body = rights[:-1] if len(rights) > 2 else rights
    left_spread = max(lefts) - min(lefts)
    right_spread = max(rights_body) - min(rights_body)
    if left_spread < 5 and right_spread < 9:
        return 3  # justified: both edges straight
    if left_spread < 5:
        return 0
    if right_spread < 7:
        return 2
    try:
        centers = [(float(b[0]) + float(b[2])) / 2 for b in line_bboxes]
        if max(centers) - min(centers) < 7:
            return 1
    except Exception:
        pass
    return 0


def _text_length(text: str, fontname: str, fontsize: float) -> float:
    try:
        return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    except Exception:
        return len(text) * fontsize * 0.5


def _reflow_words(words, fontname, fontsize, max_width):
    """Greedy word-wrap into lines that fit max_width. Returns [line_str]."""
    lines = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip() if cur else w
        try:
            need = _text_length(cand, fontname, fontsize)
        except Exception:
            need = len(cand) * fontsize * 0.5
        if not cur or need <= max_width:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            # single very long word wider than column: put it alone, it will
            # be shrunk by the font-size loop rather than overflowing.
    if cur:
        lines.append(cur)
    return lines if lines else [" "]


def extract_text_blocks(input_path: str) -> List[dict]:
    """
    Returns every piece of text in the PDF with its exact position, so a
    client can show it as a tappable/editable overlay on top of the page.
    Each block is one "span" (a run of text with uniform font/size/color) -
    this is finer-grained than a whole paragraph, which keeps edits precise.

    Extra fields (block_no / line_no / line_bbox / block_bbox / flags /
    origin / page_width / page_height) let apply_text_edits() rewrite the
    whole paragraph with the original width + alignment preserved, instead
    of drawing one endless single line that shoots past the right margin.
    """
    doc = fitz.open(input_path)
    blocks = []
    block_id = 0
    for page_index, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height
        raw = page.get_text("dict")
        text_blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
        for bno, block in enumerate(text_blocks):
            bx0, by0, bx1, by1 = block.get("bbox", [0, 0, 0, 0])
            lines = block.get("lines", [])
            for lno, line in enumerate(lines):
                lx0, ly0, lx1, ly1 = line.get("bbox", [0, 0, 0, 0])
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = span.get("bbox", [lx0, ly0, lx1, ly1])
                    color_int = span.get("color", 0)
                    try:
                        c = int(color_int)
                    except Exception:
                        c = 0
                    origin = span.get("origin", [x0, y1])
                    blocks.append({
                        "id": block_id,
                        "page": page_index,
                        "text": text,
                        "bbox": [round(float(x0), 2), round(float(y0), 2),
                                 round(float(x1), 2), round(float(y1), 2)],
                        "font_size": round(float(span.get("size", 12)), 1),
                        "font_name": span.get("font", "helv"),
                        "color_rgb": [
                            (c >> 16) & 255,
                            (c >> 8) & 255,
                            c & 255,
                        ],
                        "block_no": bno,
                        "line_no": lno,
                        "line_bbox": [round(float(lx0), 2), round(float(ly0), 2),
                                      round(float(lx1), 2), round(float(ly1), 2)],
                        "block_bbox": [round(float(bx0), 2), round(float(by0), 2),
                                       round(float(bx1), 2), round(float(by1), 2)],
                        "flags": int(span.get("flags", 0)),
                        "origin": [round(float(origin[0]), 2), round(float(origin[1]), 2)],
                        "page_width": round(float(pw), 2),
                        "page_height": round(float(ph), 2),
                    })
                    block_id += 1
    doc.close()
    return blocks


def _rects_overlap(a: List[float], b: List[float]) -> float:
    """Intersection-over-min-area of two [x0,y0,x1,y1] rects (0..1)."""
    try:
        ix0 = max(a[0], b[0])
        iy0 = max(a[1], b[1])
        ix1 = min(a[2], b[2])
        iy1 = min(a[3], b[3])
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0
        inter = (ix1 - ix0) * (iy1 - iy0)
        area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
        area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
        return inter / min(area_a, area_b)
    except Exception:
        return 0.0


def _insert_textbox_safe(page, rect, text, fontsize, fontname, color, align):
    """insert_textbox with fontname fallback. Returns rect-code or -1."""
    if not text or not text.strip():
        text = " "
    try:
        return page.insert_textbox(rect, text, fontsize=fontsize,
                                   fontname=fontname, color=color, align=align)
    except Exception:
        try:
            return page.insert_textbox(rect, text, fontsize=fontsize,
                                       color=color, align=align)
        except Exception:
            return -1


def apply_text_edits(input_path: str, edits: List[dict], output_path: str) -> None:
    """
    edits: list of dicts, each shaped like one item from extract_text_blocks
    but with "new_text" instead of/alongside "text":
        {"page":0,"bbox":[x0,y0,x1,y1],"new_text":"Hello","font_size":14,
         "color_rgb":[0,0,0],"id":12 (optional, preferred)}

    Page-property-preserving behaviour:
      * x0/x1 (left + right margins) are NEVER expanded. Only height may
        grow downward into free space, capped before the next paragraph.
      * Justified body keeps its look WITHOUT the old bug: full lines are
        written justified, the last (usually short) line is written
        left-aligned (or centered if the block was centered). This is what
        stops "ukygyuy 8yguy 8g ..." from stretching across the page.
      * Font family / bold / italic / size / color carried from source.
      * If every edited line still fits its original line width, lines are
        redrawn 1:1 in their original rects (pixel-faithful, no reflow).
        Reflow only kicks in on real overflow.
    """
    doc = fitz.open(input_path)

    edits_by_id = {}
    bbox_edits = []
    for e in (edits or []):
        try:
            page_no = int(e.get("page", 0))
        except Exception:
            continue
        new_text = e.get("new_text", e.get("text", ""))
        if new_text is None:
            new_text = ""
        e["_page"] = page_no
        e["_new_text"] = str(new_text)
        if "id" in e and e["id"] is not None:
            try:
                edits_by_id[(page_no, int(e["id"]))] = e
            except Exception:
                bbox_edits.append(e)
        else:
            bbox_edits.append(e)

    for page_index, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height
        raw = page.get_text("dict")
        text_blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]

        # global span ids must match extract_text_blocks() ordering
        gid = 0
        for pi in range(page_index):
            try:
                praw = doc[pi].get_text("dict")
                for bb in praw.get("blocks", []):
                    if bb.get("type") != 0:
                        continue
                    for ll in bb.get("lines", []):
                        for ss in ll.get("spans", []):
                            if (ss.get("text", "") or "").strip():
                                gid += 1
            except Exception:
                pass

        page_spans = []  # (gid, bno, lno, span, line, block)
        for bno, block in enumerate(text_blocks):
            for lno, line in enumerate(block.get("lines", [])):
                for span in line.get("spans", []):
                    if not (span.get("text", "") or "").strip():
                        continue
                    page_spans.append((gid, bno, lno, span, line, block))
                    gid += 1

        def span_matches(gid_, span_):
            if (page_index, gid_) in edits_by_id:
                return edits_by_id[(page_index, gid_)]
            sb = span_.get("bbox")
            if not sb:
                return None
            for be in bbox_edits:
                if be["_page"] != page_index:
                    continue
                ebb = be.get("bbox")
                if not ebb or len(ebb) != 4:
                    continue
                try:
                    if _rects_overlap([float(v) for v in sb],
                                      [float(v) for v in ebb]) > 0.4:
                        return be
                except Exception:
                    continue
            return None

        affected = {}
        for gid_, bno, lno, span, line, block in page_spans:
            m = span_matches(gid_, span)
            if m is not None:
                affected.setdefault(bno, {"block": block, "hits": {}})
                affected[bno]["hits"][gid_] = m

        if not affected:
            continue

        sorted_bnos = sorted(affected.keys(),
                             key=lambda b: affected[b]["block"].get("bbox", [0, 0, 0, 0])[1])

        # ---- Phase 1: redact whole affected paragraphs ----
        block_rects = {}
        for bno in sorted_bnos:
            block = affected[bno]["block"]
            bx0, by0, bx1, by1 = block.get("bbox", [0, 0, 0, 0])
            rect = fitz.Rect(float(bx0), float(by0), float(bx1), float(by1))
            try:
                page.add_redact_annot(
                    fitz.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1),
                    fill=(1, 1, 1))
            except Exception:
                pass
            block_rects[bno] = rect
        try:
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            except Exception:
                page.apply_redactions()
        except Exception:
            pass

        block_tops = sorted([(b.get("bbox", [0, 0, 0, 0])[1], bi)
                             for bi, b in enumerate(text_blocks)])

        # ---- Phase 2: rewrite each paragraph inside ORIGINAL x0..x1 ----
        for bno in sorted_bnos:
            info = affected[bno]
            block = info["block"]
            hits = info["hits"]
            rect = block_rects[bno]
            lines = block.get("lines", [])
            if not lines:
                continue

            # new text per original line (gap-aware space join)
            new_line_texts = []
            line_origins = []
            for line in lines:
                spans_sorted = sorted(line.get("spans", []),
                                      key=lambda s: s.get("bbox", [0, 0, 0, 0])[0])
                parts = []
                prev_x1 = None
                for sp in spans_sorted:
                    if not (sp.get("text", "") or "").strip():
                        continue
                    sgid = None
                    for g2, b2, _l2, s2, _ln2, _b2 in page_spans:
                        if b2 == bno and s2 is sp:
                            sgid = g2
                            break
                    orig = sp.get("text", "")
                    repl = orig
                    if sgid is not None and sgid in hits:
                        repl = hits[sgid]["_new_text"]
                    else:
                        m2 = span_matches(sgid if sgid is not None else -1, sp)
                        if m2 is not None:
                            repl = m2["_new_text"]
                    if prev_x1 is not None and parts:
                        try:
                            gap = float(sp.get("bbox", [0, 0, 0, 0])[0]) - float(prev_x1)
                            fsz = float(sp.get("size", 12))
                            if (gap > fsz * 0.18
                                    and not parts[-1].endswith((" ", "\n", "\t"))
                                    and not str(repl).startswith(" ")):
                                parts.append(" ")
                        except Exception:
                            pass
                    parts.append(str(repl))
                    try:
                        prev_x1 = float(sp.get("bbox", [0, 0, 0, 0])[2])
                    except Exception:
                        prev_x1 = None
                new_line_texts.append("".join(parts))
                try:
                    sp0 = spans_sorted[0] if spans_sorted else {}
                    o = sp0.get("origin", None)
                    line_origins.append(o)
                except Exception:
                    line_origins.append(None)

            # dominant style for the paragraph
            all_spans = []
            for line in lines:
                all_spans.extend(line.get("spans", []))
            sizes = sorted([float(s.get("size", 12)) for s in all_spans if (s.get("text", "") or "").strip()])
            dom_size = sizes[len(sizes) // 2] if sizes else 12.0
            ov = []
            for g2, b2, _l2, _s2, _ln2, _b2 in page_spans:
                if b2 == bno and g2 in hits:
                    try:
                        ov.append(float(hits[g2].get("font_size", dom_size)))
                    except Exception:
                        pass
            if ov:
                dom_size = sorted(ov)[len(ov) // 2]

            font_votes = {}
            color_votes = {}
            for s in all_spans:
                if not (s.get("text", "") or "").strip():
                    continue
                key = (s.get("font", "helv"), int(s.get("flags", 0)))
                font_votes[key] = font_votes.get(key, 0) + 1
                try:
                    ci = int(s.get("color", 0))
                except Exception:
                    ci = 0
                color_votes[ci] = color_votes.get(ci, 0) + 1
            dom_font, dom_flags = max(font_votes.items(), key=lambda kv: kv[1])[0] if font_votes else ("helv", 0)
            dom_color_int = max(color_votes.items(), key=lambda kv: kv[1])[0] if color_votes else 0
            for g2, b2, _l2, _s2, _ln2, _b2 in page_spans:
                if b2 == bno and g2 in hits:
                    crgb = hits[g2].get("color_rgb")
                    if isinstance(crgb, (list, tuple)) and len(crgb) == 3:
                        try:
                            dom_color_int = ((int(crgb[0]) & 255) << 16) | ((int(crgb[1]) & 255) << 8) | (int(crgb[2]) & 255)
                        except Exception:
                            pass
                        break
            fontname = _map_font(dom_font, dom_flags)
            color = ((dom_color_int >> 16 & 255) / 255,
                     (dom_color_int >> 8 & 255) / 255,
                     (dom_color_int & 255) / 255)

            try:
                line_bboxes_f = [list(map(float, ln.get("bbox", [0, 0, 0, 0]))) for ln in lines]
            except Exception:
                line_bboxes_f = []
            orig_align = _detect_block_align(line_bboxes_f, pw)

            try:
                cur_top = float(block.get("bbox", [0, 0, 0, 0])[1])
                following = [t for t, _ in block_tops if t > cur_top + 1]
                cap_y1 = min(ph - 20, min(following) - 2) if following else ph - 20
            except Exception:
                cap_y1 = ph - 20

            fontsize = max(6.0, min(float(dom_size) if dom_size else 12.0, 72.0))
            block_w = max(20.0, float(rect.x1 - rect.x0))

            # Fast path 1: single line that still fits -> exact origin write.
            if len(lines) == 1:
                single = new_line_texts[0] if new_line_texts else " "
                if _text_length(single, fontname, fontsize) <= block_w + 2 and line_origins and line_origins[0]:
                    try:
                        page.insert_text(fitz.Point(float(line_origins[0][0]), float(line_origins[0][1])),
                                         single, fontsize=fontsize,
                                         fontname=fontname, color=color)
                        continue
                    except Exception:
                        try:
                            page.insert_text(fitz.Point(float(line_origins[0][0]), float(line_origins[0][1])),
                                             single, fontsize=fontsize, color=color)
                            continue
                        except Exception:
                            pass

            # Unified rewrite: give PyMuPDF the WHOLE paragraph text in one
            # insert_textbox() call and let it wrap AND justify together.
            # This is the key fix: calling insert_textbox once per
            # already-split line (the old approach) makes PyMuPDF think
            # each line is "already fitting" and it never stretches word
            # spacing - align=3 is silently ignored on a single fitting
            # line. Justify only works when PyMuPDF does its own wrapping
            # across multiple lines inside one call.
            full_text = " ".join(t for t in new_line_texts if t.strip())
            if not full_text.strip():
                continue

            orig_bottom = float(rect.y1)
            attempt = fontsize
            done = False
            for _try in range(10):
                if _try > 0:
                    # widen the erased area before retrying at a new size
                    try:
                        page.add_redact_annot(
                            fitz.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, cap_y1 + 1),
                            fill=(1, 1, 1))
                        try:
                            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                        except Exception:
                            page.apply_redactions()
                    except Exception:
                        pass

                # First try within the paragraph's original height; only
                # grow downward into free space if that's not enough.
                target_bottom = orig_bottom if _try == 0 else cap_y1
                write_rect = fitz.Rect(float(rect.x0), float(rect.y0),
                                       float(rect.x1), float(target_bottom))
                rc = _insert_textbox_safe(page, write_rect, full_text,
                                          attempt, fontname, color, orig_align)
                if rc is not None and rc >= 0:
                    done = True
                    break
                if attempt > 6.5:
                    attempt = round(attempt - 0.5, 1)
            _ = done

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


# ---------- IMAGES & STAMPS (move / drag) ----------

def _rect_iou(a, b) -> float:
    """Intersection-over-union of two rects (any 4-number sequences)."""
    try:
        ra, rb = fitz.Rect(a), fitz.Rect(b)
        inter = ra & rb
        if inter.is_empty:
            return 0.0
        ia = inter.width * inter.height
        ua = ra.width * ra.height + rb.width * rb.height - ia
        return ia / ua if ua > 0 else 0.0
    except Exception:
        return 0.0


def _stamp_annots(page):
    """All stamp annotations on a page (empty list if none)."""
    out = []
    try:
        for annot in (page.annots() or []):
            if annot.type[0] == fitz.PDF_ANNOT_STAMP:
                out.append(annot)
    except Exception:
        pass
    return out


def extract_page_images(input_path: str) -> List[dict]:
    """
    Lists every movable non-text object: embedded images and stamp
    annotations, with the exact page position of each.

    kind "image": an image drawn on the page (logo, photo, signature scan..)
    kind "stamp": a stamp annotation (Approved, Draft, image stamps...)
    full_page:    True when the object covers ~the whole page (a scanned
                  page). Clients should not offer to drag those.
    """
    doc = fitz.open(input_path)
    items: List[dict] = []
    next_id = 0
    for page_index, page in enumerate(doc):
        page_area = max(page.rect.width * page.rect.height, 1.0)

        stamps = _stamp_annots(page)
        stamp_rects = []
        for annot in stamps:
            r = fitz.Rect(annot.rect)
            if r.width < 2 or r.height < 2:
                continue
            stamp_rects.append(r)
            items.append({
                "id": next_id,
                "page": page_index,
                "kind": "stamp",
                "bbox": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                "xref": int(annot.xref),
                "full_page": (r.width * r.height) > 0.9 * page_area,
            })
            next_id += 1

        for info in page.get_image_info(xrefs=True):
            r = fitz.Rect(info["bbox"])
            if r.width < 4 or r.height < 4:
                continue
            # an image that is just the picture inside a stamp -> skip, the
            # stamp itself is the draggable object
            if any(_rect_iou(r, sr) > 0.8 for sr in stamp_rects):
                continue
            items.append({
                "id": next_id,
                "page": page_index,
                "kind": "image",
                "bbox": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                "xref": int(info.get("xref", 0) or 0),
                "full_page": (r.width * r.height) > 0.9 * page_area,
            })
            next_id += 1
    doc.close()
    return items


def apply_image_edits(input_path: str, edits: List[dict], output_path: str) -> None:
    """
    Moves (and/or resizes) images and stamps.

    edits: [{"page": 0, "kind": "image"|"stamp",
             "old_bbox": [x0,y0,x1,y1],   # where it was (from extract_page_images)
             "new_bbox": [x0,y0,x1,y1]}]  # where it should go

    Objects are matched by POSITION (old_bbox), not by xref/id, because ids
    change whenever a PDF is re-saved (e.g. after text edits ran first).

    * stamp  -> the annotation's rectangle is simply changed.
    * image  -> the image is removed from its old place (only that picture,
                text and everything else stay untouched) and re-drawn at the
                new rectangle with its original pixels + transparency.
                If the same picture is used several times, only the one you
                moved is moved; the others stay where they were.
    """
    doc = fitz.open(input_path)

    stamp_edits, image_edits = [], []
    for e in (edits or []):
        try:
            page_no = int(e.get("page", 0))
            old_bbox = [float(v) for v in e["old_bbox"]]
            new_bbox = [float(v) for v in e["new_bbox"]]
            if len(old_bbox) != 4 or len(new_bbox) != 4:
                continue
            if page_no < 0 or page_no >= len(doc):
                continue
        except Exception:
            continue
        item = {"page": page_no, "old": old_bbox, "new": new_bbox}
        (stamp_edits if e.get("kind") == "stamp" else image_edits).append(item)

    # ---- stamps: just change the rectangle ----
    for e in stamp_edits:
        page = doc[e["page"]]
        best, best_iou = None, 0.4
        for annot in _stamp_annots(page):
            iou = _rect_iou(annot.rect, e["old"])
            if iou > best_iou:
                best, best_iou = annot, iou
        if best is not None:
            best.set_rect(fitz.Rect(e["new"]))
            best.update()

    # ---- images: match each move to a real image on the page ----
    by_xref: dict = {}
    for e in image_edits:
        page = doc[e["page"]]
        best, best_iou = None, 0.4
        for info in page.get_image_info(xrefs=True):
            if not info.get("xref"):
                continue
            iou = _rect_iou(info["bbox"], e["old"])
            if iou > best_iou:
                best, best_iou = info, iou
        if best is None:
            continue
        by_xref.setdefault(int(best["xref"]), []).append(
            (e["page"], fitz.Rect(best["bbox"]), fitz.Rect(e["new"])))

    for xref, moves in by_xref.items():
        # every place this picture is drawn anywhere in the document
        placements = []
        for pi, pg in enumerate(doc):
            try:
                for r in pg.get_image_rects(xref):
                    placements.append((pi, fitz.Rect(r)))
            except Exception:
                pass
        if not placements:
            continue

        # grab the original pixels (+ soft mask for transparency) first
        ext = doc.extract_image(xref)
        if not ext or not ext.get("image"):
            continue
        kwargs = {"stream": ext["image"]}
        smask = ext.get("smask", 0)
        if smask:
            try:
                mask_ext = doc.extract_image(smask)
                if mask_ext and mask_ext.get("image"):
                    kwargs["mask"] = mask_ext["image"]
            except Exception:
                pass

        # remove the old picture (replaced by a 1x1 transparent pixel)
        doc[moves[0][0]].delete_image(xref)

        new_xref = 0
        for pi, r in placements:
            target = r
            for mpi, orect, nrect in moves:
                if mpi == pi and _rect_iou(orect, r) > 0.9:
                    target = nrect
                    break
            pg = doc[pi]
            if new_xref:
                pg.insert_image(target, xref=new_xref,
                                keep_proportion=False, overlay=True)
            else:
                new_xref = pg.insert_image(target, keep_proportion=False,
                                           overlay=True, **kwargs)

    doc.save(output_path, garbage=4, deflate=True)
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