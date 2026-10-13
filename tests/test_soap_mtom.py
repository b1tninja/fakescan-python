"""MTOM binary extraction must split on the exact boundary, not on any ``\n--`` inside the payload."""

from fakescan import soap


def _mtom(payload: bytes, boundary: str = "uuid:abc123") -> bytes:
    b = boundary.encode()
    return (
        b"--" + b + b"\r\nContent-Type: application/xop+xml\r\n\r\n<?xml version='1.0'?><soap/>\r\n"
        b"--" + b + b"\r\nContent-Type: image/tiff\r\nContent-Transfer-Encoding: binary\r\n\r\n"
        + payload + b"\r\n--" + b + b"--\r\n"
    )


def test_payload_containing_newline_dashes_is_not_truncated():
    payload = b"II*\x00" + b"\x00" * 50 + b"\n--" + b"\xff" * 20 + b"\r\n--not-the-boundary\r\n" + b"\x01" * 100
    assert soap._mtom_binary_part(_mtom(payload)) == payload


def test_boundary_from_content_type_header():
    payload = b"BM" + b"\n--zz\n" * 10 + b"end"
    data = _mtom(payload, "MIME_boundary")
    ctype = 'multipart/related; type="application/xop+xml"; boundary="MIME_boundary"'
    assert soap._mtom_binary_part(data, soap._mime_boundary(ctype, data)) == payload
