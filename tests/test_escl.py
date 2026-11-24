import xml.etree.ElementTree as ET

from brscan.escl import ESCL_NS, PWG_NS, ScanSettings, jpeg_complete

NS = {"scan": ESCL_NS, "pwg": PWG_NS}


def test_scan_settings_xml_letter():
    root = ET.fromstring(ScanSettings(res=300, duplex=True).to_xml())
    assert root.find("scan:XResolution", NS).text == "300"
    assert root.find("scan:Duplex", NS).text == "true"
    assert root.find("pwg:InputSource", NS).text == "Feeder"
    regions = root.find("pwg:ScanRegions", NS)
    assert regions.get(f"{{{PWG_NS}}}MustHonor") == "true"
    region = regions.find("pwg:ScanRegion", NS)
    assert region.find("pwg:Width", NS).text == "2550"
    assert region.find("pwg:Height", NS).text == "3300"


def test_scan_settings_auto_size_omits_region():
    root = ET.fromstring(ScanSettings(size="auto").to_xml())
    assert root.find("pwg:ScanRegions", NS) is None


def test_jpeg_complete(tmp_path):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"\xff\xd8 data \xff\xd9")
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(b"\xff\xd8 data")
    assert jpeg_complete(good)
    assert not jpeg_complete(cut)
    assert not jpeg_complete(tmp_path / "missing.jpg")
