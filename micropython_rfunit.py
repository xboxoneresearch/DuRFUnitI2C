import os
import sys
from tqdm import tqdm
from vendor import pyboard
from pathlib import Path
from serial.tools import list_ports

# Reference: https://pyinstaller.org/en/stable/runtime-information.html#using-sys-executable-and-sys-argv-0
if getattr(sys, 'frozen', False):
    # we are running in a bundle
    bundle_dir = sys._MEIPASS
else:
    # we are running in a normal Python environment
    bundle_dir = os.path.dirname(os.path.abspath(__file__))

cwd = os.getcwd()

FLASH_FILESIZE = 0x24400

DUMP_FILENAME = "dump.bin"
FLASH_FILENAME = "flash.bin"

# Path in bundle or next to script
rfunit_py_path = os.path.join(bundle_dir, "rfunit.py")
# Path in current working directory
dump_path = os.path.join(cwd, DUMP_FILENAME)
flash_path = os.path.join(cwd, FLASH_FILENAME)

do_flash = False
do_dump = True

if Path(flash_path).is_file():
   do_dump = False
   do_flash = True
elif Path(dump_path).is_file():
    print(f"Dump file at '{dump_path}' already exists, please rename or delete and try again!")
    sys.exit(1)

"""
# Linux
{
  'device': '/dev/ttyACM0',
  'name': 'ttyACM0',
  'description': 'Board in FS mode - Board CDC',
  'hwid': 'USB VID:PID=2E8A:0005 SER=abc LOCATION=1-3:1.0',
  'vid': 11914,
  'pid': 5,
  'serial_number': 'abc',
  'location': '1-3:1.0',
  'manufacturer': 'MicroPython',
  'product': 'Board in FS mode',
  'interface': 'Board CDC'
}
"""
ports = list_ports.comports()
if len(ports) == 0:
  print("No serial port found. Is Pico with flashed Micropython connected?")
  sys.exit(1)

devices = list(filter(lambda x: x.manufacturer == "MicroPython", ports))
device = None

if devices:
  device = devices[0]
else:
  # Did not find device by manufacturer -> let user choose
  print("Found following serial ports:")
  for idx, p in enumerate(ports):
    print(f"{idx}) {p.name} - {p.device} - {p.manufacturer}")
  index = None
  while not device:
    try:
      choice = int(input("Choose device, enter number and press [ENTER] (CTRL-C to exit): "))
    except ValueError:
      continue

    try:
      device = ports[choice]
    except:
      print("Invalid choice!")
      continue

print(f"chosen device: {device} - Port: {device.device}")

def exit(code: int):
  input("[ Press any key to exit ]")
  sys.exit(code)

# connect to micropython device
pyb = pyboard.Pyboard(device.device, 115200)
print("Entering RAW REPL")
pyb.enter_raw_repl()

# Cleanup stale files
if pyb.fs_exists(DUMP_FILENAME):
  pyb.fs_rm(DUMP_FILENAME)
if pyb.fs_exists(FLASH_FILENAME):
  pyb.fs_rm(FLASH_FILENAME)

if do_flash:
  print("Copying flash.bin to micropython device...")
  pyb.fs_put(flash_path, "flash.bin")

progress_bar: tqdm = None

data_buf = bytearray()
def data_consumer(data: bytes):
    # Read data from micropython device via serial, byte by byte
    global data_buf, progress_bar
    data_buf += data
    if data == b'\n':
      msg = bytes(data_buf).decode("utf-8").strip()
      if msg.startswith("0x"):
        offset = int(msg, 16)
        # Progress
        if not progress_bar:
          progress_bar = tqdm(
            total=FLASH_FILESIZE,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            miniters=1
          )
        progress_bar.update(offset - progress_bar.n)
      elif msg:
        print(msg)
 
      data_buf.clear()

print("Executing script...")
with open(rfunit_py_path, "rb") as f:
    script_data = f.read()

pyb.exec_(script_data, data_consumer)

if do_dump:
  print("Copying dump from micropython filesystem")
  pyb.fs_get("dump.bin", dump_path)
  print(f"Dump copied to {dump_path}")

pyb.exit_raw_repl()
exit(0)
