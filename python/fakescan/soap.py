"""Minimal WS-Scan SOAP helpers for CI (no WIA).

Creates a job and pulls RetrieveImage MTOM parts so loopback/faults can be
asserted without pdfcraft or Windows Imaging.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import httpx

SCAN_NS = "http://schemas.microsoft.com/windows/2006/01/wdp/scan"


def _envelope(action_local: str, body_xml: str) -> str:
    action = f"{SCAN_NS}/{action_local}"
    msg_id = str(uuid.uuid4())
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:wscn="{SCAN_NS}">
  <soap:Header>
    <wsa:Action>{action}</wsa:Action>
    <wsa:MessageID>uuid:{msg_id}</wsa:MessageID>
  </soap:Header>
  <soap:Body>
{body_xml}
  </soap:Body>
</soap:Envelope>
"""


@dataclass
class ScanJobRef:
    job_id: int
    token: str


class SoapFault(RuntimeError):
    """WS-Scan SOAP Fault (HTTP 500 with subcode / reason)."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        self.subcode = _soap_subcode(body)
        self.reason = _soap_reason(body) or body[:200]
        super().__init__(f"SOAP Fault {status_code}: {self.subcode or '?'} {self.reason}")


def _soap_subcode(text: str) -> str | None:
    m = re.search(r"<[^:>]*:?Value>([^<]+)</", text)
    if m and "Error" in m.group(1):
        return m.group(1).strip()
    # Prefer the deepest Value that looks like a scan fault.
    hits = re.findall(r"<[^:>]*:?Value>\s*([^<]*ClientError[^<]*)\s*</", text)
    return hits[-1].strip() if hits else None


def _soap_reason(text: str) -> str | None:
    m = re.search(r"<[^:>]*:?Text[^>]*>([^<]+)</", text)
    return m.group(1).strip() if m else None


def create_scan_job(
    client: httpx.Client,
    base_url: str,
    *,
    format: str = "jfif",
    pages: int = 0,
    dpi: int = 100,
    input_source: str = "ADF",
    color: str | None = None,
) -> ScanJobRef:
    """POST CreateScanJob; return JobId + JobToken.

    ``color`` is a WSD ColorProcessing token (``RGB24``, ``Grayscale8``,
    ``BlackAndWhite1``); omitted leaves the device default.
    """
    color_xml = (
        f"""
          <wscn:MediaSides><wscn:MediaFront>
            <wscn:ColorProcessing>{color}</wscn:ColorProcessing>
          </wscn:MediaFront></wscn:MediaSides>"""
        if color
        else ""
    )
    body = f"""    <wscn:CreateScanJobRequest>
      <wscn:ScanTicket>
        <wscn:JobDescription>
          <wscn:JobName>fakescan-lab</wscn:JobName>
          <wscn:JobOriginatingUserName>lab</wscn:JobOriginatingUserName>
        </wscn:JobDescription>
        <wscn:DocumentParameters>
          <wscn:Format>{format}</wscn:Format>
          <wscn:ImagesToTransfer>{pages}</wscn:ImagesToTransfer>
          <wscn:InputSource>{input_source}</wscn:InputSource>
          <wscn:Resolution>
            <wscn:Width>{dpi}</wscn:Width>
            <wscn:Height>{dpi}</wscn:Height>
          </wscn:Resolution>{color_xml}
        </wscn:DocumentParameters>
      </wscn:ScanTicket>
    </wscn:CreateScanJobRequest>"""
    env = _envelope("CreateScanJob", body)
    r = client.post(
        f"{base_url.rstrip('/')}/ScannerService",
        content=env.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    text = r.text
    if r.status_code >= 400 or "Fault" in text[:800]:
        raise SoapFault(r.status_code, text)
    m_id = re.search(r"<[^:>]*:?JobId>(\d+)</", text)
    m_tok = re.search(r"<[^:>]*:?JobToken>([^<]+)</", text)
    if not m_id or not m_tok:
        raise RuntimeError(f"CreateScanJob missing JobId/JobToken: {text[:400]}")
    return ScanJobRef(job_id=int(m_id.group(1)), token=m_tok.group(1).strip())


def get_scanner_elements(
    client: httpx.Client,
    base_url: str,
    *names: str,
) -> str:
    """POST GetScannerElements for the named elements; return response XML."""
    if not names:
        names = ("wscn:ScannerStatus",)
    requested = "\n".join(f"        <wscn:Name>{name}</wscn:Name>" for name in names)
    body = f"""    <wscn:GetScannerElementsRequest>
      <wscn:RequestedElements>
{requested}
      </wscn:RequestedElements>
    </wscn:GetScannerElementsRequest>"""
    r = client.post(
        f"{base_url.rstrip('/')}/ScannerService",
        content=_envelope("GetScannerElements", body).encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    if r.status_code >= 400 or "Fault" in r.text[:800]:
        raise SoapFault(r.status_code, r.text)
    return r.text


def get_scanner_status(client: httpx.Client, base_url: str) -> str:
    """POST GetScannerElements for ScannerStatus; return response XML."""
    return get_scanner_elements(client, base_url, "wscn:ScannerStatus")


def retrieve_image_bytes(
    client: httpx.Client,
    base_url: str,
    job: ScanJobRef,
) -> bytes:
    """POST RetrieveImage; return the binary image part (MTOM or raw)."""
    body = f"""    <wscn:RetrieveImageRequest>
      <wscn:JobId>{job.job_id}</wscn:JobId>
      <wscn:JobToken>{job.token}</wscn:JobToken>
    </wscn:RetrieveImageRequest>"""
    env = _envelope("RetrieveImage", body)
    r = client.post(
        f"{base_url.rstrip('/')}/ScannerService",
        content=env.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    text_preview = r.text if "xml" in (r.headers.get("content-type") or "").lower() else ""
    if r.status_code >= 400 or "Fault" in text_preview[:800]:
        body_text = r.text if r.text else r.content.decode("utf-8", "replace")
        if "Fault" in body_text or r.status_code >= 400:
            raise SoapFault(r.status_code, body_text)
    ctype = r.headers.get("content-type", "")
    data = r.content
    if "multipart" in ctype.lower() or b"--" in data[:80]:
        return _mtom_binary_part(data, _mime_boundary(ctype, data))
    payload = _payload_from_bytes(data)
    if payload is not None:
        return payload
    # SOAP fault may still be HTTP 200 on some stacks.
    body_text = data.decode("utf-8", "replace")
    if "Fault" in body_text:
        raise SoapFault(r.status_code, body_text)
    raise RuntimeError("RetrieveImage: could not locate image bytes")


_CONTAINER_MAGICS = (
    b"%PDF-",  # PDF embeds JPEG — must win over SOI search
    b"PK\x03\x04",  # XPS zip
    b"II*\x00",  # TIFF (may embed JPEG TN2)
    b"MM\x00*",
    b"\x89PNG\r\n\x1a\n",
    b"BM",
    b"\x00\x00\x00\x0cjP  ",  # JP2
    b"\xff\xd8",  # JPEG last: containers often contain SOI
)


def _starts_with_magic(data: bytes) -> bytes | None:
    """If `data` begins with a known container/image magic, return trimmed payload."""
    raw = data.lstrip(b"\r\n")
    for magic in _CONTAINER_MAGICS:
        if raw.startswith(magic):
            chunk = raw
            if magic == b"\xff\xd8":
                eoi = chunk.rfind(b"\xff\xd9")
                if eoi >= 0:
                    chunk = chunk[: eoi + 2]
            return chunk.rstrip(b"\r\n")
    return None


def _payload_from_bytes(data: bytes) -> bytes | None:
    """Return a known payload. Prefer start-of-buffer magic (containers embed JPEG)."""
    at_start = _starts_with_magic(data)
    if at_start is not None:
        return at_start
    # Fallback: locate magic inside a mixed buffer (raw HTTP without clean part split).
    for magic in _CONTAINER_MAGICS:
        i = data.find(magic)
        if i < 0:
            continue
        return _starts_with_magic(data[i:]) or data[i:].rstrip(b"\r\n")
    return None


def _mime_boundary(ctype: str, data: bytes) -> bytes | None:
    """The exact multipart boundary: from the Content-Type header, else the first body line."""
    m = re.search(r'boundary="?([^";\s]+)"?', ctype or "", re.IGNORECASE)
    if m:
        return m.group(1).encode("ascii", "ignore")
    first = data.lstrip(b"\r\n").split(b"\r\n", 1)[0]
    return first[2:] if first.startswith(b"--") and len(first) > 2 else None


def _mtom_binary_part(data: bytes, boundary: bytes | None = None) -> bytes:
    """Extract the largest binary part from a multipart/related MTOM response.

    Splits on the exact boundary: binary payloads may contain ``\\n--`` and must not be cut there.
    """
    boundary = boundary or _mime_boundary("", data)
    if boundary:
        parts = re.split(br"\r?\n?--" + re.escape(boundary) + br"(?:--)?", data)
    else:
        parts = re.split(br"\r?\n--[^\r\n]+", data)
    best = b""
    for part in parts:
        body = part
        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end >= 0:
            body = part[hdr_end + 4 :]
        # Drop trailing MIME boundary crumbs.
        if body.endswith(b"--"):
            body = body[:-2]
        body = body.rstrip(b"\r\n")
        if not body or body.lstrip().startswith(b"<?xml") or body.lstrip().startswith(b"<soap"):
            continue
        # Prefer the part body as-is when it starts with a container magic.
        payload = _starts_with_magic(body) or body
        if len(payload) > len(best):
            best = payload
    if not best:
        raise RuntimeError("MTOM: no image part found")
    return best


def format_magic_ok(fmt: str, data: bytes) -> bool:
    """True when `data` matches the expected magic for a WSD Format token."""
    if not data:
        return False
    key = fmt.lower()
    if key in ("jfif", "jpeg", "jpg", "exif"):
        return data[:2] == b"\xff\xd8"
    if key in ("dib", "bmp"):
        return data[:2] == b"BM"
    if key == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if "tiff" in key:
        return data[:4] in (b"II*\x00", b"MM\x00*")
    if key in ("jpeg2k", "jp2", "j2k"):
        # JP2 signature box: len=12, type `jP  `, then `\r\n\x87\n`.
        return data.startswith(b"\x00\x00\x00\x0cjP  ") or data[:4] == b"\xff\x4f\xff\x51"
    if key in ("pdf-a", "pdf"):
        return data.startswith(b"%PDF-")
    if key == "xps":
        return data[:4] == b"PK\x03\x04"
    return len(data) > 16


def acquire_pages(
    base_url: str,
    *,
    page_count: int,
    format: str = "jfif",
    dpi: int = 100,
    input_source: str = "ADF",
    timeout: float = 60.0,
) -> list[bytes]:
    """CreateScanJob + RetrieveImage × N (no WIA). Feeder must be loaded for ADF."""
    pages: list[bytes] = []
    src = input_source
    if src.lower() in ("platen", "glass"):
        src = "Platen"
        page_count = max(page_count, 1)
        images = 1
    else:
        # ADF: 0 = until empty / available stack.
        images = 0
    with httpx.Client(timeout=timeout) as http:
        job = create_scan_job(
            http,
            base_url,
            format=format,
            pages=images,
            dpi=dpi,
            input_source=src,
        )
        for _ in range(page_count):
            pages.append(retrieve_image_bytes(http, base_url, job))
    return pages


def acquire_jfif_pages(
    base_url: str,
    *,
    page_count: int,
    dpi: int = 100,
    timeout: float = 60.0,
) -> list[bytes]:
    """ADF JFIF acquire. Requires feeder already loaded."""
    return acquire_pages(
        base_url,
        page_count=page_count,
        format="jfif",
        dpi=dpi,
        input_source="ADF",
        timeout=timeout,
    )


def acquire_dib_platen(
    base_url: str,
    *,
    dpi: int = 200,
    timeout: float = 120.0,
) -> bytes:
    """Platen wire ``dib`` (raw BMP, no JFIF). Profile must advertise ``dib`` (e.g. accept-dib)."""
    pages = acquire_pages(
        base_url,
        page_count=1,
        format="dib",
        dpi=dpi,
        input_source="Platen",
        timeout=timeout,
    )
    return pages[0]
