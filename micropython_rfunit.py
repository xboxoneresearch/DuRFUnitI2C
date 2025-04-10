import os
import sys
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

dump_path = os.path.join(bundle_dir, "dump.bin")
print(dump_path)

if Path(dump_path).is_file():
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

print("Executing dumping script...")
# Execute the dumping script
res = pyb.execfile("rfunit.py")
if b'RF Unit was not detected' in res:
    print("RF Unit not detected, exiting!")
    exit(2)
elif b'File written' not in res:
    print("Failed dump RF unit")
    print("Script result:")
    print(res)
    exit(3)

# Copy dump from micropython filesystem
dump_bytes = pyb.fs_get("dump.bin", dump_path)
print(f"Dump copied to {dump_path}")

pyb.exit_raw_repl()
exit(0)
