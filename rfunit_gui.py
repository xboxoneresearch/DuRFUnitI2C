"""
Simple Tkinter GUI for interacting with the Xbox One RF Unit (ISD9160) via I2C.

- Supports GreatFET, Raspberry Pi (smbus2), and a Dummy backend.
- Supports a Pi Pico running MicroPython (via `vendor.pyboard` over USB serial).
- Exposes play/stop/reset, dump/flash, register read/write, and raw hex commands.

Run:
  python rfunit_gui.py
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import rfunit


def _parse_int(text: str) -> int:
    s = (text or "").strip().lower()
    if not s:
        raise ValueError("empty value")
    # int(x, 0) supports 0x.. and decimal.
    return int(s, 0)


def _parse_hex_bytes(text: str) -> list[int]:
    s = (text or "").strip()
    if not s:
        return []

    # Token form: "81 00" or "0x81,0x00"
    if any(ch in s for ch in (" ", ",", "\t", "\n", "\r", ";")):
        parts = s.replace(",", " ").replace(";", " ").split()
        out: list[int] = []
        for p in parts:
            p = p.strip().lower()
            if p.startswith("0x"):
                p = p[2:]
            if not p:
                continue
            out.append(int(p, 16) & 0xFF)
        return out

    # Packed form: "8100" or "0x8100"
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) % 2:
        raise ValueError("hex string length must be even (e.g. 8100)")
    return [int(s[i : i + 2], 16) & 0xFF for i in range(0, len(s), 2)]


def _hex_list(data: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _sound_items() -> list[tuple[str, int]]:
    return [
        ("0x00 PowerOn", rfunit.Sound.POWERON),
        ("0x01 Ding", rfunit.Sound.BING),
        ("0x02 PowerOff", rfunit.Sound.POWEROFF),
        ("0x03 DiscDrive1", rfunit.Sound.DISC_DRIVE_1),
        ("0x04 DiscDrive2", rfunit.Sound.DISC_DRIVE_2),
        ("0x05 DiscDrive3", rfunit.Sound.DISC_DRIVE_3),
        ("0x06 Plopp", rfunit.Sound.PLOPP),
        ("0x07 NoDisc", rfunit.Sound.NO_DISC),
        ("0x08 PloppLouder", rfunit.Sound.PLOPP_LOUDER),
    ]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DuRFUnitI2C GUI")
        self.minsize(880, 620)

        self._q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._busy = False

        self._dev: rfunit.I2CClient | None = None
        self._rf: rfunit.RfUnitI2C | None = None
        self._backend_type: str | None = None
        self._pyb = None  # Pico/MicroPython only: vendor.pyboard.Pyboard instance

        self._build_ui()
        self.after(50, self._drain_queue)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(9, weight=1)

        ttk.Label(top, text="Device:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar(value="greatfet")
        device_values = ["greatfet", "pico", "rpi", "dummy"]
        self.device_combo = ttk.Combobox(
            top, textvariable=self.device_var, state="readonly", values=device_values, width=10
        )
        self.device_combo.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.device_combo.bind("<<ComboboxSelected>>", lambda _evt: self._apply_device_ui_state())

        ttk.Label(top, text="RPi bus:").grid(row=0, column=2, sticky="w")
        self.rpi_bus_var = tk.StringVar(value="1")
        self.rpi_bus_entry = ttk.Entry(top, textvariable=self.rpi_bus_var, width=6)
        self.rpi_bus_entry.grid(row=0, column=3, sticky="w", padx=(6, 12))

        ttk.Label(top, text="Pico port:").grid(row=0, column=4, sticky="w")
        self.pico_port_var = tk.StringVar(value="")
        self.pico_port_entry = ttk.Entry(top, textvariable=self.pico_port_var, width=14)
        self.pico_port_entry.grid(row=0, column=5, sticky="w", padx=(6, 6))
        self.pico_detect_btn = ttk.Button(top, text="Detect", command=self.on_detect_pico)
        self.pico_detect_btn.grid(row=0, column=6, sticky="w", padx=(0, 12))

        self.connect_btn = ttk.Button(top, text="Connect", command=self.on_connect)
        self.connect_btn.grid(row=0, column=7, sticky="w")

        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self.on_disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=8, sticky="w", padx=(6, 0))

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=9, sticky="w", padx=(12, 0))

        self._apply_device_ui_state()

        mid = ttk.Frame(self, padding=(10, 0, 10, 10))
        mid.grid(row=1, column=0, sticky="nsew")
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        self.nb = ttk.Notebook(mid)
        self.nb.grid(row=0, column=0, sticky="nsew")

        self.tab_main = ttk.Frame(self.nb, padding=10)
        self.tab_flash = ttk.Frame(self.nb, padding=10)
        self.tab_reg = ttk.Frame(self.nb, padding=10)
        self.tab_raw = ttk.Frame(self.nb, padding=10)

        self.nb.add(self.tab_main, text="Main")
        self.nb.add(self.tab_flash, text="Flash")
        self.nb.add(self.tab_reg, text="Registers")
        self.nb.add(self.tab_raw, text="Raw")

        self._build_tab_main()
        self._build_tab_flash()
        self._build_tab_reg()
        self._build_tab_raw()

        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        ttk.Label(log_frame, text="Log:").grid(row=0, column=0, sticky="w")
        self.log = ScrolledText(log_frame, height=10, width=120)
        self.log.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.log.configure(state="disabled")

    def _build_tab_main(self) -> None:
        f = self.tab_main
        for i in range(0, 4):
            f.columnconfigure(i, weight=1)

        info = ttk.LabelFrame(f, text="Info", padding=10)
        info.grid(row=0, column=0, sticky="ew", columnspan=4)
        info.columnconfigure(3, weight=1)

        ttk.Button(info, text="Refresh Info", command=self.on_refresh).grid(row=0, column=0, sticky="w")
        ttk.Button(info, text="Stop", command=self.on_stop).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(info, text="Reset", command=self.on_reset).grid(row=0, column=2, sticky="w", padx=(6, 0))

        self.info_var = tk.StringVar(value="(not connected)")
        ttk.Label(info, textvariable=self.info_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))

        sound = ttk.LabelFrame(f, text="Sound", padding=10)
        sound.grid(row=1, column=0, sticky="ew", columnspan=4, pady=(10, 0))
        sound.columnconfigure(6, weight=1)

        self.sound_items = _sound_items()
        self.sound_label_to_idx = {label: idx for (label, idx) in self.sound_items}

        ttk.Label(sound, text="Preset:").grid(row=0, column=0, sticky="w")
        self.sound_preset_var = tk.StringVar(value=self.sound_items[1][0])  # Ding
        self.sound_preset_combo = ttk.Combobox(
            sound, textvariable=self.sound_preset_var, state="readonly", values=[x[0] for x in self.sound_items], width=18
        )
        self.sound_preset_combo.grid(row=0, column=1, sticky="w", padx=(6, 12))

        ttk.Label(sound, text="Or index:").grid(row=0, column=2, sticky="w")
        self.sound_custom_var = tk.StringVar(value="")
        self.sound_custom_entry = ttk.Entry(sound, textvariable=self.sound_custom_var, width=10)
        self.sound_custom_entry.grid(row=0, column=3, sticky="w", padx=(6, 12))
        ttk.Label(sound, text="(e.g. 0x03)").grid(row=0, column=4, sticky="w")

        ttk.Button(sound, text="Play", command=self.on_play).grid(row=0, column=5, sticky="w", padx=(12, 0))

    def _build_tab_flash(self) -> None:
        f = self.tab_flash
        f.columnconfigure(0, weight=1)

        ttk.Label(
            f,
            text=(
                "Warning: flashing erases and rewrites the entire RF Unit APROM.\n"
                "Make sure your wiring is correct and you have a verified image."
            ),
        ).grid(row=0, column=0, sticky="w")

        ops = ttk.Frame(f, padding=(0, 10, 0, 0))
        ops.grid(row=1, column=0, sticky="ew")
        ops.columnconfigure(6, weight=1)

        ttk.Button(ops, text="Dump Flash...", command=self.on_dump).grid(row=0, column=0, sticky="w")
        ttk.Button(ops, text="Flash File...", command=self.on_flash).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(ops, text="Boot LDROM", command=self.on_boot_ldrom).grid(row=0, column=2, sticky="w", padx=(24, 0))
        ttk.Button(ops, text="Boot APROM", command=self.on_boot_aprom).grid(row=0, column=3, sticky="w", padx=(6, 0))

        self.progress = ttk.Progressbar(f, orient="horizontal", mode="determinate", maximum=rfunit.FLASH_SIZE)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(14, 0))

    def _build_tab_reg(self) -> None:
        f = self.tab_reg
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Register:").grid(row=0, column=0, sticky="w")
        self.reg_var = tk.StringVar(value="0x0C")
        ttk.Entry(f, textvariable=self.reg_var, width=10).grid(row=0, column=1, sticky="w", padx=(6, 12))

        ttk.Button(f, text="Read (4 bytes)", command=self.on_reg_read).grid(row=0, column=2, sticky="w")

        ttk.Label(f, text="Write bytes:").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.reg_write_var = tk.StringVar(value="01")
        ttk.Entry(f, textvariable=self.reg_write_var, width=22).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(12, 0))
        ttk.Label(f, text='(hex, e.g. "FF FF")').grid(row=1, column=2, sticky="w", pady=(12, 0))
        ttk.Button(f, text="Write", command=self.on_reg_write).grid(row=1, column=3, sticky="w", pady=(12, 0))

        self.reg_out_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.reg_out_var).grid(row=2, column=0, columnspan=4, sticky="w", pady=(16, 0))

    def _build_tab_raw(self) -> None:
        f = self.tab_raw
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="TX bytes:").grid(row=0, column=0, sticky="w")
        self.raw_tx_var = tk.StringVar(value="81 01")
        ttk.Entry(f, textvariable=self.raw_tx_var).grid(row=0, column=1, sticky="ew", padx=(6, 12))

        ttk.Label(f, text="Read len:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.raw_readlen_var = tk.StringVar(value="0")
        ttk.Entry(f, textvariable=self.raw_readlen_var, width=10).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(10, 0))

        ttk.Button(f, text="Send", command=self.on_raw_send).grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Label(f, text='Examples: "C2" + read_len=128, "C3 <addr_u32_le>" + read_len=8').grid(
            row=2, column=1, sticky="w", pady=(12, 0)
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for w in (
            self.connect_btn,
            self.device_combo,
            self.rpi_bus_entry,
            self.pico_port_entry,
            self.pico_detect_btn,
            self.disconnect_btn,
        ):
            # connect controls: keep disconnect enabled while busy so user can bail
            if w is self.disconnect_btn:
                w.configure(state="normal" if (self._rf is not None) else "disabled")
            else:
                w.configure(state=state)

        if not busy:
            self.progress.stop()

    def _log(self, msg: str) -> None:
        self._q.put(("log", msg))

    def _ensure_connected(self) -> rfunit.RfUnitI2C:
        if self._rf is None:
            raise RuntimeError("Not connected")
        return self._rf

    def _run_bg(self, fn) -> None:
        if self._busy:
            return

        def _wrap():
            try:
                fn()
            except Exception:
                self._q.put(("log", traceback.format_exc().strip()))
            finally:
                self._q.put(("busy", False))

        self._set_busy(True)
        self._q.put(("busy", True))
        self._worker = threading.Thread(target=_wrap, daemon=True)
        self._worker.start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", str(payload) + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "info":
                    self.info_var.set(str(payload))
                elif kind == "progress":
                    cur, total = payload  # type: ignore[misc]
                    self.progress.configure(maximum=total)
                    self.progress["value"] = cur
                elif kind == "busy":
                    self._set_busy(bool(payload))
                elif kind == "set_pico_port":
                    self.pico_port_var.set(str(payload))
        except queue.Empty:
            pass
        finally:
            self.after(50, self._drain_queue)

    def _apply_device_ui_state(self) -> None:
        dev_type = self.device_var.get()

        def _set(widget, enabled: bool):
            widget.configure(state="normal" if enabled else "disabled")

        _set(self.rpi_bus_entry, dev_type == "rpi")
        _set(self.pico_port_entry, dev_type == "pico")
        _set(self.pico_detect_btn, dev_type == "pico")

    def _detect_pico_port(self) -> str:
        try:
            from serial.tools import list_ports
        except Exception as e:
            raise RuntimeError("pyserial is required for Pico support. Install with: pip install pyserial") from e

        ports = list_ports.comports()
        if not ports:
            raise RuntimeError("No serial ports found. Is your Pico plugged in and running MicroPython?")

        mp = [p for p in ports if (p.manufacturer or "") == "MicroPython"]
        if mp:
            return mp[0].device

        # Fallback: Raspberry Pi Pico VID is usually 0x2E8A. This may still include non-micropython devices.
        rp = [p for p in ports if getattr(p, "vid", None) == 0x2E8A]
        if rp:
            return rp[0].device

        for p in ports:
            self._log(f"Port: {p.device} | {p.description} | {p.manufacturer}")

        raise RuntimeError("Could not auto-detect a MicroPython device. Enter the COM port manually (e.g. COM6).")

    def on_detect_pico(self) -> None:
        def work() -> None:
            port = self._detect_pico_port()
            self._q.put(("set_pico_port", port))
            self._log(f"Detected Pico port: {port}")

        self._run_bg(work)

    def _ensure_pico(self):
        if self._backend_type != "pico" or self._pyb is None:
            raise RuntimeError("Not connected to a Pico/MicroPython device")
        return self._pyb

    def _pico_cleanup_files(self, pyb) -> None:
        # Avoid rfunit.py no-op behavior if dump.bin exists.
        for name in ("dump.bin", "flash.bin"):
            try:
                if pyb.fs_exists(name):
                    pyb.fs_rm(name)
            except Exception:
                pass

    def _pico_setup_i2c_helpers(self, pyb) -> None:
        # Define small helper functions for fast TX/RX without uploading rfunit.py.
        pyb.exec_(
            (
                "import machine, sys\n"
                "I2C_ADDR = 0x5A\n"
                "if sys.platform == 'rp2':\n"
                "  i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=50000)\n"
                "elif sys.platform == 'esp8266':\n"
                "  i2c = machine.I2C(sda=machine.Pin(4), scl=machine.Pin(5), freq=50000)\n"
                "else:\n"
                "  raise Exception('Unsupported platform: %s' % sys.platform)\n"
                "\n"
                "def _i2c_scan():\n"
                "  return i2c.scan()\n"
                "\n"
                "def _i2c_read(n):\n"
                "  return list(i2c.readfrom(I2C_ADDR, n))\n"
                "\n"
                "def _i2c_write(data):\n"
                "  i2c.writeto(I2C_ADDR, bytes(data))\n"
                "  return 0\n"
                "\n"
                "def _i2c_txrx(data, n):\n"
                "  i2c.writeto(I2C_ADDR, bytes(data))\n"
                "  if n:\n"
                "    return list(i2c.readfrom(I2C_ADDR, n))\n"
                "  return []\n"
            )
        )

    def _pico_exec_rfunit_py(self, pyb) -> None:
        rfunit_py_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rfunit.py")
        with open(rfunit_py_path, "rb") as f:
            script_data = f.read()

        buf = bytearray()

        def data_consumer(data: bytes):
            # Stream stdout from the MicroPython board; update progress when lines contain 0x.... offsets.
            nonlocal buf
            if not data:
                return
            buf.extend(data.replace(b"\x04", b""))
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                msg = line.decode("utf-8", errors="replace").strip()
                if not msg:
                    continue
                self._log(msg)
                if msg.startswith("0x"):
                    try:
                        off = int(msg, 16)
                    except ValueError:
                        continue
                    self._q.put(("progress", (off, rfunit.FLASH_SIZE)))

        pyb.exec_(script_data, data_consumer=data_consumer)
        # Ensure our small helper functions exist afterwards (the script may modify globals).
        try:
            self._pico_setup_i2c_helpers(pyb)
        except Exception:
            pass

    # UI Handlers

    def on_connect(self) -> None:
        def work() -> None:
            dev_type = self.device_var.get()
            self._q.put(("status", f"Connecting ({dev_type})..."))

            if dev_type == "greatfet":
                try:
                    dev = rfunit.GreatFetDevice()
                except Exception as e:
                    raise RuntimeError("Failed to init GreatFET. Did you `pip install greatfet`?") from e
            elif dev_type == "rpi":
                if os.name == "nt":
                    raise RuntimeError(
                        "RPi backend requires running on Linux on the Raspberry Pi (smbus2 needs fcntl/ioctl). "
                        "Options: run this GUI on the Pi, use GreatFET on Windows, or we can add a remote-Pi backend."
                    )
                try:
                    bus_id = _parse_int(self.rpi_bus_var.get())
                except Exception as e:
                    raise ValueError("Invalid RPi bus id") from e
                try:
                    dev = rfunit.RPiDevice(bus_id=bus_id)
                except Exception as e:
                    raise RuntimeError("Failed to init smbus2. Did you `pip install smbus2` (and run on a Pi)?") from e
            elif dev_type == "pico":
                try:
                    from vendor import pyboard
                except Exception as e:
                    raise RuntimeError("Failed to import vendor.pyboard") from e

                port = (self.pico_port_var.get() or "").strip()
                if not port:
                    port = self._detect_pico_port()
                    self._q.put(("set_pico_port", port))

                pyb = None
                try:
                    self._log(f"Connecting to MicroPython device on {port} ...")
                    pyb = pyboard.Pyboard(port, 115200)
                    pyb.enter_raw_repl()

                    self._pico_setup_i2c_helpers(pyb)

                    class PicoDevice(rfunit.I2CClient):
                        def __init__(self, pyb_):
                            self.pyb = pyb_

                        def scan(self):
                            return self.pyb.eval("_i2c_scan()", parse=True)

                        def read(self, read_len: int):
                            return self.pyb.eval(f"_i2c_read({int(read_len)})", parse=True)

                        def write(self, data):
                            self.pyb.eval(f"_i2c_write({list(map(int, data))})", parse=True)

                        def transmit(self, data, read_len: int):
                            return self.pyb.eval(
                                f"_i2c_txrx({list(map(int, data))}, {int(read_len)})", parse=True
                            )

                    self._pyb = pyb
                    dev = PicoDevice(pyb)
                except Exception:
                    if pyb is not None:
                        try:
                            pyb.exit_raw_repl()
                        except Exception:
                            pass
                        try:
                            pyb.close()
                        except Exception:
                            pass
                    raise
            elif dev_type == "dummy":
                dev = rfunit.DummyDevice()
            else:
                raise NotImplementedError(dev_type)

            rf = rfunit.RfUnitI2C(dev, logger=self._log)
            if not rf.detect():
                raise RuntimeError("RF Unit was not detected (expected I2C address 0x5A)")

            rf.init()
            rf.stop()

            self._dev = dev
            self._rf = rf
            self._backend_type = dev_type

            self._q.put(("status", "Connected"))
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_disconnect(self) -> None:
        try:
            if self._pyb is not None:
                try:
                    self._pyb.exit_raw_repl()
                except Exception:
                    pass
                try:
                    self._pyb.close()
                except Exception:
                    pass
        finally:
            self._pyb = None
        self._dev = None
        self._rf = None
        self._backend_type = None
        self._q.put(("status", "Disconnected"))
        self._q.put(("info", "(not connected)"))

    def _collect_info(self, rf: rfunit.RfUnitI2C) -> str:
        status_u16 = rf.read_status()
        status = status_u16 & 0xFF
        in_ldrom = rf.is_in_ldrom()
        fw = b""
        vpe = b""
        if not in_ldrom:
            try:
                fw = rf.read_fw_version()
                vpe = rf.read_vpe_version()
            except Exception as e:
                self._log(f"Warning: failed reading versions: {e!r}")
        return (
            f"status=0x{status:02X} (u16=0x{status_u16:04X}) | "
            f"ldrom={in_ldrom} | "
            f"fw='{fw.decode(errors='replace') if fw else ''}' | "
            f"vpe='{vpe.decode(errors='replace') if vpe else ''}'"
        )

    def on_refresh(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_play(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()

            custom = (self.sound_custom_var.get() or "").strip()
            if custom:
                idx = _parse_int(custom)
            else:
                idx = self.sound_label_to_idx[self.sound_preset_var.get()]

            rf.init()
            rf.stop()
            rf.play_sound(idx & 0xFF)
            self._log(f"Play sound: 0x{idx & 0xFF:02X}")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_stop(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            rf.stop()
            self._log("Stop")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_reset(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            rf.reset()
            self._log("Reset")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_boot_ldrom(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            self._log("Boot LDROM...")
            ok = rf.boot_to_ldrom()
            self._log(f"Boot LDROM result: {ok}")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_boot_aprom(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            self._log("Boot APROM...")
            ok = rf.boot_to_aprom()
            self._log(f"Boot APROM result: {ok}")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_dump(self) -> None:
        out_path = filedialog.asksaveasfilename(
            title="Save dump as...",
            defaultextension=".bin",
            filetypes=[("Binary", "*.bin"), ("All files", "*.*")],
            initialfile="dump.bin",
        )
        if not out_path:
            return

        def work() -> None:
            if self._backend_type == "pico":
                pyb = self._ensure_pico()
                rf = self._ensure_connected()

                self._q.put(("progress", (0, rfunit.FLASH_SIZE)))
                self._log(f"Dumping flash via Pico to: {out_path}")
                self._pico_cleanup_files(pyb)
                self._pico_exec_rfunit_py(pyb)

                self._log("Copying dump.bin from Pico...")

                def prog(written: int, total: int):
                    self._q.put(("progress", (written, total)))

                pyb.fs_get("dump.bin", out_path, progress_callback=prog)
                try:
                    pyb.fs_rm("dump.bin")
                except Exception:
                    pass

                self._log("Dump complete")
                self._q.put(("info", self._collect_info(rf)))
                return

            rf = self._ensure_connected()
            self._log(f"Dumping flash to: {out_path}")
            pos = 0
            total = rfunit.FLASH_SIZE
            with open(out_path, "wb") as f:
                for chunk in rf.dump_flash(0, total):
                    f.write(chunk)
                    pos += len(chunk)
                    self._q.put(("progress", (pos, total)))
            self._log("Dump complete")
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_flash(self) -> None:
        in_path = filedialog.askopenfilename(
            title="Select flash image (0x24400 bytes)...",
            filetypes=[("Binary", "*.bin"), ("All files", "*.*")],
        )
        if not in_path:
            return

        try:
            sz = os.path.getsize(in_path)
        except OSError:
            messagebox.showerror("Error", "Failed to stat selected file")
            return

        if sz != rfunit.FLASH_SIZE:
            messagebox.showerror(
                "Error",
                f"Invalid flash size.\nExpected: 0x{rfunit.FLASH_SIZE:X} ({rfunit.FLASH_SIZE} bytes)\nGot: {sz} bytes",
            )
            return

        if not messagebox.askyesno(
            "Confirm flash",
            "This will erase and rewrite the entire RF Unit flash.\n\nContinue?",
        ):
            return

        def work() -> None:
            if self._backend_type == "pico":
                pyb = self._ensure_pico()
                rf = self._ensure_connected()

                self._q.put(("progress", (0, rfunit.FLASH_SIZE)))
                self._log(f"Flashing via Pico: {in_path}")
                self._pico_cleanup_files(pyb)

                self._log("Uploading flash.bin to Pico...")

                def prog(written: int, total: int):
                    self._q.put(("progress", (written, total)))

                pyb.fs_put(in_path, "flash.bin", progress_callback=prog)
                self._q.put(("progress", (0, rfunit.FLASH_SIZE)))
                self._pico_exec_rfunit_py(pyb)
                try:
                    pyb.fs_rm("flash.bin")
                except Exception:
                    pass

                self._log("Flash complete")
                self._q.put(("info", self._collect_info(rf)))
                return

            rf = self._ensure_connected()
            self._log(f"Flashing file: {in_path}")

            if not rf.is_in_ldrom():
                self._log("Entering LDROM...")
                if not rf.boot_to_ldrom():
                    raise RuntimeError("Failed to enter LDROM")

            self._log("Erasing flash...")
            if not rf.erase_flash(0x00, rfunit.FLASH_SIZE):
                raise RuntimeError("Erase failed")

            self._log("Writing flash...")
            pos = 0
            total = rfunit.FLASH_SIZE
            chunk_sz = 0x80
            with open(in_path, "rb") as f:
                for addr in range(0, total, chunk_sz):
                    data = f.read(chunk_sz)
                    if len(data) != chunk_sz:
                        raise RuntimeError(f"Unexpected short read at 0x{addr:X}")
                    if not rf.write_flash(addr, data):
                        raise RuntimeError(f"Write failed at 0x{addr:X}")
                    pos = addr + chunk_sz
                    self._q.put(("progress", (pos, total)))

            self._log("Rebooting to APROM...")
            rf.boot_to_aprom()
            self._log("Flash complete")

            rf.init()
            rf.stop()
            try:
                rf.play_sound(rfunit.Sound.BING)
            except Exception:
                pass
            self._q.put(("info", self._collect_info(rf)))

        self._run_bg(work)

    def on_reg_read(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            reg = _parse_int(self.reg_var.get()) & 0xFF
            data = rf.read_register(reg)
            as_u32 = int.from_bytes(bytes(data[:4]), "little", signed=False) if len(data) >= 4 else 0
            out = f"reg 0x{reg:02X}: {_hex_list(data)} (u32=0x{as_u32:08X})"
            self._log(out)
            self.reg_out_var.set(out)

        self._run_bg(work)

    def on_reg_write(self) -> None:
        if not messagebox.askyesno("Confirm write", "Write register now?"):
            return

        def work() -> None:
            rf = self._ensure_connected()
            reg = _parse_int(self.reg_var.get()) & 0xFF
            data = _parse_hex_bytes(self.reg_write_var.get())
            if not data:
                raise ValueError("No write bytes provided")
            rf.write_register(reg, data)
            out = f"wrote reg 0x{reg:02X}: {_hex_list(data)}"
            self._log(out)
            self.reg_out_var.set(out)

        self._run_bg(work)

    def on_raw_send(self) -> None:
        def work() -> None:
            rf = self._ensure_connected()
            tx = _parse_hex_bytes(self.raw_tx_var.get())
            if not tx:
                raise ValueError("TX is empty")
            read_len = _parse_int(self.raw_readlen_var.get())

            self._log(f"TX ({len(tx)}): {_hex_list(tx)} | read_len={read_len}")
            if read_len > 0:
                rx = rf.dev.transmit(tx, read_len)  # type: ignore[union-attr]
                self._log(f"RX ({len(rx)}): {_hex_list(rx)}")
                if len(rx) >= 2:
                    self._log(f"RX data (drop status): {_hex_list(rx[2:])}")
            else:
                rf.dev.write(tx)  # type: ignore[union-attr]
                self._log("TX write complete")

        self._run_bg(work)


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
