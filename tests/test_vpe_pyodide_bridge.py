"""
Tests for vpe_pyodide_bridge.py - the path-in/path-out glue the TS side
(src/vpe/pyodide-runtime.ts) calls inside Pyodide. Runs as plain CPython
here (not inside Pyodide itself - that's only exercisable manually in a
browser, see the plan's build-order notes), verifying the bridge's own
JSON-shape contract and error handling against the same synthetic firmware
fixture used in test_vpe.py.

encode_wav_into_segment() is not covered here: SirenEncoder needs the real
Huffman/DCT lookup tables embedded in an actual firmware dump (see
test_vpe.py's module docstring) which the synthetic fixture doesn't have.
"""

import pytest

import vpe_pyodide_bridge as bridge
from test_vpe import SEG_DATA, VERSION_STR, _build_synthetic_firmware


@pytest.fixture
def fw_path(tmp_path):
    path = tmp_path / "fw.bin"
    path.write_bytes(_build_synthetic_firmware())
    return str(path)


def test_extract_segments_returns_per_segment_metadata(fw_path, tmp_path):
    output_dir = str(tmp_path / "out")

    result = bridge.extract_segments(fw_path, output_dir)

    assert result == [
        {"index": 0, "codec": "UNKNOWN", "wavFile": None, "rawFile": "segment_00_UNKNOWN.raw"}
    ]


def test_extract_segments_actually_writes_the_raw_file(fw_path, tmp_path):
    output_dir = str(tmp_path / "out")

    bridge.extract_segments(fw_path, output_dir)

    raw_path = tmp_path / "out" / "segment_00_UNKNOWN.raw"
    assert raw_path.read_bytes() == SEG_DATA


def test_patch_firmware_writes_a_reloadable_patched_image(fw_path, tmp_path):
    output_path = str(tmp_path / "patched.bin")
    replacement = bytes([0x00, 1, 2, 3, 4])
    seg_path = tmp_path / "segment0.raw"
    seg_path.write_bytes(replacement)

    bridge.patch_firmware(fw_path, {0: str(seg_path)}, "9.9.9-bridge-test", output_path)

    from vpe import ISD9160Firmware

    reloaded = ISD9160Firmware.from_filepath(output_path)
    assert reloaded.is_checksum_valid()
    assert reloaded.version == "9.9.9-bridge-test"
    assert reloaded.get_segment(0).data == replacement


def test_patch_firmware_rejects_an_out_of_range_segment_index(fw_path, tmp_path):
    output_path = str(tmp_path / "patched.bin")
    seg_path = tmp_path / "segment0.raw"
    seg_path.write_bytes(b"\x00")

    with pytest.raises(IndexError):
        bridge.patch_firmware(fw_path, {5: str(seg_path)}, VERSION_STR, output_path)


def test_encode_wav_into_segment_rejects_an_unknown_profile(fw_path, tmp_path):
    with pytest.raises(ValueError, match="Unknown encoding profile"):
        bridge.encode_wav_into_segment(fw_path, str(tmp_path / "in.wav"), profile_name="not-a-real-profile")
