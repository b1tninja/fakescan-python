"""Tests for fakescan.wsd_schema (WSD Scan Service XSD model + body validation)."""

from __future__ import annotations

from fakescan.wsd_schema import load_schema, body_problems

NS = "http://schemas.microsoft.com/windows/2006/08/wdp/scan"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"

EXPECTED_ELEMENTS = [
    "CancelJobRequest", "CancelJobResponse",
    "CreateScanJobRequest", "CreateScanJobResponse",
    "DestinationResponses",
    "GetActiveJobsRequest", "GetActiveJobsResponse",
    "GetJobElementsRequest", "GetJobElementsResponse",
    "GetJobHistoryRequest", "GetJobHistoryResponse",
    "GetScannerElementsRequest", "GetScannerElementsResponse",
    "JobEndStateEvent", "JobStatusEvent",
    "RetrieveImageRequest", "RetrieveImageResponse",
    "ScanAvailableEvent", "ScanDestinations", "Scanner",
    "ScannerElementsChangeEvent", "ScannerStatusConditionClearedEvent",
    "ScannerStatusConditionEvent", "ScannerStatusSummaryEvent",
    "ValidateScanTicketRequest", "ValidateScanTicketResponse",
]

EXPECTED_TYPES = [
    "JobType", "ScanTicketType", "JobDescriptionType",
    "DocumentParametersType", "CreateScanJobRequestType", "DocumentFormatType",
]


def envelope(body_inner: str) -> str:
    return (
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:scan="{NS}">'
        f"<soap:Body>{body_inner}</soap:Body>"
        "</soap:Envelope>"
    )


def _particle(type_name: str, name: str):
    for p in load_schema().types[type_name].particles:
        if p.name == name:
            return p
    raise AssertionError(f"{type_name} has no particle {name}")


def test_elements_exact_set():
    schema = load_schema()
    assert set(schema.elements) == set(EXPECTED_ELEMENTS)
    assert len(schema.elements) == 26
    assert schema.elements["CreateScanJobRequest"].endswith("CreateScanJobRequestType")


def test_inline_names_are_not_global_elements():
    schema = load_schema()
    for name in ("Job", "ScanTicket", "Format"):
        assert name not in schema.elements, f"{name} should be inline, not an element"


def test_expected_types_present():
    schema = load_schema()
    for t in EXPECTED_TYPES:
        assert t in schema.types, t


def test_create_scan_job_request_particles():
    assert (_particle("CreateScanJobRequestType", "ScanIdentifier").min,
            _particle("CreateScanJobRequestType", "ScanIdentifier").max) == (0, 1)
    assert (_particle("CreateScanJobRequestType", "DestinationToken").min,
            _particle("CreateScanJobRequestType", "DestinationToken").max) == (0, 1)
    assert (_particle("CreateScanJobRequestType", "ScanTicket").min,
            _particle("CreateScanJobRequestType", "ScanTicket").max) == (1, 1)


def test_scan_ticket_particles():
    assert (_particle("ScanTicketType", "JobDescription").min,
            _particle("ScanTicketType", "JobDescription").max) == (1, 1)
    assert (_particle("ScanTicketType", "DocumentParameters").min,
            _particle("ScanTicketType", "DocumentParameters").max) == (0, 1)
    any_p = [p for p in load_schema().types["ScanTicketType"].particles if p.kind == "any"]
    assert len(any_p) == 1
    assert any_p[0].min == 0 and any_p[0].max is None  # unbounded


def test_job_description_particles():
    assert (_particle("JobDescriptionType", "JobName").min,
            _particle("JobDescriptionType", "JobName").max) == (1, 1)
    assert (_particle("JobDescriptionType", "JobOriginatingUserName").min,
            _particle("JobDescriptionType", "JobOriginatingUserName").max) == (1, 1)
    assert (_particle("JobDescriptionType", "JobInformation").min,
            _particle("JobDescriptionType", "JobInformation").max) == (0, 1)
    any_p = [p for p in load_schema().types["JobDescriptionType"].particles if p.kind == "any"]
    assert len(any_p) == 1
    assert any_p[0].min == 0 and any_p[0].max is None


def test_boolean_attributes():
    schema = load_schema()
    for name in ("MustHonor", "Override", "UsedDefault"):
        assert name in schema.attributes
        assert schema.attributes[name].kind == "boolean"


# ------------------------------------------------------------- body_problems

def test_valid_create_scan_job_request():
    xml = envelope(
        "<scan:CreateScanJobRequest>"
        "<scan:ScanTicket>"
        "<scan:JobDescription>"
        "<scan:JobName>TestJob</scan:JobName>"
        "<scan:JobOriginatingUserName>alice</scan:JobOriginatingUserName>"
        "</scan:JobDescription>"
        "</scan:ScanTicket>"
        "</scan:CreateScanJobRequest>"
    )
    assert body_problems(xml) == []


def test_unknown_element():
    xml = envelope("<scan:DoesNotExist/>")
    problems = body_problems(xml)
    assert any("unknown element: DoesNotExist" in m for m in problems)


def test_missing_required_scan_ticket():
    xml = envelope(
        "<scan:CreateScanJobRequest>"
        "<scan:JobDescription>"
        "<scan:JobName>J</scan:JobName>"
        "<scan:JobOriginatingUserName>u</scan:JobOriginatingUserName>"
        "</scan:JobDescription>"
        "</scan:CreateScanJobRequest>"
    )
    problems = body_problems(xml)
    assert any("missing element: ScanTicket" in m for m in problems)


def _job_request(inner_extra: str) -> str:
    return (
        "<scan:CreateScanJobRequest><scan:ScanTicket>"
        "<scan:JobDescription>"
        "<scan:JobName>J</scan:JobName>"
        "<scan:JobOriginatingUserName>u</scan:JobOriginatingUserName>"
        "</scan:JobDescription>"
        f"{inner_extra}"
        "</scan:ScanTicket></scan:CreateScanJobRequest>"
    )


def test_lab_extension_child_allowed():
    """ScanTicket has an xs:any tail: same-NS lab extensions (JobToken) are OK."""
    problems = body_problems(envelope(_job_request("<scan:JobToken>abc</scan:JobToken>")))
    assert not any("unexpected" in m or "missing" in m for m in problems)
    assert problems == []


def test_bad_simple_value():
    """DocumentParameters/Format is constrained to DocumentFormatWKVType or a
    keyword-extension pattern; a nonsense value is rejected, 'png' accepted."""
    fmt = "<scan:DocumentParameters><scan:Format>{}</scan:Format></scan:DocumentParameters>"
    bad = body_problems(envelope(_job_request(fmt.format("not-a-format"))))
    assert any("value is not a valid" in m for m in bad)
    assert body_problems(envelope(_job_request(fmt.format("png")))) == []


def test_non_scan_child_passes_through():
    xml = (
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:scan="{NS}">'
        '<soap:Body><other:Echo xmlns:other="http://example.com/other">x</other:Echo>'
        "</soap:Body></soap:Envelope>"
    )
    problems = body_problems(xml)
    assert not any("Echo" in m for m in problems)
    assert problems == []
