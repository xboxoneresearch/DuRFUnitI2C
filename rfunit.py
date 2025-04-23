"""
Xbox One I2C RF Unit
"""

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

    def scan(self) -> bool:
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
CMD_STOP_x02 = 0x02
CMD_FLASH_ERASE_x95 = 0x95
CMD_FLASH_WRITE_x9A = 0x9A
CMD_FLASH_SET_WRITE_ADDR_x9B = 0x9B
CMD_RESET_x4A = 0x4A

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

Missing registers:
0x1f (PDMA related)
0x20
0x22
0x2a 
"""


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
    def __init__(self, dev: I2CClient):
        self.old_status = 0
        self.dev = dev

    def detect(self) -> bool:
        ids = self.dev.scan()
        print("Discovered devices: ", ids)
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
                    print("wait_busy: Got error status!")
                    print(f"Error: {self.read_error_string()}")
                    return False
                elif status == STATUS_BOOT_LDROM_IN_PROGRESS:
                    pass
                else:
                    return True
            except OSError:
                pass
        
        print("wait_busy: Timeout")
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
                    print("wait_for_status: Got error status!")
                    print(f"Error: {self.read_error_string()}")
                    return False
                elif status & STATUS_BUSY == STATUS_BUSY:
                    sleep_count += 1
                    time.sleep(0.5)
                elif status == STATUS_BOOT_LDROM_IN_PROGRESS:
                    pass
                else:
                    print(f"wait_for_status: Unknown status: {status:02x}")
                    return False
            except OSError:
                pass
        
        print("wait_for_status: Timeout")
        return False

    def _read_interrupt(self) -> List[int]:
        return self.dev.transmit([CMD_INTERRUPT_READ_xC0], 0)

    def read_register(self, register: int) -> List[int]:
        return self.dev.transmit([CMD_REG_READ_xC1, register], 4)

    def _write_register(self, register: int, data: List[int]):
        write_data = [CMD_REG_WRITE_x48]
        write_data.append(register)
        write_data.extend(data)

        self.dev.write(write_data)

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

        print(f"Waiting for {WAIT_SECS} seconds...")
        start_time = time.time()
        # This loop is necessary for micropython to not fail due to timeout
        while (time.time() - start_time) < WAIT_SECS:
            sys.stdout.write("\n")
            time.sleep(1)
        print("Checking if LDROM was reached...")

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

def print_position(position: int, mod_value: int = None):
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

def main(device: I2CClient) -> int:
    rfunit = RfUnitI2C(device)

    if not rfunit.detect():
        print("RF Unit was not detected!")
        return 1

    rfunit.init()
    rfunit.stop()

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

if __name__ == "__main__":
    device = None
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
        args = parser.parse_args()

        if args.device == Devices.GREATFET:
            device = GreatFetDevice()
        elif args.device == Devices.RPI:
            device = RPiDevice()
        else:
            raise NotImplementedError()

    sys.exit(main(device))
