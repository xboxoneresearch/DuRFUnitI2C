"""
ISD9160 VPE/DPCM Audio Firmware GUI

Loads an ISD9160 firmware binary, displays all audio segments,
and allows per-segment extract (to WAV) and inject (from WAV).
"""

import os
import struct
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import darkdetect
import sv_ttk

# Ensure the script's directory is on the path so vpe_extract can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vpe import (
    FirmwareDecoderContext,
    ISD9160Firmware,
    Segment,
    VPEEncoder,
    VpeSegmentHeader,
)


def size_to_magnitude(size: int) -> tuple[int | float, str]:
    if size < 1024:
        return size, "bytes"
    return size / 1024, "kBytes"


class FirmwareGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ISD9160 Audio Tool")
        # self.root.minsize(720, 400)

        self.decoder: FirmwareDecoderContext
        self.fw: ISD9160Firmware
        self.fw_path: str  # path to loaded .bin

        self._build_toolbar()
        self._build_segment_list()
        self._build_log()

    # ------------------------------------------------------------------ UI
    def _build_toolbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))

        ttk.Button(bar, text="Open Firmware", command=self._open_fw).pack(side=tk.LEFT)

        self.save_btn = ttk.Button(
            bar, text="Save Firmware As...", command=self._save_fw, state=tk.DISABLED
        )
        self.save_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.fw_label = ttk.Label(bar, text="No firmware loaded")
        self.fw_label.pack(side=tk.LEFT, padx=(12, 0))

    def _build_segment_list(self):
        frame = ttk.LabelFrame(self.root, text="Audio Segments")
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Scrollable canvas that holds the segment rows
        canvas = tk.Canvas(frame, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        self.seg_inner = ttk.Frame(canvas)
        self.seg_inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.seg_inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse-wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _build_log(self):
        frame = ttk.LabelFrame(self.root, text="Log")
        frame.pack(fill=tk.X, padx=6, pady=(0, 6))

        self.log_text = tk.Text(frame, height=6, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.X, padx=4, pady=4)

    # ------------------------------------------------------------------ log
    def _log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ open
    def _open_fw(self):
        path = filedialog.askopenfilename(
            title="Open ISD9160 Firmware",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            self.fw = ISD9160Firmware.from_filepath(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load firmware:\n{e}")
            return

        self.fw_path = path
        self.patched = bytearray(self.fw.data)
        self.decoder = FirmwareDecoderContext(self.fw.data)
        self.fw_label.configure(
            text=f"{os.path.basename(path)}  ({len(self.fw.segments)} segments)"
        )
        self.save_btn.configure(state=tk.NORMAL)
        self._log(f"Loaded {path}  —  {len(self.fw.segments)} audio segments found")
        self._populate_segments()

    # ------------------------------------------------------------------ save
    def _save_fw(self):
        if self.patched is None:
            return
        path = filedialog.asksaveasfilename(
            title="Save Patched Firmware",
            defaultextension=".bin",
            initialfile="patched_fw.bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(self.patched)
        self._log(f"Saved patched firmware to {path}")

    # ------------------------------------------------------------------ rows
    def _populate_segments(self):
        # Clear old rows
        for child in self.seg_inner.winfo_children():
            child.destroy()

        # Header row

        headers = ("Seg", "Codec", "Offset", "Size", "Details", "", "", "")
        for col, hdr in enumerate(headers):
            lbl = ttk.Label(self.seg_inner, text=hdr, font=("", 9, "bold"))
            lbl.grid(row=0, column=col, padx=4, pady=(2, 4), sticky=tk.W)

        for row_idx, seg in enumerate(self.fw.segments, start=1):
            idx = seg.index
            codec = seg.codec_name
            is_vpe = seg.codec_type in (0x1D, 0x1E)

            # Compute extra details for VPE segments
            details = ""
            if is_vpe and seg.size >= 16:
                hdr = VpeSegmentHeader.from_bytes(seg.data)
                details = (
                    f"{hdr.samplerate // 1000}kHz ({hdr.bitrate}bps {hdr.num_frames}fr {hdr.duration_secs:.1f}s)"
                )
            else:
                sr_map = {
                    0: 4000,
                    1: 5300,
                    2: 6400,
                    3: 8000,
                    4: 12000,
                    5: 16000,
                    6: 32000,
                    7: 16000,
                }
                ctype = (seg.code_byte >> 5) & 0x7
                sr = sr_map.get(ctype, 8000)
                details = f"{sr // 1000}kHz"

            size, unit = size_to_magnitude(seg.size)

            ttk.Label(self.seg_inner, text=str(idx)).grid(
                row=row_idx, column=0, padx=4, sticky=tk.W
            )
            ttk.Label(self.seg_inner, text=codec).grid(
                row=row_idx, column=1, padx=4, sticky=tk.W
            )
            ttk.Label(self.seg_inner, text=f"0x{seg.start:05X}").grid(
                row=row_idx, column=2, padx=4, sticky=tk.W
            )
            ttk.Label(self.seg_inner, text=f"{size:.2f} {unit}").grid(
                row=row_idx, column=3, padx=4, sticky=tk.W
            )
            ttk.Label(self.seg_inner, text=details).grid(
                row=row_idx, column=4, padx=4, sticky=tk.W
            )

            play_btn = ttk.Button(
                self.seg_inner, text="Play", command=lambda s=seg: self._play_seg(s)
            )
            play_btn.grid(row=row_idx, column=5, padx=4)

            ext_btn = ttk.Button(
                self.seg_inner,
                text="Extract",
                command=lambda s=seg: self._extract_seg(s),
            )
            ext_btn.grid(row=row_idx, column=6, padx=4)

            inj_btn = ttk.Button(
                self.seg_inner, text="Inject", command=lambda s=seg: self._inject_seg(s)
            )
            inj_btn.grid(row=row_idx, column=7, padx=4)

    # ------------------------------------------------------------------ playback
    def _play_seg(self, seg: Segment):
        self._log(f"Decoding and playing segment {seg.index} ({seg.codec_name})...")
        self._run_in_thread(self._do_play_seg, seg)

    def _do_play_seg(self, seg: Segment):
        try:
            # Import sounddevice only when needed
            try:
                import sounddevice as sd
            except ImportError:
                self._log_ts(
                    "sounddevice not installed. Please install with 'pip install sounddevice'."
                )
                return

            samples, sr = self.decoder.decode_segment(seg.data, seg.codec_type)

            def audio_cb(outdata: bytearray, frames: int, time, status):
                chunk = data[self.playback_pos :]
                if len(chunk) < len(outdata):
                    outdata[: len(chunk)] = chunk
                    outdata[len(chunk) :] = b"\x00" * (len(outdata) - len(chunk))
                    raise sd.CallbackStop
                else:
                    outdata[:] = chunk[: len(outdata)]
                    self.playback_pos += len(outdata)

            self.playback_pos = 0
            data: bytes = struct.pack(
                f"<{len(samples)}h", *[int(max(-32768, min(32767, s))) for s in samples]
            )

            event = threading.Event()
            os = sd.RawOutputStream(
                samplerate=sr,
                channels=1,
                dtype="int16",
                callback=audio_cb,
                finished_callback=event.set,
            )

            self._log_ts(f"  Playing {len(samples)} samples at {sr} Hz...")
            os.start()
            event.wait()
            self._log_ts("  Playback finished.")
        except Exception as e:
            self._log_ts(f"  Playback error: {e}")

    # ------------------------------------------------------------------ extract
    def _extract_seg(self, seg: Segment):
        idx = seg.index
        codec = seg.codec_name
        default_name = f"segment_{idx:02d}_{codec.replace('/', '_')}.wav"

        path = filedialog.asksaveasfilename(
            title=f"Extract Segment {idx}",
            defaultextension=".wav",
            initialfile=default_name,
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return

        self._log(f"Extracting segment {idx} ({codec})...")
        self._run_in_thread(self._do_extract, seg, path)

    def _do_extract(self, seg: Segment, path: str):
        try:
            samples, sr = self.decoder.decode_segment(seg.data, seg.codec_type)
            ISD9160Firmware._write_wav(path, samples, sr, normalize=False)
            dur = len(samples) / sr
            self._log_ts(f"  -> {path}  ({len(samples)} samples, {sr}Hz, {dur:.1f}s)")
        except Exception as e:
            self._log_ts(f"  Extract error: {e}")

    # ------------------------------------------------------------------ inject
    def _inject_seg(self, seg: Segment):
        idx = seg.index
        is_vpe = seg.codec_type in (0x1D, 0x1E)

        path = filedialog.askopenfilename(
            title=f"Inject WAV into Segment {idx}",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return

        if is_vpe:
            self._log(f"Encoding WAV -> VPE for segment {idx}...")
            self._run_in_thread(self._do_inject_vpe, seg, path)
        else:
            messagebox.showinfo(
                "Not supported",
                f"Segment {idx} uses {seg.codec_name} codec.\n"
                "Only VPE/Siren7 injection is currently supported.",
            )

    def _do_inject_vpe(self, seg: Segment, wav_path: str):
        try:
            encoder = VPEEncoder(self.fw.data)
            new_frames = encoder.encode_vpe_segment_frames_from_wav(seg.data, wav_path)

            expected = seg.size - 16
            if len(new_frames) != expected:
                self._log_ts(
                    f"  Frame size mismatch: got {len(new_frames)}, need {expected}"
                )
                return

            # Patch into working copy (preserve 16-byte segment header)
            self.patched[seg.start + 16 : seg.end + 1] = new_frames
            self._log_ts(
                f"  Segment {seg.index} injected OK  ({len(new_frames)} bytes replaced)"
            )
        except Exception as e:
            self._log_ts(f"  Inject error: {e}")

    # ------------------------------------------------------------------ threading
    def _run_in_thread(self, fn, *args):
        """Run fn(*args) on a background thread; keeps GUI responsive."""

        def wrapper():
            fn(*args)

        threading.Thread(target=wrapper, daemon=True).start()

    def _log_ts(self, msg):
        """Thread-safe log append."""
        self.root.after(0, self._log, msg)


def main():
    root = tk.Tk()
    FirmwareGUI(root)
    sv_ttk.set_theme(darkdetect.theme())
    root.mainloop()


if __name__ == "__main__":
    main()
