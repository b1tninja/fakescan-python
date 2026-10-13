"""Local scan matrix against a fakescan this file starts.

Covers the rows in docs/test-matrix.md that do not need a Windows Imaging
install: profiles, ticket refusal, sticky_empty / feeder quirks, page order,
the seven smoke scenarios, manual duplex, loopback, faults (including
lamp_error / busy), and cancel. Imaging association (rows 1 and 12) stays a
person step.
"""

from __future__ import annotations

import re
import time
from io import BytesIO

import httpx
import pytest
from PIL import Image

from fakescan import Client
from fakescan.soap import (
    ScanJobRef,
    SoapFault,
    create_scan_job,
    format_magic_ok,
    get_scanner_elements,
    get_scanner_status,
    retrieve_image_bytes,
)

pytestmark = pytest.mark.local

SCAN_NS = "http://schemas.microsoft.com/windows/2006/01/wdp/scan"


@pytest.fixture
def client(fakescan_base_url: str):
    lab = Client(fakescan_base_url)
    lab.reset()
    lab.set_profile("brother-wsd")
    yield lab
    lab.reset()
    lab.set_profile("brother-wsd")
    lab.close()


def jpeg_comments(data: bytes) -> list[str]:
    """COM payloads in a JPEG, in order."""
    if data[:2] != b"\xff\xd8":
        return []
    out: list[str] = []
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or marker == 0xDA:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            break
        if marker == 0xFE:
            out.append(data[i + 4 : i + 2 + length].decode("latin1"))
        i += 2 + length
    return out


def marker(data: bytes, prefix: str) -> str | None:
    for comment in jpeg_comments(data):
        if comment.startswith(prefix):
            return comment[len(prefix) :]
    return None


def _ticket(
    action: str,
    fmt: str,
    dpi: int,
    source: str = "ADF",
    pages: int = 1,
    *,
    resolution_must_honor: bool = False,
    format_must_honor: bool = False,
) -> str:
    res_honor = ' wscn:MustHonor="true"' if resolution_must_honor else ""
    fmt_honor = ' wscn:MustHonor="true"' if format_must_honor else ""
    return f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:wscn="{SCAN_NS}">
  <soap:Header>
    <wsa:Action>{SCAN_NS}/{action}</wsa:Action>
    <wsa:MessageID>uuid:11111111-1111-1111-1111-111111111111</wsa:MessageID>
  </soap:Header>
  <soap:Body>
    <wscn:{action}Request>
      <wscn:ScanTicket>
        <wscn:DocumentParameters>
          <wscn:Format{fmt_honor}>{fmt}</wscn:Format>
          <wscn:ImagesToTransfer>{pages}</wscn:ImagesToTransfer>
          <wscn:InputSource>{source}</wscn:InputSource>
          <wscn:MediaSides>
            <wscn:MediaFront>
              <wscn:Resolution{res_honor}>
                <wscn:Width>{dpi}</wscn:Width>
                <wscn:Height>{dpi}</wscn:Height>
              </wscn:Resolution>
            </wscn:MediaFront>
          </wscn:MediaSides>
        </wscn:DocumentParameters>
      </wscn:ScanTicket>
    </wscn:{action}Request>
  </soap:Body>
</soap:Envelope>"""


def post_soap(base: str, xml: str) -> httpx.Response:
    response = httpx.post(
        f"{base}/ScannerService",
        content=xml.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=30.0,
    )
    return response


def test_status_exposes_wsd_discovery_sequence(client: Client):
    """Restart A/B: InstanceId / MetadataVersion visible without Wireshark."""
    st = client.status()
    assert st.wsd_instance_id >= 1
    assert st.wsd_metadata_version >= 1
    assert st.raw["wsd_instance_id"] == st.wsd_instance_id
    assert st.raw["wsd_metadata_version"] == st.wsd_metadata_version


def test_profiles_publish_connection_and_optical(client: Client):
    wsd = client.status()
    assert wsd.profile_id == "brother-wsd"
    assert wsd.raw["connection"] == "wsd"
    assert wsd.raw["optical"] is None
    assert "jfif" in wsd.raw["formats"]
    # The shipped lab profile is single-sided; hardware duplex is wia-rich / brother-usb.
    assert wsd.duplex is False

    usb = client.set_profile("brother-usb")
    assert usb.raw["connection"] == "usb"
    assert usb.raw["optical"]["x_dpi"] == 1200
    assert usb.raw["optical"]["y_dpi"] == 2400
    assert usb.raw["formats"] == ["bmp"]
    assert usb.raw["sticky_ready"] is True
    assert usb.duplex is True
    assert wsd.raw["sticky_ready"] is False

    glass = client.set_profile("glass-wsd")
    assert glass.duplex is False


def test_unknown_dpi_and_format_are_refused(client: Client, fakescan_base_url: str):
    ok = post_soap(fakescan_base_url, _ticket("ValidateScanTicket", "jfif", 200))
    assert ok.status_code == 200
    assert "<wscn:ValidTicket>true</wscn:ValidTicket>" in ok.text

    # Without MustHonor, brother-wsd soft-maps unknown dpi (pdfcraft WIA may do the same).
    soft_dpi = post_soap(fakescan_base_url, _ticket("ValidateScanTicket", "jfif", 999))
    assert soft_dpi.status_code == 200
    assert "<wscn:ValidTicket>true</wscn:ValidTicket>" in soft_dpi.text

    bad_dpi = post_soap(
        fakescan_base_url,
        _ticket("ValidateScanTicket", "jfif", 999, resolution_must_honor=True),
    )
    assert bad_dpi.status_code == 200
    assert "<wscn:ValidTicket>false</wscn:ValidTicket>" in bad_dpi.text

    # Inbox WSD soft-accepts unsupported format on Validate (Create still faults).
    bad_fmt_validate = post_soap(
        fakescan_base_url,
        _ticket("ValidateScanTicket", "png", 200, format_must_honor=True),
    )
    assert "<wscn:ValidTicket>true</wscn:ValidTicket>" in bad_fmt_validate.text

    created_fmt = post_soap(
        fakescan_base_url,
        _ticket("CreateScanJob", "png", 200, format_must_honor=True),
    )
    assert created_fmt.status_code == 500
    assert "Fault" in created_fmt.text

    created = post_soap(
        fakescan_base_url,
        _ticket("CreateScanJob", "jfif", 999, resolution_must_honor=True),
    )
    assert created.status_code == 500
    assert "Fault" in created.text


def test_sticky_feeder_stays_ready_and_platen_does_not_consume_it(client: Client, fakescan_base_url: str):
    """Row 4: glass with sticky FEED_READY must not pull the ADF."""
    client.set_profile("brother-usb")
    client.set_pages(0)
    status = client.status()
    assert status.raw["sticky_ready"] is True
    assert status.raw["feeder_ready"] is True
    assert status.pages_remaining == 0

    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="bmp", pages=1, dpi=100, input_source="Platen")
    pulled = post_soap(
        fakescan_base_url,
        f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:wscn="{SCAN_NS}">
  <soap:Header>
    <wsa:Action>{SCAN_NS}/RetrieveImage</wsa:Action>
    <wsa:MessageID>uuid:33333333-3333-3333-3333-333333333333</wsa:MessageID>
  </soap:Header>
  <soap:Body>
    <wscn:RetrieveImageRequest>
      <wscn:JobId>{job.job_id}</wscn:JobId>
      <wscn:JobToken>{job.token}</wscn:JobToken>
    </wscn:RetrieveImageRequest>
  </soap:Body>
</soap:Envelope>""",
    )
    # brother-usb encodes DIB, which is not a JFIF the loopback helper extracts.
    assert pulled.status_code == 200, pulled.text[:300]
    assert b"Fault" not in pulled.content[:800]
    assert len(pulled.content) > 100
    after = client.status()
    assert after.pages_remaining == 0
    assert after.raw["feeder_ready"] is True


def test_sticky_empty_adf_fails_and_platen_leaves_tray(client: Client, fakescan_base_url: str):
    """Row 5: sticky FEED_READY + empty stack → ADF Create faults; Platen OK."""
    client.set_profile("brother-usb")
    armed = client.set_scenario("sticky_empty")
    assert armed.raw["scenario"] == "sticky_empty"
    assert armed.raw["sticky_ready"] is True
    assert armed.raw["feeder_ready"] is True
    assert armed.pages_remaining == 0

    adf = post_soap(fakescan_base_url, _ticket("CreateScanJob", "bmp", 100, source="ADF"))
    assert adf.status_code >= 400
    assert "feeder empty" in adf.text

    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="bmp", pages=1, dpi=100, input_source="Platen")
        page = retrieve_image_bytes(http, fakescan_base_url, job)
    assert len(page) > 100
    after = client.status()
    assert after.pages_remaining == 0
    assert after.raw["feeder_ready"] is True
    assert after.raw["sticky_ready"] is True


def test_empty_feeder_refuses_adf(client: Client, fakescan_base_url: str):
    client.set_pages(0)
    assert client.status().raw["feeder_ready"] is False
    response = post_soap(fakescan_base_url, _ticket("CreateScanJob", "jfif", 100, source="ADF"))
    assert response.status_code >= 400
    assert "feeder empty" in response.text


def test_images_to_transfer_stops_the_stack(client: Client, fakescan_base_url: str):
    client.set_pages(5)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=2, dpi=100)
        first = retrieve_image_bytes(http, fakescan_base_url, job)
        second = retrieve_image_bytes(http, fakescan_base_url, job)
        with pytest.raises(SoapFault) as caught:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert marker(first, "fakescan-page:") == "0"
    assert marker(second, "fakescan-page:") == "1"
    assert "NoImagesAvailable" in (caught.value.subcode or caught.value.body)


def test_slow_retrieve_delays_and_cancel_drops_the_job(client: Client, fakescan_base_url: str):
    client.set_pages(2)
    client.set_slow(80)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        started = time.perf_counter()
        retrieve_image_bytes(http, fakescan_base_url, job)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert elapsed_ms >= 60

        cancelled = post_soap(
            fakescan_base_url,
            f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:wscn="{SCAN_NS}">
  <soap:Header>
    <wsa:Action>{SCAN_NS}/CancelJob</wsa:Action>
    <wsa:MessageID>uuid:22222222-2222-2222-2222-222222222222</wsa:MessageID>
  </soap:Header>
  <soap:Body>
    <wscn:CancelJobRequest>
      <wscn:JobId>{job.job_id}</wscn:JobId>
    </wscn:CancelJobRequest>
  </soap:Body>
</soap:Envelope>""",
        )
        assert cancelled.status_code == 200
        assert "CancelJobResponse" in cancelled.text
        with pytest.raises(SoapFault) as caught:
            retrieve_image_bytes(http, fakescan_base_url, job)
    body = caught.value.body.lower()
    assert "not found" in body or "cancelled" in body or "jobcancelled" in body


def test_smoke_scenarios_walk_in_order_and_stamp_pages(client: Client, fakescan_base_url: str):
    listed = client._json("GET", "/v1/scenarios")
    assert [row["id"] for row in listed] == ["order", "qr", "tone", "color", "orient", "crop", "duplex"]

    expected_next = {
        "order": "qr",
        "qr": "tone",
        "tone": "color",
        "color": "orient",
        "orient": "crop",
        "crop": "duplex",
        "duplex": None,
    }
    for row in listed:
        status = client.set_scenario(row["id"])
        assert status.image_pattern == row["pattern"]
        assert status.raw["scenario"] == row["id"]
        assert status.raw["scenario_step"] == row["step"]
        assert status.raw["scenario_check"]
        assert status.raw["scenario_next"] == expected_next[row["id"]]
        assert status.pages_remaining == row["pages"]
        with httpx.Client(timeout=60.0) as http:
            job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
            pages = [retrieve_image_bytes(http, fakescan_base_url, job) for _ in range(row["pages"])]
        indexes = [marker(page, "fakescan-page:") for page in pages]
        assert indexes == [str(i) for i in range(row["pages"])]
        if row["id"] == "duplex":
            assert [marker(page, "fakescan-side:") for page in pages] == ["front"] * row["pages"]


def test_manual_duplex_backs_come_out_last_sheet_first(client: Client, fakescan_base_url: str):
    client.set_pages(3, side="front")
    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        fronts = [retrieve_image_bytes(http, fakescan_base_url, job) for _ in range(3)]
    assert [marker(page, "fakescan-page:") for page in fronts] == ["0", "1", "2"]
    assert [marker(page, "fakescan-side:") for page in fronts] == ["front", "front", "front"]

    client.set_pages(3, side="back", order="reverse")
    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        backs = [retrieve_image_bytes(http, fakescan_base_url, job) for _ in range(3)]
    assert [marker(page, "fakescan-page:") for page in backs] == ["2", "1", "0"]
    assert [marker(page, "fakescan-side:") for page in backs] == ["back", "back", "back"]
    paired = list(zip(fronts, reversed(backs)))
    assert [marker(back, "fakescan-page:") for _front, back in paired] == ["0", "1", "2"]


def test_loopback_echoes_bytes_and_faults_do_not_clear_it(client: Client, fakescan_base_url: str):
    pages = [b"\xff\xd8LOOP-A\xff\xd9", b"\xff\xd8LOOP-B\xff\xd9"]
    info = client.set_loopback_bytes(pages)
    assert info.active and info.count == 2
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        got = [retrieve_image_bytes(http, fakescan_base_url, job) for _ in range(2)]
    assert got == pages

    client.set_loopback_bytes(pages)
    client.set_fault(jam=True)
    assert client.loopback().count == 2
    client.set_pages(1)
    assert client.loopback().count == 0


def test_jam_and_cover_show_on_scanner_status(client: Client, fakescan_base_url: str):
    client.set_fault(jam=True, cover_open=True)
    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, fakescan_base_url)
    assert "MediaJam" in xml
    assert "CoverOpen" in xml
    assert "Stopped" in xml


def test_hardware_adfduplex_emits_front_then_back(client: Client, fakescan_base_url: str):
    client.set_profile("wia-rich")
    assert client.status().duplex is True
    client.set_pages(2)
    with httpx.Client(timeout=60.0) as http:
        caps = get_scanner_elements(http, fakescan_base_url, "wscn:ScannerConfiguration")
        assert "<wscn:ADFSupportsDuplex>1</wscn:ADFSupportsDuplex>" in caps
        assert "<wscn:ADFBack>" in caps
        assert "<wscn:ADFFront>" in caps
        job = create_scan_job(
            http,
            fakescan_base_url,
            format="jfif",
            pages=0,
            dpi=100,
            input_source="ADFDuplex",
        )
        faces = [retrieve_image_bytes(http, fakescan_base_url, job) for _ in range(4)]
    assert [marker(page, "fakescan-side:") for page in faces] == [
        "front",
        "back",
        "front",
        "back",
    ]


def test_glass_only_refuses_adfduplex(client: Client, fakescan_base_url: str):
    client.set_profile("glass-wsd")
    assert client.status().duplex is False
    with httpx.Client(timeout=30.0) as http:
        caps = get_scanner_elements(http, fakescan_base_url, "wscn:ScannerConfiguration")
        assert "<wscn:ADF>" not in caps
        with pytest.raises(SoapFault) as refused:
            create_scan_job(
                http,
                fakescan_base_url,
                format="jfif",
                pages=1,
                dpi=100,
                input_source="ADFDuplex",
            )
    body = refused.value.subcode or refused.value.body
    assert "ADFDuplex" in body or "FormatNotSupported" in body or "not offered" in body


def test_offline_blocks_create_and_clears_on_reset(client: Client, fakescan_base_url: str):
    client.set_fault(offline=True)
    assert client.status().offline is True
    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, fakescan_base_url)
        assert "AttentionRequired" in xml
        assert "Stopped" in xml
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, fakescan_base_url, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)
    client.reset()
    assert client.status().offline is False


def test_lamp_error_blocks_create_and_clears_on_reset(client: Client, fakescan_base_url: str):
    client.set_fault(lamp_error=True)
    assert client.status().lamp_error is True
    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, fakescan_base_url)
        assert "LampError" in xml
        assert "Stopped" in xml
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, fakescan_base_url, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)
    client.reset()
    assert client.status().lamp_error is False


def test_busy_blocks_create_and_retrieve_and_clears_on_reset(client: Client, fakescan_base_url: str):
    client.set_pages(1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
    client.set_fault(busy=True)
    assert client.status().busy is True
    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, fakescan_base_url)
        assert "Processing" in xml
        with pytest.raises(SoapFault) as create_blocked:
            create_scan_job(http, fakescan_base_url, format="jfif", pages=1, dpi=100)
        with pytest.raises(SoapFault) as retrieve_blocked:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert "NotAcceptingJobs" in (create_blocked.value.subcode or create_blocked.value.body)
    assert "NotAcceptingJobs" in (retrieve_blocked.value.subcode or retrieve_blocked.value.body)
    assert "busy" in create_blocked.value.body.lower() or "NotAccepting" in create_blocked.value.body
    client.reset()
    assert client.status().busy is False


def test_mid_job_jam_and_empty_feeder(client: Client, fakescan_base_url: str):
    client.set_pages(4)
    client.set_fault(jam_after_n=1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, fakescan_base_url, job)
        with pytest.raises(SoapFault) as jammed:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert "MediaJam" in (jammed.value.subcode or jammed.value.body)
    assert client.status().jam is True

    client.reset()
    client.set_pages(4)
    client.set_fault(empty_mid_job=1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, fakescan_base_url, job)
        with pytest.raises(SoapFault) as empty:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert "NoImagesAvailable" in (empty.value.subcode or empty.value.body)
    assert client.status().jam is False

    client.reset()
    client.set_pages(4)
    client.set_fault(offline_after_n=1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, fakescan_base_url, job)
        with pytest.raises(SoapFault) as dropped:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert "NotAcceptingJobs" in (dropped.value.subcode or dropped.value.body)
    assert client.status().offline is True

    client.reset()
    client.set_pages(4)
    client.set_fault(multi_feed_after_n=1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, fakescan_base_url, job)
        with pytest.raises(SoapFault) as multi:
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert "MultipleFeed" in (multi.value.subcode or multi.value.body)
    assert client.status().multi_feed is True

    client.reset()
    client.set_pages(2)
    client.set_fault(abort_retrieve=True, abort_retrieve_bytes=64)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        with pytest.raises(Exception):
            retrieve_image_bytes(http, fakescan_base_url, job)
    assert client.status().abort_retrieve is True


def test_lamp_warmup_delays_only_the_first_page(client: Client, fakescan_base_url: str):
    client.set_pages(2)
    client.set_fault(lamp_warmup_ms=120)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=0, dpi=100)
        first = time.perf_counter()
        retrieve_image_bytes(http, fakescan_base_url, job)
        first_ms = (time.perf_counter() - first) * 1000
        second = time.perf_counter()
        retrieve_image_bytes(http, fakescan_base_url, job)
        second_ms = (time.perf_counter() - second) * 1000
    assert first_ms >= 90
    assert second_ms < first_ms - 40


def _chroma(page: bytes) -> int:
    img = Image.open(BytesIO(page)).convert("RGB")
    raw = img.tobytes()
    worst = 0
    for i in range(0, len(raw), 3):
        red, green, blue = raw[i], raw[i + 1], raw[i + 2]
        worst = max(worst, abs(red - green) + abs(green - blue))
    return worst


def _one_page(client: Client, base: str, *, pattern: str, gray: bool = False) -> bytes:
    client.set_pages(1)
    client.set_pattern(pattern)
    xml = _ticket("CreateScanJob", "jfif", 100, source="ADF", pages=1)
    if gray:
        xml = xml.replace(
            "<wscn:Format>jfif</wscn:Format>",
            "<wscn:Format>jfif</wscn:Format><wscn:ColorProcessing>Grayscale8</wscn:ColorProcessing>",
        )
    created = post_soap(base, xml)
    assert created.status_code == 200, created.text[:400]
    job_id = re.search(r"<[^:>]*:?JobId>(\d+)</", created.text)
    token = re.search(r"<[^:>]*:?JobToken>([^<]+)</", created.text)
    assert job_id and token
    with httpx.Client(timeout=30.0) as http:
        return retrieve_image_bytes(
            http,
            base,
            ScanJobRef(job_id=int(job_id.group(1)), token=token.group(1).strip()),
        )


def test_pattern_pages_differ_and_gray8_drops_chroma(client: Client, fakescan_base_url: str):
    pages = {
        name: _one_page(client, fakescan_base_url, pattern=name)
        for name in ("wedges", "patches", "slant_f", "corners")
    }
    blobs = list(pages.values())
    assert len({blob for blob in blobs}) == 4
    assert _chroma(pages["patches"]) > 20
    gray = _one_page(client, fakescan_base_url, pattern="patches", gray=True)
    assert _chroma(gray) <= 2


def test_dirty_adf_effect_still_returns_a_page(client: Client, fakescan_base_url: str):
    client.set_pages(1)
    client.set_effects(preset="dirty_adf", seed=1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, fakescan_base_url, format="jfif", pages=1, dpi=100)
        page = retrieve_image_bytes(http, fakescan_base_url, job)
    assert page[:2] == b"\xff\xd8"
    assert marker(page, "fakescan-page:") == "0"


# WSD Format tokens advertised by wia-rich (see docs/formats.md).
_WIA_RICH_SINGLE = (
    "jfif",
    "exif",
    "png",
    "dib",
    "tiff-single-uncompressed",
    "tiff-single-g4",
    "tiff-single-g3mh",
    "tiff-single-jpeg-tn2",
    "jpeg2k",
)
_WIA_RICH_MULTI = (
    "tiff-multi-uncompressed",
    "tiff-multi-g4",
    "pdf-a",
    "xps",
)


def test_wia_rich_formats_retrieve_with_expected_magic(client: Client, fakescan_base_url: str):
    """Every shipped single-page format Create+Retrieve has the right magic."""
    client.set_profile("wia-rich")
    with httpx.Client(timeout=60.0) as http:
        caps = get_scanner_elements(http, fakescan_base_url, "wscn:ScannerConfiguration")
        for fmt in _WIA_RICH_SINGLE + _WIA_RICH_MULTI:
            assert f"<wscn:FormatValue>{fmt}</wscn:FormatValue>" in caps, fmt
        for fmt in _WIA_RICH_SINGLE:
            client.set_pages(1)
            job = create_scan_job(
                http,
                fakescan_base_url,
                format=fmt,
                pages=1,
                dpi=100,
                input_source="Platen",
            )
            blob = retrieve_image_bytes(http, fakescan_base_url, job)
            assert format_magic_ok(fmt, blob), f"{fmt}: magic fail ({blob[:16]!r})"


def test_wia_rich_multipage_formats_consume_the_stack(client: Client, fakescan_base_url: str):
    """tiff-multi / pdf-a / xps: one RetrieveImage returns all armed pages."""
    client.set_profile("wia-rich")
    with httpx.Client(timeout=90.0) as http:
        for fmt in _WIA_RICH_MULTI:
            client.set_pages(3)
            job = create_scan_job(
                http,
                fakescan_base_url,
                format=fmt,
                pages=0,
                dpi=100,
                input_source="ADF",
            )
            blob = retrieve_image_bytes(http, fakescan_base_url, job)
            assert format_magic_ok(fmt, blob), f"{fmt}: magic fail ({blob[:16]!r})"
            with pytest.raises(SoapFault) as done:
                retrieve_image_bytes(http, fakescan_base_url, job)
            assert "NoImagesAvailable" in (done.value.subcode or done.value.body)
