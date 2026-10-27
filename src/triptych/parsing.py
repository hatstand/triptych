"""Turn whatever the user forwarded (raw email, PDF, screenshot, pasted text) into LLM input."""

import base64
import email
import email.policy
import io
import re
from email.message import EmailMessage

import pillow_heif
from bs4 import BeautifulSoup
from pypdf import PdfReader

from triptych.models import IngestRequest, ParsedDocument

MAX_TEXT_CHARS = 30_000
MAX_IMAGES = 4
# Below this many characters of extractable text we assume a PDF is a scan and rasterise it.
SCANNED_PDF_THRESHOLD = 200
# ISO-BMFF brands of HEIF stills (iPhone photos), at bytes 8-12 after "ftyp" at 4-8. The model can't
# read HEIF, so these are sent as JPEG instead.
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}


def _is_heif(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS


def _image_b64(data: bytes) -> str:
    """An image as the model gets it: as it is, unless it's HEIF, which becomes a JPEG."""
    if _is_heif(data):
        buf = io.BytesIO()
        pillow_heif.open_heif(data).to_pillow().convert("RGB").save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
    return base64.b64encode(data).decode()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _render_pdf_pages(data: bytes, max_pages: int) -> list[str]:
    import pypdfium2 as pdfium

    out: list[str] = []
    pdf = pdfium.PdfDocument(data)
    for i in range(min(len(pdf), max_pages)):
        image = pdf[i].render(scale=1.5).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def pdf_to_parts(data: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if len(text) >= SCANNED_PDF_THRESHOLD:
        return text, []
    return text, _render_pdf_pages(data, max_pages=2)


def _parse_email(raw: bytes) -> ParsedDocument:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    texts: list[str] = []
    images: list[str] = []

    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        texts.append(html_to_text(content) if body.get_content_type() == "text/html" else content)

    for part in msg.iter_attachments():
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if ctype == "application/pdf":
            t, imgs = pdf_to_parts(payload)
            texts.append(f"--- attachment {part.get_filename() or ''} ---\n{t}")
            images.extend(imgs)
        elif ctype.startswith("image/") or _is_heif(payload):
            images.append(_image_b64(payload))
        elif ctype == "message/rfc822":
            # A forwarded-as-attachment email.
            inner = _parse_email(payload)
            texts.append(f"--- forwarded: {inner.subject or ''} ---\n{inner.text}")
            images.extend(inner.images_b64)

    subject = msg.get("subject")
    sender = msg.get("from")
    header = f"From: {sender}\n" if sender else ""
    return ParsedDocument(
        subject=str(subject) if subject else None,
        text=header + "\n\n".join(texts),
        images_b64=images,
    )


def _looks_like_email(data: bytes) -> bool:
    head = data[:2048].decode("utf-8", "ignore")
    return bool(re.search(r"^(From|Subject|Received|MIME-Version):", head, re.MULTILINE))


def parse(req: IngestRequest) -> ParsedDocument:
    data = base64.b64decode(req.data_b64)
    ctype = req.content_type.split(";")[0].strip().lower()
    name = (req.filename or "").lower()

    if req.source == "email" or ctype == "message/rfc822" or name.endswith(".eml"):
        doc = _parse_email(data)
    elif ctype == "application/pdf" or name.endswith(".pdf"):
        text, images = pdf_to_parts(data)
        doc = ParsedDocument(subject=req.subject, text=text, images_b64=images)
    elif ctype.startswith("image/") or _is_heif(data):  # a .heic can come as application/octet-stream
        doc = ParsedDocument(subject=req.subject, text="", images_b64=[_image_b64(data)])
    else:
        text = data.decode("utf-8", "replace")
        if ctype == "text/html" or "<html" in text[:500].lower():
            text = html_to_text(text)
        elif _looks_like_email(data):
            return _truncate(_parse_email(data))
        doc = ParsedDocument(subject=req.subject, text=text)

    if doc.subject is None:
        doc.subject = req.subject
    return _truncate(doc)


def _truncate(doc: ParsedDocument) -> ParsedDocument:
    return doc.model_copy(
        update={"text": doc.text[:MAX_TEXT_CHARS], "images_b64": doc.images_b64[:MAX_IMAGES]}
    )
