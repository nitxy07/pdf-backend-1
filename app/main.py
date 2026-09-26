"""
main.py
The API layer. Every client (Android app, website, desktop app) talks to
THIS service over HTTP. None of them need to know how PDF editing works
internally — they just upload a file, call an endpoint, and download the
result.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Then open http://localhost:8000/docs for an interactive Swagger UI where
you can test every endpoint from the browser immediately.
"""

import os
import shutil
from typing import List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from . import storage, pdf_utils

app = FastAPI(
    title="PDF Engine API",
    description="Backend PDF processing service — merge, split, rotate, "
                 "compress, watermark, protect, OCR, and more.",
    version="1.0.0",
)

# Allow requests from any client (Android app, website, desktop app).
# Lock this down to your real domains before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# UPLOAD / DOWNLOAD
# ---------------------------------------------------------------------------

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """Upload a PDF. Returns a file_id used by every other endpoint."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only .pdf files are accepted")
    file_id = storage.save_upload(file.file, file.filename)
    pages = pdf_utils.get_page_count(storage.resolve_input(file_id))
    return {"file_id": file_id, "filename": file.filename, "pages": pages}


@app.get("/download/{file_id}")
async def download_file(file_id: str):
    """Download a processed result by its output file_id."""
    try:
        path = storage.path_for_output(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=os.path.basename(path))


# ---------------------------------------------------------------------------
# MERGE
# ---------------------------------------------------------------------------

@app.post("/merge")
async def merge(file_ids: List[str] = Form(...)):
    """Merge multiple previously-uploaded PDFs (in given order) into one."""
    try:
        input_paths = [storage.resolve_input(fid) for fid in file_ids]
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.merge_pdfs(input_paths, out_path)
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


# ---------------------------------------------------------------------------
# SPLIT
# ---------------------------------------------------------------------------

@app.post("/split")
async def split(file_id: str = Form(...), ranges: str = Form(...)):
    """
    ranges: comma-separated page ranges, e.g. "1-3,5,7-9"
    Returns one PDF containing just those pages, in that order.
    """
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    parsed_ranges = []
    for part in ranges.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            parsed_ranges.append((int(start), int(end)))
        else:
            parsed_ranges.append((int(part), int(part)))

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.split_pdf(input_path, parsed_ranges, out_path)
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


# ---------------------------------------------------------------------------
# ROTATE
# ---------------------------------------------------------------------------

@app.post("/rotate")
async def rotate(file_id: str = Form(...), angle: int = Form(...),
                  pages: str = Form(None)):
    """
    angle: 90, 180, or 270
    pages: optional comma-separated 1-indexed page numbers, e.g. "1,3,5".
           Omit to rotate every page.
    """
    if angle not in (90, 180, 270):
        raise HTTPException(400, "angle must be 90, 180, or 270")
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    page_list = None
    if pages:
        page_list = [int(p.strip()) for p in pages.split(",")]

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.rotate_pages(input_path, angle, out_path, pages=page_list)
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


# ---------------------------------------------------------------------------
# COMPRESS
# ---------------------------------------------------------------------------

@app.post("/compress")
async def compress(file_id: str = Form(...), quality: int = Form(60)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.compress_pdf(input_path, out_path, image_quality=quality)

    before = os.path.getsize(input_path)
    after = os.path.getsize(out_path)
    return {
        "output_file_id": out_id,
        "download_url": f"/download/{out_id}",
        "size_before_kb": round(before / 1024, 1),
        "size_after_kb": round(after / 1024, 1),
    }


# ---------------------------------------------------------------------------
# WATERMARK
# ---------------------------------------------------------------------------

@app.post("/watermark")
async def watermark(file_id: str = Form(...), text: str = Form(...),
                     opacity: float = Form(0.3)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.add_watermark(input_path, out_path, text, opacity=opacity)
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


# ---------------------------------------------------------------------------
# PROTECT / UNLOCK
# ---------------------------------------------------------------------------

@app.post("/protect")
async def protect(file_id: str = Form(...), password: str = Form(...)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    pdf_utils.protect_pdf(input_path, out_path, password)
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


@app.post("/unlock")
async def unlock(file_id: str = Form(...), password: str = Form(...)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    out_id = storage.new_id()
    out_path = storage.new_output_path(out_id)
    try:
        pdf_utils.unlock_pdf(input_path, out_path, password)
    except Exception:
        raise HTTPException(400, "Incorrect password or corrupt file")
    return {"output_file_id": out_id, "download_url": f"/download/{out_id}"}


# ---------------------------------------------------------------------------
# TEXT EXTRACTION + OCR
# ---------------------------------------------------------------------------

@app.post("/extract-text")
async def extract_text(file_id: str = Form(...)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    text = pdf_utils.extract_text(input_path)
    return {"text": text, "char_count": len(text)}


@app.post("/ocr")
async def ocr(file_id: str = Form(...), language: str = Form("eng")):
    """
    Use this for scanned PDFs where /extract-text returns nothing useful.
    Requires the 'tesseract-ocr' and 'poppler-utils' system packages
    (see README).
    """
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    try:
        text = pdf_utils.ocr_pdf(input_path, language=language)
    except Exception as e:
        raise HTTPException(500, f"OCR failed: {e}")
    return {"text": text, "char_count": len(text)}


# ---------------------------------------------------------------------------
# PDF -> IMAGES
# ---------------------------------------------------------------------------

@app.post("/to-images")
async def to_images(file_id: str = Form(...), dpi: int = Form(150)):
    try:
        input_path = storage.resolve_input(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file_id not found")

    out_dir = os.path.join(storage.OUTPUT_DIR, f"images_{file_id}")
    os.makedirs(out_dir, exist_ok=True)
    image_paths = pdf_utils.pdf_to_images(input_path, out_dir, dpi=dpi)

    # Register each image as its own downloadable output
    results = []
    for path in image_paths:
        out_id = storage.new_id()
        new_path = storage.new_output_path(out_id, ext=".png")
        shutil.move(path, new_path)
        results.append({"file_id": out_id, "download_url": f"/download/{out_id}"})
    shutil.rmtree(out_dir, ignore_errors=True)
    return {"pages": results}


@app.get("/")
async def root():
    return JSONResponse({
        "service": "PDF Engine API",
        "docs": "/docs",
        "endpoints": [
            "/upload", "/download/{file_id}", "/merge", "/split", "/rotate",
            "/compress", "/watermark", "/protect", "/unlock",
            "/extract-text", "/ocr", "/to-images",
        ],
    })
