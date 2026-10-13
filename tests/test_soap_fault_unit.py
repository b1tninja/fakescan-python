"""Offline SOAP fault parsing (no server)."""

from fakescan.soap import SoapFault, _soap_reason, _soap_subcode


def test_soap_fault_parses_media_jam_subcode():
    body = """<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Body>
    <soap:Fault>
      <soap:Code>
        <soap:Value>soap:Receiver</soap:Value>
        <soap:Subcode>
          <soap:Value>wscn:ClientErrorMediaJam</soap:Value>
        </soap:Subcode>
      </soap:Code>
      <soap:Reason>
        <soap:Text xml:lang="en">ClientErrorMediaJam: jam_after_n=2</soap:Text>
      </soap:Reason>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>"""
    assert _soap_subcode(body) == "wscn:ClientErrorMediaJam"
    assert "jam_after_n" in (_soap_reason(body) or "")
    fault = SoapFault(500, body)
    assert "MediaJam" in (fault.subcode or "")
