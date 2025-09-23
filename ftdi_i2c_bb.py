import time
from pyftdi.gpio import GpioAsyncController

# FT4232H interface C (3rd port)
DEFAULT_FTDI_URL = 'ftdi://ftdi:4232h/3'

# Pin mapping within this interface
SCL_BIT = 1 << 6  # Pin 6
SDA_BIT = 1 << 7  # Pin 7

I2C_PIN_MASK = SCL_BIT | SDA_BIT

# I2C timing
T_DELAY = 5e-6  # ~5 µs delay => ~100 kHz bus

class FtdiBitBangI2C:
    def __init__(self, url: str = DEFAULT_FTDI_URL):
        self.gpio = GpioAsyncController()
        # Both pins inputs initially (released -> pulled up externally)
        self.gpio.configure(url, direction=0x00)

    def close(self):
        self.gpio.close()

    def _delay(self):
        time.sleep(T_DELAY)

    def _set_scl(self, level: bool):
        if level:
            # release line (input)
            self.gpio.set_direction(SCL_BIT, self.gpio.direction & ~SCL_BIT)
        else:
            # drive low
            self.gpio.set_direction(SCL_BIT, self.gpio.direction | SCL_BIT)
            self.gpio.write_port(self.gpio.read_port() & ~SCL_BIT)
        self._delay()

    def _set_sda(self, level: bool):
        if level:
            # release SDA
            self.gpio.set_direction(SDA_BIT, self.gpio.direction & ~SDA_BIT)
        else:
            # drive low
            self.gpio.set_direction(SDA_BIT, self.gpio.direction | SDA_BIT)
            self.gpio.write_port(self.gpio.read_port() & ~SDA_BIT)
        self._delay()

    def _read_sda(self) -> bool:
        return bool(self.gpio.read_port() & SDA_BIT)

    def scan(self) -> list[int]:
        res = []
        for addr in range(1, 0x7F):
            self.start()
            if self.write_byte((addr << 1) | 1):
                res.append(addr)
            self.stop()
        return res

    def start(self):
        self._set_sda(True)
        self._set_scl(True)
        self._set_sda(False)
        self._set_scl(False)

    def stop(self):
        self._set_sda(False)
        self._set_scl(True)
        self._set_sda(True)

    def write_bit(self, bit: int):
        if bit:
            self._set_sda(True)
        else:
            self._set_sda(False)
        self._set_scl(True)
        self._set_scl(False)

    def read_bit(self) -> int:
        self._set_sda(True)  # release SDA
        self._set_scl(True)
        bit = self._read_sda()
        self._set_scl(False)
        return int(bit)

    def write_byte(self, data: int) -> bool:
        for i in range(8):
            self.write_bit((data & 0x80) != 0)
            data <<= 1
        # Read ACK (0 = ACK)
        ack = self.read_bit()
        return ack == 0

    def read_byte(self, ack: bool) -> int:
        val = 0
        for i in range(8):
            val = (val << 1) | self.read_bit()
        # Send ACK/NACK
        self.write_bit(0 if ack else 1)
        return val

