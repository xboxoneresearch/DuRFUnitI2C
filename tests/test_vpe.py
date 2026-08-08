"""
Container-format tests for vpe.py's ISD9160Firmware (headers, TOC, checksum,
segment injection/patching).

This is a Pyodide-integration safety net, NOT a DSP-correctness suite: the
Siren/DPCM codec's decode tables are read from fixed addresses inside a real
firmware dump (reverse-engineered ROM data we can't synthesize), so encode/
decode accuracy is out of scope here - see notes/General.md. What IS in
scope, and fully synthesizable via vpe.py's own dataclass serializers, is the
VPE header/audio-library-header/TOC/checksum container format that
extract_all()/patch_with_new_segments() (the functions the Pyodide bridge
calls) depend on.
"""

import struct

import pytest
from fastcrc import crc16

import vpe

# ---------------------------------------------------------------------------
# Synthetic firmware fixture
#
# Real firmware embeds decoder lookup tables (Huffman trees, DCT matrices,
# etc.) at fixed absolute addresses up to ~0xA738 - SirenDecoder/DPCMDecoder
# read them unconditionally at construction time (FirmwareDecoderContext),
# even for segments that don't use that codec. So the buffer must be at
# least real-firmware-sized (FLASH_SIZE, 0x24400) even though only the
# header/TOC/segment/version region below is meaningful; the rest is 0xFF
# filler, matching an erased-flash idle value.
# ---------------------------------------------------------------------------

LIB_OFFSET = 0x8020
TOC_OFFSET = LIB_OFFSET + 88  # right after the 88-byte AudioLibraryHeader
SEG_START = TOC_OFFSET + 8  # right after 1 LibrarySegEntry (8 bytes)
# First byte's low 5 bits == 0 -> AudioSegment.codec == Codec.UNKNOWN, which
# FirmwareDecoderContext.decode_segment() handles as a safe no-op ([], 0)
# rather than dispatching into the Siren/DPCM decoders (which would need the
# real lookup tables to produce anything meaningful).
SEG_DATA = bytes([0x00]) + bytes(range(1, 16))
SEG_END = SEG_START + len(SEG_DATA) - 1
AUDIO_END = SEG_END + 1
VERSION_STR = "1.0.0-test"


def _build_synthetic_firmware(version_str: str = VERSION_STR, seg_data: bytes = SEG_DATA) -> bytes:
    seg_end = SEG_START + len(seg_data) - 1
    audio_end = seg_end + 1
    version_bytes = version_str.encode("utf-8") + b"\x00"
    vpe_end = vpe.ALIGN(audio_end + len(version_bytes) + vpe.CRC16_LEN)
    vpe_start = vpe.VPE_HEADER_ADDR
    crc_offset = vpe_end - vpe.CRC16_LEN
    total_size = 0x24400
    assert total_size > vpe_end

    buf = bytearray(b"\xff" * total_size)

    header = vpe.VpeHeader(
        magic=vpe.VPE_MAGIC,
        unknown=0,
        library_offset=LIB_OFFSET,
        vpe_end_offset=vpe_end,
        funcptr_init=0,
        funcptr_setup=0,
        funcptr_decode=0,
    )
    buf[vpe.VPE_HEADER_ADDR : vpe.VPE_HEADER_ADDR + len(header)] = header.to_bytes()

    audiolib_header = vpe.AudioLibraryHeader(
        magic=vpe.VPE_AUDIOLIB_MAGIC,
        unknown1=0,
        unknown2=0,
        audio_toc_offset=TOC_OFFSET,
        segment_count=1,
        unknown3=0,
        vpe_end_offset=vpe_end,
        unknown4=0,
        unknown5=0,
        unknown6=0,
        unknown7=0,
        audio_end_offset=audio_end,
        vpe_start_offset=vpe_start,
        unknown8=0,
        fw_version=1,
        meta_unk1=0,
        meta_unk2=0,
        meta_unk3=0,
        meta_unk4=0,
        meta_unk5=0,
        meta_unk6=0,
        meta_unk7=0,
        meta_unk8=0,
    )
    buf[LIB_OFFSET : LIB_OFFSET + len(audiolib_header)] = audiolib_header.to_bytes()

    entry = vpe.LibrarySegEntry(SEG_START, seg_end)
    buf[TOC_OFFSET : TOC_OFFSET + len(entry)] = entry.to_bytes()

    buf[SEG_START : seg_end + 1] = seg_data
    buf[audio_end : audio_end + len(version_bytes)] = version_bytes

    checksum = crc16.ibm_3740(bytes(buf[vpe_start:crc_offset]))
    struct.pack_into(">H", buf, crc_offset, checksum)

    return bytes(buf)


@pytest.fixture
def synthetic_firmware_bytes() -> bytes:
    return _build_synthetic_firmware()


@pytest.fixture
def firmware(synthetic_firmware_bytes: bytes) -> "vpe.ISD9160Firmware":
    return vpe.ISD9160Firmware(synthetic_firmware_bytes)


def test_synthetic_firmware_parses_and_validates(firmware: "vpe.ISD9160Firmware"):
    assert firmware.version == VERSION_STR
    assert firmware.is_checksum_valid()
    assert firmware.segment_count == 1
    assert len(firmware.seg_entries) == 1


def test_from_filepath_round_trips_through_disk(tmp_path, synthetic_firmware_bytes: bytes):
    path = tmp_path / "synthetic_fw.bin"
    path.write_bytes(synthetic_firmware_bytes)

    firmware = vpe.ISD9160Firmware.from_filepath(str(path))

    assert firmware.is_checksum_valid()
    assert firmware.version == VERSION_STR


def test_constructor_rejects_a_corrupted_checksum(synthetic_firmware_bytes: bytes):
    corrupted = bytearray(synthetic_firmware_bytes)
    # Flip a segment-data byte: covered by the checksum region, but not part
    # of the magic/TOC fields _parse() validates before reaching the
    # checksum check (flipping those would raise a different error first).
    corrupted[SEG_START] ^= 0xFF

    with pytest.raises(Exception, match="checksum"):
        vpe.ISD9160Firmware(bytes(corrupted))


def test_rehash_fixes_a_checksum_after_a_direct_data_edit(firmware: "vpe.ISD9160Firmware"):
    # Simulate editing raw segment bytes without going through inject_new_segments
    # (which would already rehash) - poke a byte directly, as e.g. a raw hex
    # editor workflow would, then confirm rehash() restores validity.
    tmp = bytearray(firmware.data)
    tmp[SEG_START] = 0x01
    firmware.data = bytes(tmp)
    firmware._invalidate_cache()

    assert not firmware.is_checksum_valid()
    firmware.rehash()
    assert firmware.is_checksum_valid()


def test_extract_all_writes_raw_for_every_segment_even_when_undecodable(firmware: "vpe.ISD9160Firmware", tmp_path):
    # Our synthetic segment uses the UNKNOWN codec (real decode tables aren't
    # present) - extract_all must still succeed overall and always write the
    # raw segment bytes, even though WAV decoding for this segment can't
    # produce real audio (matches the codebase's own resilience: decode
    # failures are caught and logged per-segment, not fatal).
    firmware.extract_all(str(tmp_path))

    raw_files = sorted(p.name for p in tmp_path.glob("*.raw"))
    assert raw_files == ["segment_00_UNKNOWN.raw"]
    assert (tmp_path / "segment_00_UNKNOWN.raw").read_bytes() == SEG_DATA


def test_patch_with_new_segments_produces_a_checksum_valid_reparseable_firmware(firmware: "vpe.ISD9160Firmware"):
    new_segments = firmware.get_all_segments()
    replacement = bytes([0x00]) + bytes(range(20, 20 + 15))
    new_segments[0] = vpe.AudioSegment(replacement)

    patched = firmware.patch_with_new_segments(new_segments, "2.0.0-patched")

    assert patched.is_checksum_valid()

    # Known gotcha (not something this test suite should paper over): after
    # patch_with_new_segments(), several fields on the *returned* object
    # itself are stale - inject_new_segments()/set_version_string() call
    # _invalidate_cache() (clearing _vpe_header/_audiolib_header/seg_entries)
    # but nothing re-triggers _parse() on `patched` itself, and `.version` is
    # a plain attribute only ever set inside _parse(), never refreshed by
    # _invalidate_cache() at all. Only the throwaway verification instance
    # `ISD9160Firmware(fw_copy.data)` inside patch_with_new_segments() gets a
    # fresh, accurate `.version`/`.seg_entries`. `patched.data` (the raw
    # bytes) is correct; `patched`'s cached Python attributes are not.
    # Re-wrapping, exactly like vpe.py's own internal verification does, is
    # required before reading segments or version back - the Pyodide bridge
    # must do the same rather than trusting the object patch_with_new_segments() returns.
    assert patched.seg_entries == []
    assert patched.version == VERSION_STR  # stale - NOT "2.0.0-patched"

    reloaded = vpe.ISD9160Firmware(patched.data)
    assert reloaded.version == "2.0.0-patched"
    assert reloaded.get_segment(0).data == replacement


def test_patch_with_new_segments_preserves_segment_count(firmware: "vpe.ISD9160Firmware"):
    new_segments = firmware.get_all_segments()
    patched = firmware.patch_with_new_segments(new_segments, firmware.version)

    reloaded = vpe.ISD9160Firmware(patched.data)
    assert reloaded.segment_count == firmware.segment_count
