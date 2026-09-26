# PDF Engine API

A self-contained backend that does the actual PDF work — merge, split,
rotate, compress, watermark, password protect/unlock, text extraction, OCR,
and PDF→image conversion. Upload a file once, get back a `file_id`, then
call any operation on it. **One backend, three clients**: your Android app,
your website, and any desktop app all just call this over HTTP — none of
them need PDF logic of their own.

```
[Android App]  ─┐
[Website]       ├──►  This API  ──►  pypdf / PyMuPDF / Tesseract OCR
[Desktop App]  ─┘
```

## 1. Run it locally

```bash
pip install -r requirements.txt

# System packages needed for OCR / PDF→image (Ubuntu/Debian):
sudo apt-get install -y poppler-utils tesseract-ocr

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/docs** — this gives you a full interactive
Swagger UI where you can upload a PDF and try every endpoint from the
browser before writing any client code.

## 2. Run it with Docker (recommended for a real server)

```bash
docker build -t pdf-engine .
docker run -p 8000:8000 pdf-engine
```

The Dockerfile already installs `poppler-utils` and `tesseract-ocr`, so
OCR works out of the box — no manual server setup needed.

## 3. Deploying to an actual server

Any host that runs Docker works: a $5 DigitalOcean droplet, AWS EC2,
Railway, Render, Fly.io, etc.

```bash
# On your server:
git clone <your-repo>
cd pdf-backend
docker build -t pdf-engine .
docker run -d -p 8000:8000 --restart unless-stopped pdf-engine
```

Then point your Android app / website / desktop app at
`https://your-server-domain.com` instead of `localhost`.

**Before going to production:**
- Put this behind HTTPS (e.g. nginx + Let's Encrypt, or a platform that
  provides it automatically like Render/Railway).
- Lock down CORS in `main.py` (`allow_origins=["*"]` → your actual domains).
- Replace disk storage in `storage.py` with S3/GCS if you expect real
  traffic or need multiple server instances.
- Add auth (an API key header, or JWT) — right now anyone who can reach
  the server can call any endpoint.
- Add a cron job or scheduled task to delete old files from
  `app/storage/uploads` and `app/storage/outputs` so disk doesn't fill up.

## 4. API reference

All endpoints are POST unless noted. All file inputs/outputs are
referenced by `file_id`, not raw bytes — upload once, then chain
operations by passing the same `file_id` (or a previous output's
`file_id`) into the next call.

| Endpoint | Purpose | Key params |
|---|---|---|
| `POST /upload` | Upload a PDF | `file` (multipart) |
| `GET /download/{file_id}` | Download any result | — |
| `POST /merge` | Combine multiple PDFs | `file_ids` (repeat field) |
| `POST /split` | Extract a page range | `file_id`, `ranges` e.g. `"1-3,5"` |
| `POST /rotate` | Rotate pages | `file_id`, `angle` (90/180/270), `pages` optional |
| `POST /compress` | Shrink file size | `file_id`, `quality` (1-100) |
| `POST /watermark` | Diagonal text watermark | `file_id`, `text`, `opacity` |
| `POST /protect` | Add a password | `file_id`, `password` |
| `POST /unlock` | Remove a password | `file_id`, `password` |
| `POST /extract-text` | Get selectable text | `file_id` |
| `POST /ocr` | OCR a scanned PDF | `file_id`, `language` (default `eng`) |
| `POST /to-images` | Render pages as PNGs | `file_id`, `dpi` |

**Example: merge two PDFs with curl**
```bash
ID1=$(curl -s -F "file=@a.pdf" http://localhost:8000/upload | python3 -c "import sys,json;print(json.load(sys.stdin)['file_id'])")
ID2=$(curl -s -F "file=@b.pdf" http://localhost:8000/upload | python3 -c "import sys,json;print(json.load(sys.stdin)['file_id'])")
curl -F "file_ids=$ID1" -F "file_ids=$ID2" http://localhost:8000/merge
# -> {"output_file_id": "...", "download_url": "/download/..."}
curl -o merged.pdf http://localhost:8000/download/<output_file_id>
```

## 5. Calling it from each client

### Android (Kotlin + Retrofit)
```kotlin
interface PdfApi {
    @Multipart
    @POST("upload")
    suspend fun upload(@Part file: MultipartBody.Part): UploadResponse

    @FormUrlEncoded
    @POST("watermark")
    suspend fun watermark(
        @Field("file_id") fileId: String,
        @Field("text") text: String
    ): OperationResponse
}

val retrofit = Retrofit.Builder()
    .baseUrl("https://your-server-domain.com/")
    .addConverterFactory(GsonConverterFactory.create())
    .build()
```

### Website (JavaScript fetch)
```javascript
const formData = new FormData();
formData.append("file", fileInputElement.files[0]);

const uploadRes = await fetch("https://your-server-domain.com/upload", {
  method: "POST",
  body: formData,
});
const { file_id } = await uploadRes.json();

const wmForm = new FormData();
wmForm.append("file_id", file_id);
wmForm.append("text", "CONFIDENTIAL");

const wmRes = await fetch("https://your-server-domain.com/watermark", {
  method: "POST",
  body: wmForm,
});
const { download_url } = await wmRes.json();
window.location = `https://your-server-domain.com${download_url}`;
```

### Desktop (Electron, or Python/C#/etc.)
Same idea — it's just HTTP. An Electron app calls it exactly like the
website example above (it's the same JS `fetch` API). A Python desktop
app (e.g. PyQt) would use the `requests` library the same way curl does.

## 6. Project structure

```
pdf-backend/
├── app/
│   ├── main.py         # API layer — all HTTP endpoints
│   ├── pdf_utils.py     # Core PDF logic (the "algorithm")
│   ├── storage.py       # File storage (swap for S3 later)
│   └── storage/
│       ├── uploads/     # Incoming files
│       └── outputs/     # Generated results
├── requirements.txt
├── Dockerfile
└── README.md
```

`pdf_utils.py` is deliberately separate from `main.py` — if you ever want
a command-line tool, a batch job, or a different API framework, you can
reuse that file directly without touching the web layer.

## 7. Extending it (where ML actually fits in)

The core editing operations above are plain software engineering — no ML
needed. If you want the "smart" features people associate with modern PDF
tools, this is where you'd add them:
- **Smarter OCR / handwriting** → swap Tesseract for a cloud OCR API or a
  fine-tuned model.
- **Auto-redaction** (detect names, emails, SSNs) → run extracted text
  through a NER model or regex + a lightweight classifier.
- **Summarize / chat with a PDF** → after `/extract-text`, send that text
  to an LLM API (e.g. Claude) and return the response.
- **Auto-detect form fields** → a layout-detection model (e.g. LayoutLM)
  on top of the page images from `/to-images`.

Each of these would be a new endpoint in `main.py` calling a new function
in `pdf_utils.py` — the architecture doesn't change.
