"""
Xbox One I2C RF Unit
"""

try:
    from typing import List, Generator
except:
    pass
import sys
import struct

I2C_ADDR = 0x5A
FLASH_SIZE = 0x24400 # 145KB

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
CMD_REG_WRITE_x48 = 0x48
CMD_REG_READ_xC1 = 0xC1
CMD_FLASH_READ_xC3 = 0xC3

CMD_START_x81 = 0x81
CMD_STOP_x02 = 0x02
CMD_RESET_x4A = 0x4A


"""
Registers
"""

# R/W I2C Control Register
REG_CTL = 0x00
# R/W I2C Slave address Register0
REG_ADDR0 = 0x04
# R/W I2C DATA Register
REG_DAT = 0x08
# R I2C Status Register
REG_STATUS = 0x0C
# R/W I2C clock divided Register
REG_CLKDIV = 0x10
# R/W I2C Time out control Register
REG_TOCTL = 0x14
# R/W I2C Slave address Register1
REG_ADDR1 = 0x18
# R/W I2C Slave address Register2
REG_ADDR2 = 0x1C
# R/W I2C Slave address Register3
REG_ADDR3 = 0x20
# R/W I2C Slave address Mask Register0
REG_ADDRMSK0 = 0x24
# R/W I2C Slave address Mask Register1
REG_ADDRMSK1 = 0x28
# R/W I2C Slave address Mask Register2
REG_ADDRMSK2 = 0x2C
# R/W I2C Slave address Mask Register3
REG_ADDRMSK3 = 0x30

#class Sound(Enum):
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

class RfUnitI2C:
    def __init__(self, dev: I2CClient):
        self.dev = dev

    def detect(self) -> bool:
        ids = self.dev.scan()
        print("Discovered devices: ", ids)
        return I2C_ADDR in ids

    def _read_interrupt(self) -> List[int]:
        return self.dev.transmit([CMD_INTERRUPT_READ_xC0], 2)

    def read_register(self, register: int) -> List[int]:
        return self.dev.transmit([CMD_REG_READ_xC1, register], 4)

    def _write_register(self, register: int, data: List[int]):
        write_data = [CMD_REG_WRITE_x48]
        write_data.append(register)
        write_data.extend(data)

        self.dev.write(write_data)

    def init(self):
        self._write_register(REG_STATUS, [0x01])
        self._write_register(REG_ADDR0, [0xFF, 0xFF])

    def stop(self):
        self.dev.write([CMD_STOP_x02])

    def read_data(self, addr: int) -> bytes:
        cmd_bytes = [CMD_FLASH_READ_xC3]
        # Convert address to bytes, U32-LE and append to cmd buffer
        cmd_bytes.extend(list(struct.pack("<I", addr)))
        # Send command and receive data (we get 9 bytes back)
        data = bytes(self.dev.transmit(cmd_bytes, 8))
        # Cut ?status? bytes (discard first 2 bytes and cut off after 8th), yielding 6 bytes
        # GreatFET, when asked to receive 8 bytes, returns 9
        return data[2:8]

    def play_sound(self, num: int):
        self.dev.write([CMD_START_x81, num])

    def reset(self):
        self.dev.write([CMD_RESET_x4A, 0x55])

    def dump_flash(self, print_addrs: bool = False) -> Generator[bytes, None, None]:
        CHUNK_SIZE = 6
        # Read data in chunks
        for addr in range(0, FLASH_SIZE, CHUNK_SIZE):
            if print_addrs and (addr % (CHUNK_SIZE * 200)) == 0:
                percentage = (addr / FLASH_SIZE) * 100.0
                print(f"* 0x{addr:04X} ({percentage:8.2f} %)")
            res = self.read_data(addr)
            # Fixes reading trailing bytes on last read
            bytecnt = min(CHUNK_SIZE, FLASH_SIZE - addr)
            yield res[:bytecnt]

    def bruteforce_cmd(self) -> int | None:
        # POSSIBLE_DATA = b"ISD9160"
        # POSSIBLE_DATA = b"9160"
        # POSSIBLE_DATA = b"Nuvoton"
        # Reference: https://github.com/robbie-cao/piccolo/blob/cd7f55475db9e19090656238a3268f0576cc6651/SDK/CMSIS/CM0/DeviceSupport/Nuvoton/ISD91xx/boot_ISD9xx.c#L195
        POSSIBLE_DATA = b"\x00\x30\x00\x20"

        # Create list of all possible commands
        cmds_to_test = list(range(0, 0xFF))

        # Remove known cmds
        for known_cmd in [
            CMD_RESET_x4A,
            CMD_REG_WRITE_x48,
            CMD_INTERRUPT_READ_xC0,
            CMD_REG_READ_xC1,
            CMD_START_x81,
            CMD_STOP_x02
        ]:
            cmds_to_test.remove(known_cmd)

        for cmd in cmds_to_test:
            print(f"Current CMD: 0x{cmd:02X}")
            cmd_buf = [cmd]
            # Assume address is packed as LE U32, read from addr 0
            addr_bytes = struct.pack("<I", 0x0)
            # Append address to cmd
            cmd_buf.extend(list(addr_bytes))
            # Send cmd and receive back data, expect 0x10 bytes
            res = bytes(self.dev.transmit(cmd_buf, 0x10))
            # Check if we have the identifier in returned bytes
            if POSSIBLE_DATA in res:
                print(f"Possible match with payload {cmd_buf}")
                print(f"Data: {res}")
                return cmd

class RegCONTROL:
    def __init__(self, val: int):
        self.val = val
        self.INTEN = (val & (1 << 7)) != 0
        self.I2CEN = (val & (1 << 6)) != 0
        self.STA = (val & (1 << 5)) != 0
        self.STO = (val & (1 << 4)) != 0
        self.SI = (val & (1 << 3)) != 0
        self.AA = (val & (1 << 2)) != 0
        # Reserved bits
        assert (val & 2) == 0
        assert (val & 1) == 0
    
    def __str__(self):
        return f"Status({self.val}) INTEN={self.INTEN} I2CEN={self.I2CEN} STA={self.STA} STO={self.STO} SI={self.SI} AA={self.AA}"

class Devices:
    GREATFET = "greatfet"
    RPI = "rpi"
    DUMMY = "dummy"

def main(device: I2CClient) -> int:
    rfunit = RfUnitI2C(device)

    if not rfunit.detect():
        print("RF Unit was not detected!")
        sys.exit(1)

    rfunit.init()
    rfunit.stop()

    print("Dumping flash")
    with open("dump.bin", "wb") as f:
        for chunk in rfunit.dump_flash(True):
            # For Micropython, you might want to print to UART instead..
            f.write(chunk)
    print("File written")

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
