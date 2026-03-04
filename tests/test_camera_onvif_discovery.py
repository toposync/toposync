from __future__ import annotations

from toposync_ext_cameras.onvif import parse_ws_discovery_probe_matches


def test_parse_ws_discovery_probe_matches_extracts_device_id_xaddrs_and_scopes() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <e:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:1234</wsa:Address></wsa:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>
          onvif://www.onvif.org/name/Front%20Gate
          onvif://www.onvif.org/hardware/Tapo%20C200
        </d:Scopes>
        <d:XAddrs>http://192.168.0.10/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </e:Body>
</e:Envelope>
"""

    devices = parse_ws_discovery_probe_matches(xml, source_ip="192.168.0.10")
    assert len(devices) == 1
    d = devices[0]
    assert d.device_id == "urn:uuid:1234"
    assert d.xaddrs == ["http://192.168.0.10/onvif/device_service"]
    assert d.source_ip == "192.168.0.10"
    assert d.name == "Front Gate"
    assert d.hardware == "Tapo C200"

