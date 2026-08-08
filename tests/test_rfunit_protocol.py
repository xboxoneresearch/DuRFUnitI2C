"""
Protocol-framing tests for rfunit.RfUnitI2C.

These pin down the exact wire format (command bytes, status-polling state
machine, chunking math) so the TypeScript port in cmsis-dap-webusb/src/i2c/
can be checked byte-for-byte against the same fixtures. The golden vectors
below (especially gen_challenge_response) are copied verbatim into
src/i2c/rfunit-i2c.test.ts - if you change rfunit.py's protocol, update both.
"""

import struct

import pytest

import rfunit


class RecordingI2CClient(rfunit.I2CClient):
    """Fake transport that records every write/transmit call and returns
    pre-programmed responses, queued in call order. Falls back to
    zero-filled responses once the queue is exhausted."""

    def __init__(self, read_responses=None, transmit_responses=None):
        self.writes: list[list[int]] = []
        self.transmits: list[tuple[list[int], int]] = []
        self.read_lens: list[int] = []
        self._read_responses = list(read_responses or [])
        self._transmit_responses = list(transmit_responses or [])

    def scan(self):
        return [rfunit.I2C_ADDR]

    def read(self, read_len: int):
        self.read_lens.append(read_len)
        if self._read_responses:
            return self._read_responses.pop(0)
        return [0] * read_len

    def write(self, data):
        self.writes.append(list(data))

    def transmit(self, data, read_len: int):
        self.transmits.append((list(data), read_len))
        if self._transmit_responses:
            return self._transmit_responses.pop(0)
        return [0] * read_len


# ---------------------------------------------------------------------------
# gen_challenge_response golden vectors
# ---------------------------------------------------------------------------

CHALLENGE_RESPONSE_VECTORS = [
    ([0, 0, 0, 0], [0x85, 0x44, 0xE5, 0x2E]),
    ([3, 17, 250, 8], [0xC5, 0x53, 0x6F, 0x44]),
    ([255, 255, 255, 255], [0x15, 0x2A, 0x0F, 0x3A]),
    ([1, 2, 3, 4], [0x05, 0x1E, 0x40, 0x36]),
]


@pytest.mark.parametrize("challenge,expected", CHALLENGE_RESPONSE_VECTORS)
def test_gen_challenge_response_golden_vectors(challenge, expected):
    assert rfunit.gen_challenge_response(challenge) == expected


def test_gen_challenge_response_applies_modulo_11_to_each_byte():
    # 11 (0xB) and 22 both reduce to 0 mod 11 - response must be identical.
    assert rfunit.gen_challenge_response([11, 11, 11, 11]) == rfunit.gen_challenge_response([22, 22, 22, 22])
    assert rfunit.gen_challenge_response([0, 0, 0, 0]) == rfunit.gen_challenge_response([11, 22, 33, 44])


# ---------------------------------------------------------------------------
# Command byte-framing
# ---------------------------------------------------------------------------


def test_read_register_frames_command_and_slices_status_bytes():
    dev = RecordingI2CClient(transmit_responses=[[0xAA, 0xBB, 1, 2, 3, 4]])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    result = rf.read_register(0x0C)

    assert dev.transmits == [([rfunit.CMD_REG_READ_xC1, 0x0C], 6)]
    assert result == [1, 2, 3, 4]


def test_write_register_frames_command_register_and_data():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    rf.write_register(0x0C, [0x01, 0x02])

    assert dev.writes == [[rfunit.CMD_REG_WRITE_x48, 0x0C, 0x01, 0x02]]


def test_read_data_frames_address_as_little_endian_u32_and_slices_response():
    dev = RecordingI2CClient(transmit_responses=[[0xAA, 0xBB, 1, 2, 3, 4, 5, 6, 0xFF]])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    result = rf.read_data(0x00001234)

    expected_cmd = [rfunit.CMD_FLASH_READ_xC3, *struct.pack("<I", 0x00001234)]
    assert dev.transmits == [(expected_cmd, 8)]
    # read_data returns a bytes slice (rfunit.py:349), not a list.
    assert result == bytes([1, 2, 3, 4, 5, 6])


def test_erase_flash_frames_addr_and_count_as_little_endian_u32(monkeypatch):
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    monkeypatch.setattr(rf, "wait_busy", lambda: True)

    rf.erase_flash(0x1000, 0x24400)

    expected = [
        rfunit.CMD_FLASH_ERASE_x95,
        *struct.pack("<I", 0x1000),
        *struct.pack("<I", 0x24400),
    ]
    assert dev.writes == [expected]


def test_write_flash_sets_address_then_writes_data(monkeypatch):
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    monkeypatch.setattr(rf, "wait_busy", lambda: True)

    rf.write_flash(0x2000, b"\x01\x02\x03")

    assert dev.writes == [
        [rfunit.CMD_FLASH_SET_WRITE_ADDR_x9B, *struct.pack("<I", 0x2000)],
        [rfunit.CMD_FLASH_WRITE_x9A, 1, 2, 3],
    ]


def test_write_flash_aborts_before_data_write_if_set_address_wait_busy_fails(monkeypatch):
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    monkeypatch.setattr(rf, "wait_busy", lambda: False)

    result = rf.write_flash(0x2000, b"\x01\x02\x03")

    assert result is False
    # Only the "set address" write happened - not the data write.
    assert len(dev.writes) == 1


def test_boot_to_ldrom_sends_challenge_response_command(monkeypatch):
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    monkeypatch.setattr(rf, "get_timer_value", lambda: [0, 0, 0, 0])
    monkeypatch.setattr(rf, "init", lambda: None)
    monkeypatch.setattr(rf, "stop", lambda: None)
    monkeypatch.setattr(rf, "wait_for_status", lambda target: True)

    # boot_to_ldrom busy-waits on a real 10-second time.time()-based clock
    # (rfunit.py:378-381 - needed on micropython to avoid a serial timeout).
    # Fake an advancing clock so the test doesn't block for 10 real seconds
    # (or hang forever if time.time() were naively frozen).
    fake_clock = [0.0]
    monkeypatch.setattr(rfunit.time, "time", lambda: fake_clock[0])
    monkeypatch.setattr(rfunit.time, "sleep", lambda _s: fake_clock.__setitem__(0, fake_clock[0] + 1))

    result = rf.boot_to_ldrom()

    expected = [rfunit.CMD_BOOT_LDROM_x4B, *rfunit.gen_challenge_response([0, 0, 0, 0])]
    assert dev.writes == [expected]
    assert result is True


# ---------------------------------------------------------------------------
# wait_busy / wait_for_status state machine
# ---------------------------------------------------------------------------


def _status_client(statuses):
    """A RecordingI2CClient whose read(2) calls step through `statuses`
    (each a full u16, little-endian-packed on read like the real status
    register), holding the last value once exhausted."""

    class _StatusClient(RecordingI2CClient):
        def read(self, read_len: int):
            self.read_lens.append(read_len)
            value = statuses[min(len(self.read_lens) - 1, len(statuses) - 1)]
            return list(struct.pack("<H", value))

    return _StatusClient()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(rfunit.time, "sleep", lambda _s: None)


def test_wait_busy_returns_true_once_status_leaves_busy():
    dev = _status_client([rfunit.STATUS_BUSY, rfunit.STATUS_BUSY, rfunit.STATUS_READY])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    assert rf.wait_busy() is True


def test_wait_busy_returns_false_on_error_outside_ldrom():
    dev = _status_client([rfunit.STATUS_ERROR])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    rf.read_error_string = lambda: b"boom"

    assert rf.wait_busy() is False


def test_wait_busy_ignores_error_flag_while_in_ldrom():
    # STATUS_LDROM (0x0C) has the STATUS_ERROR (0x04) bit set too - the
    # real firmware always reports "error" while in LDROM, so wait_busy
    # must NOT treat this as a failure (rfunit.py's documented special case).
    dev = _status_client([rfunit.STATUS_LDROM])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    assert rf.wait_busy() is True


def test_wait_busy_passes_through_boot_ldrom_in_progress_then_succeeds():
    dev = _status_client(
        [rfunit.STATUS_BOOT_LDROM_IN_PROGRESS, rfunit.STATUS_BOOT_LDROM_IN_PROGRESS, rfunit.STATUS_READY]
    )
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    assert rf.wait_busy() is True


def test_wait_for_status_returns_true_when_target_bit_is_set():
    dev = _status_client([rfunit.STATUS_BUSY, rfunit.STATUS_LDROM])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    assert rf.wait_for_status(rfunit.STATUS_LDROM) is True


def test_wait_for_status_returns_false_on_unknown_status():
    dev = _status_client([0x40])  # not busy, not error, not the boot-in-progress sentinel
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)

    assert rf.wait_for_status(rfunit.STATUS_READY) is False


def test_wait_for_status_returns_false_on_error_outside_ldrom():
    dev = _status_client([rfunit.STATUS_ERROR])
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    rf.read_error_string = lambda: b"boom"

    assert rf.wait_for_status(rfunit.STATUS_READY) is False


# ---------------------------------------------------------------------------
# dump_flash chunking
# ---------------------------------------------------------------------------


def test_dump_flash_yields_six_byte_chunks():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    rf.read_data = lambda addr: [addr & 0xFF] * 6

    chunks = list(rf.dump_flash(0, 18))

    assert len(chunks) == 3
    assert all(len(c) == 6 for c in chunks)


def test_dump_flash_truncates_final_partial_chunk():
    dev = RecordingI2CClient()
    rf = rfunit.RfUnitI2C(dev, logger=lambda *_: None)
    rf.read_data = lambda addr: [1, 2, 3, 4, 5, 6]

    # 20 bytes total: two full 6-byte chunks + one 6-byte chunk truncated to 2.
    chunks = list(rf.dump_flash(0, 14))

    assert [len(c) for c in chunks] == [6, 6, 2]
    assert chunks[-1] == [1, 2]
