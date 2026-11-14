"""Framing tests for VPE external-play streaming (outer 0x83/0x84).

Covers the RAM-only playback path found in the 920.000c firmware:
outer 0x83 stages the 12-byte VPE init header and triggers inner
0xAC, outer 0x84 feeds frame data. All fixtures are pure Python
(no hardware needed).
"""

import pytest

import rfunit
from test_rfunit_protocol import RecordingI2CClient


def test_mailbox_packet_layout():
    header12 = list(range(12))
    pkt = rfunit.build_mailbox_packet(rfunit.CMD_EXTERNAL_PLAY_x83, 0x00, header12)
    assert len(pkt) == rfunit.MAILBOX_LEN
    assert pkt[0] == rfunit.CMD_EXTERNAL_PLAY_x83
    assert pkt[1] == 0x00
    assert pkt[2] == 0x00
    assert pkt[3] == 0x00
    assert pkt[4:16] == header12
    assert pkt[16:] == [0] * 10


def test_mailbox_packet_rejects_bad_header_len():
    with pytest.raises(ValueError):
        rfunit.build_mailbox_packet(rfunit.CMD_EXTERNAL_PLAY_x83, 0x00, [1, 2, 3])


def test_split_vpe_blob():
    # bitrate=48000, subtype=0, bits_per_frame=960 -> 120 bytes/frame
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    blob = bytes(header12 + [0xAA] * 120 + [0xBB] * 120)
    header, frames = rfunit.split_vpe_blob(blob)
    assert bytes(header) == bytes(header12)
    assert len(frames) == 2
    assert bytes(frames[0]) == bytes([0xAA] * 120)
    assert bytes(frames[1]) == bytes([0xBB] * 120)


def test_split_vpe_blob_rejects_truncated_frame():
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    with pytest.raises(ValueError):
        rfunit.split_vpe_blob(bytes(header12 + [0xAA] * 10))


def test_start_external_play_writes_gain_then_mailbox():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    rf.start_external_play(header12, gain=0x2000)
    # gain goes out as one sequential REG_WRITE starting at 0x20 (lo, hi)
    assert dev.writes[0] == [rfunit.CMD_REG_WRITE_x48, 0x20, 0x00, 0x20]
    # then the 0x83 mailbox packet carrying the header
    assert dev.writes[1][0] == rfunit.CMD_EXTERNAL_PLAY_x83
    assert dev.writes[1][4:16] == header12
    assert len(dev.writes[1]) == rfunit.MAILBOX_LEN


def test_feed_external_frame_chunks_to_22_byte_packets():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    n = rf.feed_external_frame(bytes([0x11] * 50), seq=0)
    assert n == 3  # 22 + 22 + 6
    assert all(w[0] == rfunit.CMD_EXTERNAL_DATA_x84 for w in dev.writes)
    assert dev.writes[0][1] == 0  # seq of first chunk
    assert len(dev.writes[0]) == rfunit.MAILBOX_LEN


def test_stream_audio_blob_sends_header_then_frames():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    blob = bytes(header12 + [0xCC] * 120)
    count = rf.stream_audio_blob(blob, gain=0x2000)
    assert count == 1
    assert dev.writes[0][0] == rfunit.CMD_REG_WRITE_x48
    assert dev.writes[1][0] == rfunit.CMD_EXTERNAL_PLAY_x83
    assert dev.writes[2][0] == rfunit.CMD_EXTERNAL_DATA_x84


def test_do_stream_returns_frame_count(tmp_path=None):
    import io
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    assert rfunit.do_stream(rf, io.BytesIO(bytes(header12 + [0xCC] * 120))) == 0
    assert dev.writes[1][0] == rfunit.CMD_EXTERNAL_PLAY_x83


def test_do_stream_rejects_bad_blob(capsys=None):
    import io
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    assert rfunit.do_stream(rf, io.BytesIO(b"short")) == 8
    assert dev.writes == []


def test_run_with_stream_file_skips_dump(tmp_path):
    import struct
    header12 = [0x80, 0xBB, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x03, 0x1C, 0x00, 0x80, 0x02]
    blob_path = tmp_path / "blob.bin"
    blob_path.write_bytes(bytes(header12 + [0xCC] * 120))
    ready = list(struct.pack("<H", rfunit.STATUS_READY))
    fw = [0x00, 0x00] + list(b"v1.0\x00") + [0x00] * 120
    vpe = [0x00, 0x00] + list(b"vpe\x00") + [0x00] * 120
    dev = RecordingI2CClient(read_responses=[ready, ready], transmit_responses=[fw, vpe])
    rc = rfunit.run(dev, stream_path=str(blob_path), gain=0x2000)
    assert rc == 0
    assert dev.writes[0] == [rfunit.CMD_REG_WRITE_x48, 0x0C, 0x01]
    assert any(w[0] == rfunit.CMD_EXTERNAL_PLAY_x83 for w in dev.writes)
