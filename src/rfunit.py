"""
Xbox One I2C RF Unit
"""

# NOTE: These typing hints in try/except-wrap are not available for micropython
try:
    from typing import List, Generator
    from io import BufferedReader, BufferedWriter
except:
    pass

import os
import sys
import struct
import time

I2C_ADDR = 0x5A
FLASH_SIZE = 0x24400 # 145KB

def hexdump(data: bytes) -> str:
    return "".join([f"{b:02x}" for b in data])

class I2CClient:
    """
    Base class to implement for other I2C clients
    """
    def scan(self) -> List[int]:
        raise NotImplementedError()

    def read(self, read_len: int) -> List[int]:
        raise NotImplementedError()

    def write(self, data: List[int]) -> None:
        raise NotImplementedError()

    def transmit(self, data: List[int], read_len: int) -> List[int]:
        raise NotImplementedError()


class GreatFetDevice(I2CClient):
    def __init__(self):
        import greatfet
        from greatfet.interfaces.i2c_bus import I2CBus
        from greatfet.interfaces.i2c_device import I2CDevice

        self.gf = greatfet.GreatFET()
        self.bus = I2CBus(self.gf)
        self.dev = I2CDevice(self.bus, I2C_ADDR)

    def scan(self) -> List[int]:
        return self.bus.scan()

    def read(self, read_len: int) -> List[int]:
        return self.dev.read(read_len)

    def write(self, data: List[int]) -> None:
        self.dev.write(data)

    def transmit(self, data: List[int], read_len: int) -> List[int]:
        return self.dev.transmit(data, read_len)


class RPiDevice(I2CClient):
    def __init__(self, bus_id: int = 1):
        if os.name == "nt":
            raise RuntimeError(
                "RPiDevice is only supported on Linux (smbus2 requires fcntl/ioctl + /dev/i2c-*). "
                "Run this on the Raspberry Pi, or use the GreatFET backend on Windows."
            )
        # Use I2C1 by default, I2C0 is reserved for HAT EEPROM
        from smbus2 import SMBus
        self.bus = SMBus(bus_id)

    def scan(self) -> List[int]:
        # smbus2 does not support scanning
        # use i2cdetect from CLI instead

        # Returning the expected result here anyway
        return [I2C_ADDR]

    def read(self, read_len: int) -> List[int]:
        resp = []
        for _ in range(0, read_len):
            resp.append(self.bus.read_byte(I2C_ADDR))
        return resp

    def write(self, data: List[int]) -> None:
        for b in data:
            self.bus.write_byte(I2C_ADDR, b)

    def transmit(self, data: List[int], read_len: int) -> List[int]:
        self.write(data)
        return self.read(read_len)


class MicropythonDevice(I2CClient):
    def __init__(self, i2c_dev):
        self.dev = i2c_dev

    def scan(self) -> List[int]:
        return self.dev.scan()

    def read(self, read_len: int) -> List[int]:
        return list(self.dev.readfrom(I2C_ADDR, read_len))

    def write(self, data: List[int]) -> None:
        self.dev.writeto(I2C_ADDR, bytes(data))

    def transmit(self, data: List[int], read_len: int) -> List[int]:
        self.write(data)
        return self.read(read_len)


class DummyDevice(I2CClient):
    def __init__(self):
        pass

    def scan(self) -> List[int]:
        return [I2C_ADDR]

    def read(self, read_len: int) -> List[int]:
        print(f"read ({read_len=})")
        return list(b"\x00" * read_len)

    def write(self, data: List[int]) -> None:
        print(f"write ({data=})")

    def transmit(self, data: List[int], read_len: int) -> List[int]:
        print(f"transmit ({data=}, {read_len=})")
        return list(b"\x00" * read_len)

"""
Commands
"""

CMD_INTERRUPT_READ_xC0 = 0xC0
CMD_BOOT_APROM_x1B = 0x1B
CMD_REG_WRITE_x48 = 0x48
CMD_BOOT_LDROM_x4B = 0x4B
CMD_REG_READ_xC1 = 0xC1
CMD_FW_VERSION_xC2 = 0xC2
CMD_FLASH_READ_xC3 = 0xC3
CMD_VPE_VERSION_xC4 = 0xC4
CMD_ERROR_STRING_xC5 = 0xC5
CMD_GET_TIMERVAL_xC9 = 0xC9

CMD_START_x81 = 0x81
CMD_BOOT_REPAIR_x8B = 0x8B
CMD_EXTERNAL_PLAY_x83 = 0x83
CMD_EXTERNAL_DATA_x84 = 0x84
CMD_TRANSFER_x11 = 0x11
CMD_STOP_x02 = 0x02
CMD_FLASH_ERASE_x95 = 0x95
CMD_FLASH_WRITE_x9A = 0x9A
CMD_FLASH_SET_WRITE_ADDR_x9B = 0x9B
CMD_RESET_x4A = 0x4A

MAILBOX_LEN = 0x1A
VPE_INIT_LEN = 12
MAILBOX_DATA_CHUNK = MAILBOX_LEN - 4
EXT_GAIN_UNITY = 0x2000

AUDIO_CMD_VOLUME_xAA = 0xAA
AUDIO_CMD_PAUSE_xAB = 0xAB
AUDIO_CMD_EXTERNAL_xAC = 0xAC
AUDIO_CMD_PLAY_xAD = 0xAD
AUDIO_CMD_STOP_xAE = 0xAE

STATUS_READY = 0x80
STATUS_ERROR = 0x04
STATUS_LDROM = 0x0C
STATUS_BUSY = 0x10
STATUS_BOOT_LDROM_IN_PROGRESS = 0x88

"""
Registers

Register Index -> Offsets (each 1 byte length)

0x00 -> 0x20000f24
0x01 -> 0x20000f25
0x0c -> 0x20000f16
0x0d -> 0x20000f20 (Does modulo 16000 % $value$ and stores remainder in the register)
0x0e -> 0x20000f22 (If $value$ is 0, store 1)
0x0f -> 0x20000f23
0x11 -> 0x20000f21
0x16 -> 0x20000f26
0x17 -> 0x20000f27
0x18 -> 0x20000f2e
0x19 -> 0x20000f2f
0x1a -> 0x20000f19
0x1c -> 0x20000f30
0x21 -> 0x20000f32
0x23 ->0x20000f2c
0x28 -> 0x20000f40
0x29 -> 0x20000f41
0x2b ->0x20000f42

Extra registers (from jump table at 0x1A42):
0x1f -> 0x20000f14+0x14 (mode)
0x20 -> low byte temp, 0x21 combines to u16 at 0x20000f14+0x1e (gain)
0x22 -> low byte temp, 0x23 combines to u16 at 0x20000f14+0x18
0x28 -> 0x20000f14+0x0c (clamped 4..0x14)
0x2a -> low byte temp (pairs with 0x2b to u16 at +0x2e)
"""

# Firmware-internal SRAM addresses (920.000c). Host cannot touch
# these directly, they document what outer 0x83/0x84 prime.
EXT_HEADER_ADDR = 0x20002968
EXT_FRAME_ADDR = 0x200028F0
EXT_PCM_ADDR = 0x20000400


def build_mailbox_packet(cmd: int, param: int, header12) -> List[int]:
    # Class-2 packet is 26 bytes. Handler at 0x58F2 copies
    # bytes 4:16 (3 words) to EXT_HEADER_ADDR.
    header = list(header12)
    if len(header) != VPE_INIT_LEN:
        raise ValueError(f"need {VPE_INIT_LEN} header bytes, got {len(header)}")
    pkt = [cmd & 0xFF, param & 0xFF, 0x00, 0x00]
    pkt.extend([b & 0xFF for b in header])
    pkt.extend([0x00] * (MAILBOX_LEN - 4 - VPE_INIT_LEN))
    return pkt


def split_vpe_blob(blob) -> tuple:
    # Blob is 12-byte VPE init followed by N frames of
    # bits_per_frame//8 bytes (init+6 is u16le per Segment_Size_Research).
    raw = bytes(blob)
    if len(raw) < VPE_INIT_LEN:
        raise ValueError(f"blob too short: {len(raw)}")
    header = raw[0:VPE_INIT_LEN]
    bits_per_frame = struct.unpack_from("<H", raw, 6)[0]
    if bits_per_frame == 0 or bits_per_frame % 8 != 0:
        raise ValueError(f"bad bits_per_frame: {bits_per_frame}")
    frame_len = bits_per_frame // 8
    rest = raw[VPE_INIT_LEN:]
    if len(rest) == 0 or len(rest) % frame_len != 0:
        raise ValueError(f"truncated frames: {len(rest)} not a multiple of {frame_len}")
    frames = [rest[i:i + frame_len] for i in range(0, len(rest), frame_len)]
    return (header, frames)


class Sound:
    POWERON = 0x00
    BING = 0x01
    POWEROFF = 0x02

    DISC_DRIVE_1 = 0x03
    DISC_DRIVE_2 = 0x04
    DISC_DRIVE_3 = 0x05

    PLOPP = 0x06
    NO_DISC = 0x07
    PLOPP_LOUDER = 0x08

def gen_challenge_response(challenge: List[int]) -> List[int]:
    MAGIC_VAL_MUL = 0x219f5
    MAGIC_VAL_ADD = 0x1651

    b0 = challenge[0] % 0xB
    b1 = challenge[1] % 0xB
    b2 = challenge[2] % 0xB
    b3 = challenge[3] % 0xB

    res = MAGIC_VAL_MUL * ((b0 * 0x200) + (b1 * 0x80) + (b2 * 0x20) + (b3 * 0x8) + MAGIC_VAL_ADD)
    return list(struct.pack("<I", res))

class RfUnitI2C:
    def __init__(self, dev: I2CClient, logger=None):
        self.old_status = 0
        self.dev = dev
        self.log = logger if logger is not None else print

    def detect(self) -> bool:
        ids = self.dev.scan()
        self.log(f"Discovered devices: {ids}")
        return I2C_ADDR in ids

    def read_status(self) -> int:
        # Returns u16, first 8bits being the status byte
        return struct.unpack("<H", bytes(self.dev.read(2)))[0]

    def is_in_ldrom(self) -> bool:
        return self.read_status() & STATUS_LDROM == STATUS_LDROM

    def wait_busy(self) -> bool:
        sleep_count = 0
        while sleep_count < 50:
            try:
                status = self.read_status() & 0xFF
                if self.old_status != status:
                    # print(f"{status=:#02x}")
                    self.old_status = status

                if status & STATUS_BUSY == STATUS_BUSY:
                    sleep_count += 1
                    time.sleep(0.1)
                elif (status & STATUS_ERROR == STATUS_ERROR) and (status & STATUS_LDROM != STATUS_LDROM):
                    # Error flag seems always set in LDROM
                    self.log("wait_busy: Got error status!")
                    self.log(f"Error: {self.read_error_string()}")
                    return False
                elif status == STATUS_BOOT_LDROM_IN_PROGRESS:
                    pass
                else:
                    return True
            except OSError:
                pass
        
        self.log("wait_busy: Timeout")
        return False

    def wait_for_status(self, target_status: int) -> bool:
        sleep_count = 0
        while sleep_count < 50:
            try:
                status = self.read_status() & 0xFF
                if self.old_status != status:
                    # print(f"{status=:#02x}")
                    self.old_status = status

                if status & target_status == target_status:
                    return True
                elif (status & STATUS_ERROR == STATUS_ERROR) and (status & STATUS_LDROM != STATUS_LDROM):
                    # Error flag seems always set in LDROM
                    self.log("wait_for_status: Got error status!")
                    self.log(f"Error: {self.read_error_string()}")
                    return False
                elif status & STATUS_BUSY == STATUS_BUSY:
                    sleep_count += 1
                    time.sleep(0.5)
                elif status == STATUS_BOOT_LDROM_IN_PROGRESS:
                    pass
                else:
                    self.log(f"wait_for_status: Unknown status: {status:02x}")
                    return False
            except OSError:
                pass
        
        self.log("wait_for_status: Timeout")
        return False

    def _read_interrupt(self) -> List[int]:
        return self.dev.transmit([CMD_INTERRUPT_READ_xC0], 0)

    def read_register(self, register: int) -> List[int]:
        # Wiki notes 2 leading status bytes (so total response is 6 bytes), but some clients behave differently.
        res = self.dev.transmit([CMD_REG_READ_xC1, register], 6)
        if len(res) >= 6:
            return res[2:6]
        return res

    def _write_register(self, register: int, data: List[int]):
        write_data = [CMD_REG_WRITE_x48]
        write_data.append(register)
        write_data.extend(data)

        self.dev.write(write_data)

    def write_register(self, register: int, data: List[int]):
        self._write_register(register, data)

    def init(self):
        self._write_register(0x0C, [0x01])
        self._write_register(0x04, [0xFF, 0xFF])

    def stop(self):
        self.dev.write([CMD_STOP_x02])

    def read_fw_version(self) -> bytes:
        cmd_bytes = [CMD_FW_VERSION_xC2]
        # Send command and receive data
        data = bytes(self.dev.transmit(cmd_bytes, 128))
        # Skip 2 bytes and chop off data at first null-byte
        return data[2:data.index(b'\x00')]

    def read_vpe_version(self) -> bytes:
        cmd_bytes = [CMD_VPE_VERSION_xC4]
        # Send command and receive data
        data = bytes(self.dev.transmit(cmd_bytes, 128))
        # Skip 2 bytes and chop off data at first null-byte
        return data[2:data.index(b'\x00')]

    def read_error_string(self) -> bytes:
        cmd_bytes = [CMD_ERROR_STRING_xC5]
        # Send command and receive data
        data = bytes(self.dev.transmit(cmd_bytes, 128))
        # Skip 2 bytes and chop off data at first null-byte
        return data[2:data.index(b'\x00')]

    def read_data(self, addr: int) -> bytes:
        cmd_bytes = [CMD_FLASH_READ_xC3]
        # Convert address to bytes, U32-LE and append to cmd buffer
        cmd_bytes.extend(list(struct.pack("<I", addr)))
        # Send command and receive data (we get 9 bytes back)
        data = bytes(self.dev.transmit(cmd_bytes, 8))
        # Cut status bytes (discard first 2 bytes and cut off after 8th), yielding 6 bytes
        # GreatFET, when asked to receive 8 bytes, returns 9
        return data[2:8]

    def play_sound(self, num: int):
        self.dev.write([CMD_START_x81, num])

    def send_mailbox(self, cmd: int, param: int, header12) -> None:
        self.dev.write(build_mailbox_packet(cmd, param, header12))

    def set_external_gain(self, gain: int) -> None:
        # Sequential REG_WRITE starting at 0x20 hits 0x20 then
        # auto-increments into 0x21 which stores u16 gain at +0x1e.
        gain = gain & 0xFFFF
        self._write_register(0x20, [gain & 0xFF, (gain >> 8) & 0xFF])

    def start_external_play(self, header12, gain: int = EXT_GAIN_UNITY) -> None:
        # Outer 0x83 stages the header at EXT_HEADER_ADDR and
        # the 0x5800 task fires inner 0xAC (see 0x5950).
        self.set_external_gain(gain)
        self.send_mailbox(CMD_EXTERNAL_PLAY_x83, 0x00, header12)

    def feed_external_frame(self, frame, seq: int = 0) -> int:
        # One 0x84 packet carries up to 22 payload bytes.
        # Larger VPE frames need several packets.
        raw = bytes(frame)
        if len(raw) == 0:
            return 0
        count = 0
        for off in range(0, len(raw), MAILBOX_DATA_CHUNK):
            chunk = raw[off:off + MAILBOX_DATA_CHUNK]
            pkt = [(CMD_EXTERNAL_DATA_x84), ((seq + count) & 0xFF), 0x00, 0x00]
            pkt.extend(list(chunk))
            pkt.extend([0x00] * (MAILBOX_DATA_CHUNK - len(chunk)))
            self.dev.write(pkt)
            count += 1
        return count

    def stream_audio_blob(self, blob, gain: int = EXT_GAIN_UNITY) -> int:
        # Header first via 0x83, then each frame via 0x84 chunks.
        # Returns frame count. Handshake polling (+3/+5) still
        # needs HW confirmation, so no wait here.
        header, frames = split_vpe_blob(blob)
        self.start_external_play(list(header), gain)
        seq = 0
        for frame in frames:
            seq = (seq + self.feed_external_frame(frame, seq)) & 0xFF
        return len(frames)

    def reset(self):
        self.dev.write([CMD_RESET_x4A, 0x55, 0x01])

    def boot_repair(self):
        self.dev.write([CMD_BOOT_REPAIR_x8B, 1, 1, 1])
        return self.wait_for_status(STATUS_READY)

    def get_timer_value(self) -> List[int]:
        res = self.dev.transmit([CMD_GET_TIMERVAL_xC9], 6)
        # Cut status bytes
        return res[2:6]

    def boot_to_ldrom(self):
        WAIT_SECS = 10
        challenge = self.get_timer_value()
        response = gen_challenge_response(challenge)

        cmd_bytes = [CMD_BOOT_LDROM_x4B]
        cmd_bytes.extend(response)

        self.dev.write(cmd_bytes)

        self.log(f"Waiting for {WAIT_SECS} seconds...")
        start_time = time.time()
        # This loop is necessary for micropython to not fail due to timeout
        while (time.time() - start_time) < WAIT_SECS:
            sys.stdout.write("\n")
            time.sleep(1)
        self.log("Checking if LDROM was reached...")

        self.init()
        self.stop()
        return self.wait_for_status(STATUS_LDROM)

    def boot_to_aprom(self):
        self.dev.write([CMD_BOOT_APROM_x1B])
        time.sleep(4)
        return self.wait_for_status(STATUS_READY)

    def erase_flash(self, addr: int, count: int):
        cmd_bytes = [CMD_FLASH_ERASE_x95]
        # Convert address and count to bytes, U32-LE and append to cmd buffer
        cmd_bytes.extend(list(struct.pack("<I", addr)))
        cmd_bytes.extend(list(struct.pack("<I", count)))

        self.dev.write(cmd_bytes)
        return self.wait_busy()

    def write_flash(self, addr: int, data: bytes):
        # Set address
        cmd_bytes = [CMD_FLASH_SET_WRITE_ADDR_x9B]
        # Convert address to bytes, U32-LE and append to cmd buffer
        cmd_bytes.extend(list(struct.pack("<I", addr)))
        self.dev.write(cmd_bytes)

        res = self.wait_busy()
        if not res:
            return res

        # Write actual data
        cmd_bytes = [CMD_FLASH_WRITE_x9A]
        # Convert bytes to a list of ints and append to cmd buffer
        cmd_bytes.extend(list(data))
        self.dev.write(cmd_bytes)

        return self.wait_busy()

    def dump_flash(self, offset: int, count: int) -> Generator[bytes, None, None]:
        CHUNK_SIZE = 6
        # Read data in chunks
        end_offset = offset + count
        for addr in range(offset, end_offset, CHUNK_SIZE):
            res = self.read_data(addr)
            # Fixes reading trailing bytes on last read
            bytecnt = min(CHUNK_SIZE, end_offset - addr)
            yield res[:bytecnt]

def print_position(position: int, mod_value: int | None = None):
    # As we dont want to print on each iteration..
    if not mod_value or position % mod_value == 0:
        print(f"{position:#08x}")

def do_dump(dev: RfUnitI2C, f: BufferedWriter):
    pos = 0
    print("Dumping...")
    for chunk in dev.dump_flash(0, FLASH_SIZE):
        f.write(chunk)
        pos += len(chunk)
        # Chunk is 6 bytes
        print_position(pos, len(chunk) * 0x100)
    print_position(FLASH_SIZE)
    print("* Dumping flash finished")
    return 0

def do_flash(dev: RfUnitI2C, f: BufferedReader):
    if not dev.is_in_ldrom():
        print("Entering LDROM")
        res = dev.boot_to_ldrom()
        if not res:
            print("Failed entering LDROM")
            return 4

    print("* We are in LDROM now :)")

    print("Erasing...")
    res = dev.erase_flash(0x00, FLASH_SIZE)
    if not res:
        print("Failed erasing APROM")
        return 5
    print("* Erasing finished")

    print("Writing flash now...")
    WRITE_CHUNK_SZ = 0x80

    for addr in range(0, FLASH_SIZE, WRITE_CHUNK_SZ):
        data = f.read(WRITE_CHUNK_SZ)
        res = dev.write_flash(addr, data)
        if not res:
            print("Failed to write flash")
            return 6
        print_position(addr, WRITE_CHUNK_SZ * 10)
    print_position(FLASH_SIZE)
    print("* Writing flash finished")
    return 0

class Devices:
    GREATFET = "greatfet"
    RPI = "rpi"
    DUMMY = "dummy"

DUMP_FILENAME = "dump.bin"
FLASH_FILENAME = "flash.bin"

def get_filesize(path: str) -> int:
    SEEK_END = 2
    try:
        f = open(path, "rb")
        f.seek(0, SEEK_END)
        filesize = f.tell()
        f.close()
        return filesize
    except OSError:
        return 0

def do_stream(dev: RfUnitI2C, f: BufferedReader, gain: int = EXT_GAIN_UNITY) -> int:
    blob = f.read()
    try:
        frames = dev.stream_audio_blob(blob, gain)
    except ValueError as e:
        print(f"Bad audio blob: {e}")
        return 8
    print(f"* Streamed {frames} frames without flashing")
    return 0

def run(device: I2CClient, stream_path=None, gain: int = EXT_GAIN_UNITY) -> int:
    rfunit = RfUnitI2C(device)

    if not rfunit.detect():
        print("RF Unit was not detected!")
        return 1

    rfunit.init()
    rfunit.stop()

    if stream_path is not None:
        if rfunit.is_in_ldrom():
            print("Rebooting back into APROM now...")
            if not rfunit.boot_to_aprom():
                print("Failed booting back into APROM")
                return 7
            print("* Reboot to APROM finished")
        try:
            with open(stream_path, "rb") as f:
                return do_stream(rfunit, f, gain)
        except OSError:
            print(f"Cannot open stream file '{stream_path}'!")
            return 2

    if not rfunit.is_in_ldrom():
        fw_version = rfunit.read_fw_version()
        print(fw_version)
        vpe_version = rfunit.read_vpe_version()
        print(vpe_version)

    # Do action based on file existance
    # Expecting "flash.bin" for flashing
    # Otherwise, a dump is made
    flash_filesize = get_filesize(FLASH_FILENAME)
    if flash_filesize:
        print(f"Flashing file '{FLASH_FILENAME}'...")
        if flash_filesize != FLASH_SIZE:
            print(f"Expected flash filesize of {FLASH_SIZE:#08x}, got {flash_filesize:#08x}! Exiting!")
            return 2
        with open(FLASH_FILENAME, "rb") as f:
            ret = do_flash(rfunit, f)
            if ret != 0:
                print(f"Something went wrong, code: {ret}")
                return ret

    elif get_filesize(DUMP_FILENAME):
        # Dump already exists
        print(f"Dump '{DUMP_FILENAME}' already exists on device, not doing anything!")
        return 3
    else:
        print(f"Dumping to file '{DUMP_FILENAME}'...")
        with open(DUMP_FILENAME, "wb") as f:
            ret = do_dump(rfunit, f)
            if ret != 0:
                print(f"Something went wrong, code: {ret}")
                return ret

    if rfunit.is_in_ldrom():
        print("Rebooting back into APROM now...")
        if not rfunit.boot_to_aprom():
            print("Failed booting back into APROM")
            return 7
        print("* Reboot to APROM finished")

    rfunit.play_sound(Sound.BING)
    return 0

def main() -> int:
    if sys.implementation.name == "micropython":
        import machine
        if sys.platform == "rp2":
            # Pi Pico/2 - SDA: GP0, SCL: GP1
            i2c_if = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=50000)
        elif sys.platform == "esp8266":
            # ESP8266 - SDA: GPIO4, SCL: GPIO5
            i2c_if = machine.I2C(sda=machine.Pin(4), scl=machine.Pin(5), freq=50000)
        else:
            # Implement other platforms if needed
            raise NotImplementedError()

        device = MicropythonDevice(i2c_if)
    else:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("device", choices=["greatfet", "rpi"], help="Device type to use")
        parser.add_argument("--stream", metavar="VPE_BLOB", default=None,
                            help="Stream a raw VPE blob (12-byte init + frames) for RAM-only playback")
        parser.add_argument("--gain", default="0x2000",
                            help="Playback gain, hex or int (default 0x2000 unity)")
        args = parser.parse_args()

        if args.device == Devices.GREATFET:
            device = GreatFetDevice()
        elif args.device == Devices.RPI:
            device = RPiDevice()
        else:
            raise NotImplementedError()

        return run(device, stream_path=args.stream, gain=int(args.gain, 0))

    return run(device)

if __name__ == "__main__":
    sys.exit(main())
