"""
Pyodide-facing glue for vpe.py.

Runs inside Pyodide (in-browser CPython), called from
cmsis-dap-webusb/src/vpe/pyodide-runtime.ts via pyodide.runPythonAsync(). Not
a TS port - this is Python that calls vpe.py's existing, unmodified classes
directly, so cmsis-dap-webusb's sound-editor step reuses the real codec
implementation rather than re-deriving it.

Every function here takes/returns plain paths and JSON-serializable values
(no vpe.py dataclass instances cross this boundary), using Pyodide's
in-memory FS (pyodide.FS.writeFile/readFile on the TS side) the same way the
CLI in vpe.py's own __main__ uses the real filesystem - see vpe.py's
`main()` for the equivalent path-based CLI workflow this mirrors.
"""

from __future__ import annotations

import os

from vpe import (
    ENCODING_PRESETS,
    AudioSegment,
    EncodingProfile,
    ISD9160Firmware,
    SirenEncoder,
)


def get_firmware_metadata(fw_path: str) -> dict:
    """Firmware-level metadata for display in the UI (not per-segment) -
    version string, segment count, checksum validity, total image size, and
    the AudioLibraryHeader's raw fw_version field."""
    fw = ISD9160Firmware.from_filepath(fw_path)
    return {
        "version": fw.version,
        "segmentCount": fw.segment_count,
        "checksumValid": fw.is_checksum_valid(),
        "sizeBytes": len(fw.data),
        "fwVersionRaw": fw.audiolib_header.fw_version,
    }


def extract_segments(fw_path: str, output_dir: str) -> list[dict]:
    """Extract every segment in the firmware at fw_path to WAV+raw files
    under output_dir (via ISD9160Firmware.extract_all). Returns per-segment
    metadata the UI needs to build a segment list."""
    fw = ISD9160Firmware.from_filepath(fw_path)
    fw.extract_all(output_dir)

    results = []
    for idx in range(fw.segment_count):
        seg = fw.get_segment(idx)
        codec_name = seg.codec.name
        wav_name = f"segment_{idx:02d}_{codec_name}.wav"
        raw_name = f"segment_{idx:02d}_{codec_name}.raw"
        wav_path = os.path.join(output_dir, wav_name)
        # extract_all() catches per-segment decode failures and simply skips
        # writing a usable WAV for that segment (see vpe.py's extract_all) -
        # report whether one actually landed rather than assuming it did.
        has_wav = os.path.exists(wav_path) and os.path.getsize(wav_path) > 0
        results.append(
            {
                "index": idx,
                "codec": codec_name,
                "wavFile": wav_name if has_wav else None,
                "rawFile": raw_name,
            }
        )
    return results


def encode_wav_into_segment(fw_path: str, wav_path: str, profile_name: str = "32 kHz / 48 kbps Siren14") -> bytes:
    """Encode wav_path into a Siren-compressed segment using the lookup
    tables embedded in the firmware at fw_path, returning raw segment bytes
    ready to be handed to patch_firmware()'s segment_updates."""
    if profile_name not in ENCODING_PRESETS:
        raise ValueError(f"Unknown encoding profile {profile_name!r}. Available: {list(ENCODING_PRESETS)}")
    profile: EncodingProfile = ENCODING_PRESETS[profile_name]

    fw = ISD9160Firmware.from_filepath(fw_path)
    encoder = SirenEncoder(fw.data)
    segment = encoder.encode_wav_into_audio_segment(wav_path, profile)
    return segment.data


def patch_firmware(fw_path: str, segment_paths: dict[int, str], version_str: str, output_path: str) -> None:
    """Replace the given segment indices with the raw segment bytes found at
    segment_paths[index] (a path, not inline bytes - avoids the caller
    having to embed potentially large binary payloads as Python source text
    when driving this through pyodide.runPythonAsync()), set a new version
    string, rehash, and write the patched firmware to output_path.

    NOTE: writes output_path itself rather than returning ISD9160Firmware.data
    directly, and deliberately does not return the ISD9160Firmware object
    patch_with_new_segments() produces - its `.version`/`.seg_entries` stay
    stale after patching (see this project's tests/test_vpe.py's documented
    gotcha), only `.data` is trustworthy. Read output_path back via a fresh
    ISD9160Firmware.from_filepath() call if you need to inspect the result
    afterwards (get_firmware_metadata() above does exactly that).
    """
    fw = ISD9160Firmware.from_filepath(fw_path)
    new_segments = fw.get_all_segments()

    for index, seg_path in segment_paths.items():
        idx = int(index)
        if idx < 0 or idx >= len(new_segments):
            raise IndexError(f"Segment index {idx} out of range (firmware has {len(new_segments)} segments)")
        with open(seg_path, "rb") as f:
            new_segments[idx] = AudioSegment(f.read())

    patched = fw.patch_with_new_segments(new_segments, version_str)

    with open(output_path, "wb") as f:
        f.write(patched.data)
