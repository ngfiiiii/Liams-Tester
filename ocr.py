import io
import re
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract

UUID_RE = re.compile(
    r"(?i)\b([a-f0-9]{8}[-\s]?[a-f0-9]{4}[-\s]?[a-f0-9]{4}[-\s]?[a-f0-9]{4}[-\s]?[a-f0-9]{12}|[a-f0-9]{32})\b"
)

def normalize_session_id(value: str) -> str | None:
    """Return a lowercase 32-char Fortnite Tracker session id from a URL, UUID, or OCR text."""
    if not value:
        return None
    raw = value.strip()
    # If a full Fortnite Tracker URL is pasted, this catches the last long id.
    matches = UUID_RE.findall(raw.replace("_", ""))
    if not matches:
        # OCR often inserts spaces between chunks. Remove whitespace/hyphens then retry.
        compact = re.sub(r"[^A-Fa-f0-9]", "", raw)
        if len(compact) >= 32:
            possible = compact[-32:]
            if re.fullmatch(r"(?i)[a-f0-9]{32}", possible):
                return possible.lower()
        return None
    return re.sub(r"[^A-Fa-f0-9]", "", matches[-1]).lower()

def _prepare_variants(img: Image.Image) -> list[Image.Image]:
    """Make multiple OCR-friendly versions of a screenshot/crop."""
    variants: list[Image.Image] = []
    base = img.convert("RGB")

    w, h = base.size
    crops = [base]
    # Top-right Fortnite session text is usually in the top-right corner.
    crops.append(base.crop((int(w * 0.50), 0, w, int(h * 0.28))))
    crops.append(base.crop((int(w * 0.58), 0, w, int(h * 0.20))))
    crops.append(base.crop((int(w * 0.40), 0, w, int(h * 0.35))))

    for crop in crops:
        cw, ch = crop.size
        # Upscale because Fortnite HUD text is tiny.
        up = crop.resize((cw * 4, ch * 4), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(up)
        gray = ImageEnhance.Contrast(gray).enhance(2.5)
        gray = gray.filter(ImageFilter.SHARPEN)
        variants.append(gray)
        # Thresholded version can help with white text on game background.
        bw = gray.point(lambda p: 255 if p > 155 else 0)
        variants.append(bw)
    return variants

def extract_session_id_from_image(image_bytes: bytes) -> tuple[str | None, str]:
    """OCR screenshot bytes and return (session_id, debug_text)."""
    img = Image.open(io.BytesIO(image_bytes))
    texts: list[str] = []
    config = "--psm 6 -c tessedit_char_whitelist=0123456789abcdefABCDEF- "

    for variant in _prepare_variants(img):
        try:
            txt = pytesseract.image_to_string(variant, config=config)
        except Exception as exc:
            texts.append(f"OCR_ERROR:{exc}")
            continue
        if txt:
            texts.append(txt)
            found = normalize_session_id(txt)
            if found:
                return found, "\n".join(texts)

    all_text = "\n".join(texts)
    return normalize_session_id(all_text), all_text
