"""Offline unit tests (no fakescan process)."""

import httpx

from fakescan.client import Activity, ActivityEvent, Client, Job, loopback_digest


def _client(handler) -> Client:
    return Client(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_loopback_digest_matches_rust_shape():
    data = b"\xff\xd8\xff\xe0\x00\x10JF"
    assert loopback_digest(data) == f"{len(data)}:{data[:8].hex()}"
    assert loopback_digest(b"") == "0:"


def test_jobs_reads_job_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/jobs"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 3,
                        "state": "Processing",
                        "format": "jfif",
                        "source": "Feeder",
                        "dpi": 300,
                        "color": "RGB24",
                        "pages_left": 1,
                        "page_index": 0,
                        "width_px": 2550,
                        "height_px": 3300,
                    }
                ]
            },
        )

    assert _client(handler).jobs() == [
        Job(
            id=3,
            state="Processing",
            format="jfif",
            source="Feeder",
            dpi=300,
            color="RGB24",
            pages_left=1,
            page_index=0,
            width_px=2550,
            height_px=3300,
        )
    ]


def test_jobs_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs"
        return httpx.Response(200, json={"jobs": []})

    assert _client(handler).jobs() == []


def test_activity_reads_events_after_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/activity"
        assert request.url.params["after"] == "4"
        return httpx.Response(
            200,
            json={
                "seq": 6,
                "events": [
                    {
                        "seq": 5,
                        "kind": "wsd",
                        "peer": "127.0.0.1:9",
                        "action": "CreateScanJob",
                        "outcome": "ok",
                        "ms": 12,
                        "detail": "job 3",
                    }
                ],
            },
        )

    assert _client(handler).activity(after=4) == Activity(
        seq=6,
        events=[
            ActivityEvent(
                seq=5,
                kind="wsd",
                peer="127.0.0.1:9",
                action="CreateScanJob",
                outcome="ok",
                ms=12,
                detail="job 3",
            )
        ],
    )


def test_log_text_returns_plain_body():
    body = "5 wsd CreateScanJob ok 12ms job 3\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/log"
        assert request.url.params["after"] == "0"
        return httpx.Response(200, text=body)

    assert _client(handler).log_text() == body
