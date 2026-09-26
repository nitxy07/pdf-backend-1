"""
storage.py
Simple disk-based file storage. Every file (uploaded or generated) gets a
unique ID. Swap this out for S3 / GCS later without changing any endpoint
code — just change the functions below.
"""

import os
import uuid
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "storage", "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "storage", "outputs")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


def new_id() -> str:
    return uuid.uuid4().hex


def save_upload(file_obj, filename: str) -> str:
    """Save an incoming upload, return its file_id."""
    file_id = new_id()
    ext = os.path.splitext(filename)[1] or ".pdf"
    dest = os.path.join(UPLOAD_DIR, f"{file_id}{ext}")
    with open(dest, "wb") as out:
        shutil.copyfileobj(file_obj, out)
    return file_id


def path_for_upload(file_id: str) -> str:
    """Find the stored path for a given upload file_id (any extension)."""
    for f in os.listdir(UPLOAD_DIR):
        if f.startswith(file_id):
            return os.path.join(UPLOAD_DIR, f)
    raise FileNotFoundError(f"No uploaded file found for id {file_id}")


def new_output_path(file_id: str, ext: str = ".pdf") -> str:
    return os.path.join(OUTPUT_DIR, f"{file_id}{ext}")


def path_for_output(file_id: str) -> str:
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith(file_id):
            return os.path.join(OUTPUT_DIR, f)
    raise FileNotFoundError(f"No output file found for id {file_id}")


def resolve_input(file_id: str) -> str:
    """
    Look up a file_id as either an original upload OR a previous output.
    This lets clients chain operations — e.g. protect a file, then feed
    that same file_id straight into /unlock or /watermark — without
    needing to know which folder it physically landed in.
    """
    try:
        return path_for_upload(file_id)
    except FileNotFoundError:
        return path_for_output(file_id)
