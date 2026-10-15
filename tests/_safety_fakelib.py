"""A recording stand-in for the device-touching libraries: every call is appended to CALLS."""

from __future__ import annotations

from dataclasses import dataclass

CALLS: list[tuple] = []


def open_device(url, timeout=None):
    CALLS.append(("open_device", url))
    return "opened"


def scan(*, device="", registry=None):
    CALLS.append(("scan", device))
    return "scanned"


def list_scanners(*, device=None, details=False):
    CALLS.append(("list_scanners", device, details))
    rows = [dict(r) for r in ROWS]
    return {"scanners": rows, "errors": []}


def scanner_details(id):  # noqa: A002
    CALLS.append(("scanner_details", id))
    row = next(r for r in ROWS if r["id"] == id)
    return {**row, "details": "loaded"}


def scanner_dump(filter):  # noqa: A002
    CALLS.append(("scanner_dump", filter))
    return "dump"


@dataclass(frozen=True)
class Submitted:
    job: object = None


@dataclass(frozen=True)
class PrinterRef:
    id: str
    name: str


@dataclass(frozen=True)
class Listing:
    items: tuple
    errors: tuple = ()


class Registry:
    def __init__(self):
        self.printers = []

    def register_wsd(self, urls):
        CALLS.append(("register_wsd", tuple(urls)))
        return tuple(urls)

    def submit(self, document, *, printer=None, job=None):
        CALLS.append(("submit", printer))
        return Submitted(job="wsd:http://203.0.113.9/p#1")

    submit_job = submit

    def list_printers(self):
        CALLS.append(("list_printers",))
        return Listing(tuple(PRINTERS), ())

    def job_status(self, job_id):
        CALLS.append(("job_status", str(job_id)))
        return "status"


FAKE = "00000000-0000-4000-8000-00000000abcd"
REAL = "e3248000-80ce-11db-8000-3c2af435905f"  # an arbitrary non-fake uuid for these tests
BS = chr(92)
ROWS = [
    {"id": "wia:{11111111-2222-3333-4444-555555555555}" + BS + "0000", "name": "Fake Scanner", "port": f"urn:uuid:{FAKE}/uri:x"},
    {"id": "wia:{66666666-2222-3333-4444-555555555555}" + BS + "0000", "name": "Office MFC", "port": f"urn:uuid:{REAL}/uri:x"},
    {"id": "wia:{77777777-2222-3333-4444-555555555555}" + BS + "0000", "name": "Office MFC", "port": BS * 2 + "." + BS + "Usbscan0"},
]
PRINTERS = [PrinterRef("wsd:http://127.0.0.1:5357/p", "fakeprint inkjet"), PrinterRef("wsd:http://203.0.113.9/p", "Office")]


def reset():
    CALLS.clear()
