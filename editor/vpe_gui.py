"""
ISD9160 VPE/DPCM Audio Firmware GUI

Loads an ISD9160 firmware binary, displays all audio segments,
and allows per-segment extract (to WAV) and inject (from WAV).
"""

from __future__ import annotations

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
    ENCODING_BEST,
    ENCODING_PRESETS,
    VPE_AUDIO_DATA_LIMIT,
    AudioSegment,
    DPCMSegmentHeader,
    EncodingProfile,
    FirmwareDecoderContext,
    ISD9160Firmware,
    RfUnitSound,
    VPEEncoder,
    VpeSegmentHeader,
)


def size_to_unit(size: int) -> tuple[int | float, str]:
    if size < 1024:
        return size, "bytes"
    return size / 1024, "kBytes"


class SegmentRowWidgets:
    """Container for playback segment row widgets."""

    def __init__(self, parent: tk.Widget, row: int):
        self.idx_label = ttk.Label(parent)
        self.codec_label = ttk.Label(parent)
        self.offset_label = ttk.Label(parent)
        self.size_label = ttk.Label(parent)
        self.details_label = ttk.Label(parent)
        self.play_btn = ttk.Button(parent, text="Play")
        self.extract_wav_btn = ttk.Button(parent, text="Extract WAV")
        self.extract_raw_btn = ttk.Button(parent, text="Extract RAW")
        self.row = row
        self._widgets = [
            self.idx_label,
            self.codec_label,
            self.offset_label,
            self.size_label,
            self.details_label,
            self.play_btn,
            self.extract_wav_btn,
            self.extract_raw_btn,
        ]

    def grid(self, row: int | None = None):
        """Grid all widgets at the specified row."""
        if row is not None:
            self.row = row
        for col, widget in enumerate(self._widgets):
            widget.grid(row=self.row, column=col, padx=4, sticky=tk.W)

    def grid_remove(self):
        """Hide all widgets."""
        for widget in self._widgets:
            widget.grid_remove()


class CreatorRowWidgets:
    """Container for creator segment row widgets."""

    def __init__(self, parent: tk.Widget, row: int):
        self.idx_label = ttk.Label(parent)
        self.codec_label = ttk.Label(parent)
        self.size_label = ttk.Label(parent)
        self.details_label = ttk.Label(parent)
        self.stub_btn = ttk.Button(parent, text="Make Stub")
        self.play_btn = ttk.Button(parent, text="Play")
        self.inject_wav_btn = ttk.Button(parent, text="Inject WAV")
        self.inject_raw_btn = ttk.Button(parent, text="Inject RAW")
        self.row = row
        self._widgets = [
            self.idx_label,
            self.codec_label,
            self.size_label,
            self.details_label,
            self.stub_btn,
            self.play_btn,
            self.inject_wav_btn,
            self.inject_raw_btn,
        ]

    def grid(self, row: int | None = None):
        """Grid all widgets at the specified row."""
        if row is not None:
            self.row = row
        for col, widget in enumerate(self._widgets):
            widget.grid(row=self.row, column=col, padx=4, sticky=tk.W)

    def grid_remove(self):
        """Hide all widgets."""
        for widget in self._widgets:
            widget.grid_remove()


class FirmwareGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ISD9160 Audio Tool")
        self.root.minsize(720, 800)

        self.decoder: FirmwareDecoderContext
        self.fw: ISD9160Firmware
        self.fw_path: str  # path to loaded .bin
        self.creator_segments: list[AudioSegment] = [AudioSegment.empty()] * 9
        self.version_str_var = tk.StringVar(value="")
        self.quality_var = tk.StringVar(value=next(iter(ENCODING_PRESETS)))

        self._build_toolbar()
        self._build_tabs()
        self._build_segment_list()
        self._build_vpe_creator()
        self._build_free_space_indicator()
        self._build_log()
        self._populate_creator()

    # ------------------------------------------------------------------ UI
    def _build_toolbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill=tk.X, padx=6, pady=(6, 6))

        ttk.Button(bar, text="Open Firmware", command=self._open_fw).pack(
            side=tk.LEFT, padx=3, pady=(3, 3)
        )

        self.fw_label = ttk.Label(bar, text="No firmware loaded")
        self.fw_label.pack(fill=tk.X, padx=3, pady=(3, 3))

        self.version_label = ttk.Label(master=bar, text="")
        self.version_label.pack(side=tk.BOTTOM, fill=tk.X, padx=3, pady=(3, 3))

    def _build_tabs(self):
        tabControl = ttk.Notebook(self.root)
        self.tab_playback = ttk.Frame(tabControl)
        self.tab_creator = ttk.Frame(tabControl)

        tabControl.add(self.tab_playback, text="Playback")
        tabControl.add(self.tab_creator, text="Creator")
        tabControl.pack(expand=1, fill="both")

    def _build_segment_list(self):
        frame = ttk.LabelFrame(self.tab_playback, text="Audio Segments")
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        canvas = tk.Canvas(frame, borderwidth=0, highlightthickness=0)
        self.seg_inner = ttk.Frame(canvas)

        headers = ("Seg", "Codec", "Offset", "Size", "Details", "", "", "")
        # Header row
        self._segment_header_widgets = []
        for col, hdr in enumerate(headers):
            lbl = ttk.Label(self.seg_inner, text=hdr, font=("", 9, "bold"))
            lbl.grid(row=0, column=col, padx=4, pady=(2, 4), sticky=tk.W)
            self._segment_header_widgets.append(lbl)

        canvas.create_window((0, 0), window=self.seg_inner, anchor=tk.NW)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._segment_rows = []  # Cache for segment row widgets

    def _build_vpe_creator(self):
        frame = ttk.LabelFrame(self.tab_creator, text="Audio Segments")
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        canvas = tk.Canvas(frame, borderwidth=0, highlightthickness=0)

        self.creator_inner = ttk.Frame(canvas)
        headers = ("Seg", "Codec", "Size", "Details", "", "", "", "")
        # Header row
        self._creator_header_widgets = []
        for col, hdr in enumerate(headers):
            lbl = ttk.Label(self.creator_inner, text=hdr, font=("", 9, "bold"))
            lbl.grid(row=0, column=col, padx=4, pady=(2, 4), sticky=tk.W)
            self._creator_header_widgets.append(lbl)

        canvas.create_window((0, 0), window=self.creator_inner, anchor=tk.NW)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._creator_rows = []  # Cache for creator row widgets

        controls = ttk.Frame(self.tab_creator)
        controls.pack(fill=tk.X, padx=6, pady=(0, 6))

        ttk.Label(controls, text="Quality preset:").grid(row=0, column=0)

        self.quality_combo = ttk.Combobox(
            controls,
            textvariable=self.quality_var,
            values=list(ENCODING_PRESETS.keys()),
            state="readonly",
            width=81,
        )
        self.quality_combo.grid(row=0, column=1, padx=3, pady=(3, 3))
        self.quality_combo.bind(
            "<<ComboboxSelected>>", lambda _evt: self._update_space_indicator()
        )

        ttk.Label(controls, text="Version:").grid(row=1, column=0)

        self.fw_version_entry = ttk.Entry(
            controls, textvariable=self.version_str_var, width=85
        )
        self.fw_version_entry.grid(row=1, column=1)

        self.save_btn = ttk.Button(
            controls,
            text="Save Firmware As...",
            command=self._save_new_fw,
            state=tk.DISABLED,
        )
        self.save_btn.grid(row=2, column=0, columnspan=2)

    def _build_free_space_indicator(self):
        frame = ttk.LabelFrame(self.root, text="Used space")
        frame.pack(fill=tk.X, padx=6, pady=(6, 6))
        self.pb = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate", length=100
        )
        # place the progressbar
        # pb.grid(column=0, row=0, columnspan=2, padx=10, pady=20)
        self.pb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.remaining_label = ttk.Label(frame, text="~0.0s left")
        self.remaining_label.pack(side=tk.RIGHT, padx=(12, 0))

        self.pb_label = ttk.Label(frame, text="0%")
        self.pb_label.pack(side=tk.RIGHT, padx=(12, 0))
        # self.pb['value'] = 10.0

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
        self.decoder = FirmwareDecoderContext(self.fw.data)
        self.creator_segments = self.fw.get_all_segments()
        self.fw_label.configure(
            text=f"{os.path.basename(path)}  ({self.fw.segment_count} segments)"
        )
        version = self.fw.version
        self.version_label.configure(text=version)
        self.version_str_var.initialize(version)
        self.save_btn.configure(state=tk.NORMAL)
        self._log(f"Loaded {path}  —  {self.fw.segment_count} audio segments found")
        self._populate_segments()
        self._populate_creator()

    # ------------------------------------------------------------------ save
    def _save_new_fw(self):
        print(self.version_str_var.get())
        new_fw = self.fw.patch_with_new_segments(
            self.creator_segments, self.version_str_var.get()
        )

        path = filedialog.asksaveasfilename(
            title="Save Patched Firmware",
            defaultextension=".bin",
            initialfile="patched_fw.bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(new_fw.data)
        self._log(f"Saved patched firmware to {path}")

    # ------------------------------------------------------------------ rows
    def _selected_profile(self) -> EncodingProfile:
        return ENCODING_PRESETS.get(self.quality_var.get(), ENCODING_BEST)

    def _update_space_indicator(self):
        total_used = sum(len(seg) for seg in self.creator_segments)
        if hasattr(self, "fw"):
            max_bytes = max(
                1, VPE_AUDIO_DATA_LIMIT - self.fw.audiolib_header.audiodata_start
            )
            pct = min(100.0, (total_used / max_bytes) * 100.0)
            remaining_bytes = max(0, max_bytes - total_used)
            remaining_secs = self._selected_profile().estimate_duration_secs(
                remaining_bytes
            )
            self.pb["value"] = pct
            self.pb_label.configure(text=f"{pct:.1f}% ({total_used}/{max_bytes} bytes)")
            self.remaining_label.configure(text=f"~{remaining_secs:.1f}s left")
        else:
            self.pb["value"] = 0
            self.pb_label.configure(text=f"0% ({total_used} bytes)")
            self.remaining_label.configure(text="~0.0s left")

    def _populate_segments(self):
        seg_count = len(self.fw.seg_entries)
        # Grow or shrink row cache
        while len(self._segment_rows) < seg_count:
            row_widgets = SegmentRowWidgets(self.seg_inner, len(self._segment_rows) + 1)
            row_widgets.grid()
            self._segment_rows.append(row_widgets)
        while len(self._segment_rows) > seg_count:
            row = self._segment_rows.pop()
            row.grid_remove()

        for idx, entry in enumerate(self.fw.seg_entries):
            seg = self.fw.get_segment(idx)
            row_widgets = self._segment_rows[idx]
            codec = seg.codec
            hdr = seg.get_header()
            if isinstance(hdr, VpeSegmentHeader):
                details = f"{hdr.samplerate // 1000}kHz ({hdr.bitrate}bps {hdr.num_frames}fr {hdr.duration_secs:.1f}s)"
            elif isinstance(hdr, DPCMSegmentHeader):
                details = f"{hdr.samplerate // 1000}kHz"
            else:
                details = "Unpopulated"
            size, unit = size_to_unit(len(seg))

            sound_name = RfUnitSound(idx)
            # Update labels and buttons with clear named attributes
            row_widgets.idx_label["text"] = f"{str(idx)} ({sound_name.name})"
            row_widgets.codec_label["text"] = codec.name
            row_widgets.offset_label["text"] = f"0x{entry.start:05X}"
            row_widgets.size_label["text"] = f"{size:.2f} {unit}"
            row_widgets.details_label["text"] = details
            row_widgets.play_btn["command"] = lambda s=seg: self._play_seg(s)
            row_widgets.extract_wav_btn["command"] = lambda s=seg, i=idx: (
                self._extract_seg_wav(i, s)
            )
            row_widgets.extract_raw_btn["command"] = lambda s=seg, i=idx: (
                self._extract_seg_raw(i, s)
            )
            row_widgets.grid(row=idx + 1)

    def _populate_creator(self):
        seg_count = len(self.creator_segments)
        # Grow or shrink row cache
        while len(self._creator_rows) < seg_count:
            row_widgets = CreatorRowWidgets(
                self.creator_inner, len(self._creator_rows) + 1
            )
            row_widgets.grid()
            self._creator_rows.append(row_widgets)
        while len(self._creator_rows) > seg_count:
            row = self._creator_rows.pop()
            row.grid_remove()

        for idx, seg in enumerate(self.creator_segments):
            row_widgets = self._creator_rows[idx]
            codec = seg.codec
            hdr = seg.get_header()
            if isinstance(hdr, VpeSegmentHeader):
                details = f"{hdr.samplerate // 1000}kHz ({hdr.bitrate}bps {hdr.num_frames}fr {hdr.duration_secs:.1f}s)"
            elif isinstance(hdr, DPCMSegmentHeader):
                details = f"{hdr.samplerate // 1000}kHz"
            else:
                details = "Unpopulated"
            size, unit = size_to_unit(len(seg))
            sound_name = RfUnitSound(idx)
            # Update labels and buttons with clear named attributes
            row_widgets.idx_label["text"] = f"{str(idx)} ({sound_name.name})"
            row_widgets.codec_label["text"] = codec.name
            row_widgets.size_label["text"] = f"{size:.2f} {unit}"
            row_widgets.details_label["text"] = details
            row_widgets.stub_btn["command"] = lambda i=idx: self._make_stub_seg(i)
            row_widgets.play_btn["command"] = lambda s=seg: self._play_seg(s)
            row_widgets.play_btn["state"] = (
                tk.DISABLED if len(seg.data) == 0 else tk.NORMAL
            )
            row_widgets.inject_wav_btn["command"] = lambda i=idx: self._inject_seg(i)
            row_widgets.inject_raw_btn["command"] = lambda i=idx: self._inject_seg_raw(
                i
            )
            row_widgets.grid(row=idx + 1)
        self._update_space_indicator()

    # ------------------------------------------------------------------ playback
    def _play_seg(self, seg: AudioSegment):
        self._log(f"Decoding and playing segment ({seg.codec})...")
        self._run_in_thread(self._do_play_seg, seg)

    def _do_play_seg(self, seg: AudioSegment):
        try:
            # Import sounddevice only when needed
            try:
                import sounddevice as sd
            except ImportError:
                self._log_ts(
                    "sounddevice not installed. Please install with 'pip install sounddevice'."
                )
                return

            samples, sr = self.decoder.decode_segment(seg)

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
    def _extract_seg_wav(self, index: int, seg: AudioSegment):
        codec = seg.codec
        default_name = f"segment_{index:02d}_{codec.name.replace('/', '_')}.wav"

        path = filedialog.asksaveasfilename(
            title=f"Extract Segment {index}",
            defaultextension=".wav",
            initialfile=default_name,
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return

        self._log(f"Extracting segment {index} ({codec})...")
        self._run_in_thread(self._do_extract_wav, seg, path)

    def _extract_seg_raw(self, index: int, seg: AudioSegment):
        codec = seg.codec
        default_name = f"segment_{index:02d}_.{codec.name.lower()}"

        path = filedialog.asksaveasfilename(
            title=f"Extract Segment {index}",
            defaultextension=".{codec.lower()}",
            initialfile=default_name,
        )
        if not path:
            return

        self._log(f"Extracting segment {index} ({codec})...")
        self._run_in_thread(self._do_extract_raw, seg, path)

    def _do_extract_raw(self, seg: AudioSegment, path: str):
        try:
            self._log_ts(f"  -> {path}")
            with open(path, "wb") as f:
                f.write(seg.data)
        except Exception as e:
            self._log_ts(f"  Extract error: {e}")
        self._log_ts("Done")

    def _do_extract_wav(self, seg: AudioSegment, path: str):
        try:
            samples, sr = self.decoder.decode_segment(seg)
            ISD9160Firmware._write_wav(path, samples, sr, normalize=False)
            dur = len(samples) / sr
            self._log_ts(f"  -> {path}  ({len(samples)} samples, {sr}Hz, {dur:.1f}s)")
        except Exception as e:
            self._log_ts(f"  Extract error: {e}")
        self._log_ts("Done")

    # ------------------------------------------------------------------ inject
    def _inject_seg(self, index: int):
        if not hasattr(self, "fw"):
            messagebox.showerror(
                "Error", "Open a firmware image before injecting WAV audio."
            )
            return

        path = filedialog.askopenfilename(
            title=f"Inject WAV into Segment {index}",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return

        self._run_in_thread(self._do_inject_wav, index, path)

    def _inject_seg_raw(self, index: int):
        path = filedialog.askopenfilename(
            title=f"Inject RAW into Segment {index}",
            filetypes=[("RAW files", "*.raw"), ("All files", "*.*")],
        )
        if not path:
            return

        self._log(f"Injecting RAW blob in segment {index}...")
        self._run_in_thread(self._do_inject_raw, index, path)

    def _make_stub_seg(self, index: int):
        profile = self._selected_profile()
        self.creator_segments[index] = AudioSegment.vpe_stub(profile)
        hdr = self.creator_segments[index].get_header()
        if isinstance(hdr, VpeSegmentHeader):
            self._log(
                f"Segment {index} converted to VPE stub ({hdr.samplerate} Hz, {hdr.num_frames} frame, {len(self.creator_segments[index])} bytes)"
            )
        self.root.after(0, self._populate_creator)

    def _do_inject_wav(self, index: int, wav_path: str):
        try:
            encoding_profile = self._selected_profile()
            encoder = VPEEncoder(self.fw.data)
            audio_segment = encoder.encode_wav_into_audio_segment(
                wav_path, encoding_profile
            )
            self.creator_segments[index] = audio_segment
            self._log_ts(
                f"  Segment {index} injected OK, resampled to {encoding_profile.samplerate} Hz mono)"
            )
            self.root.after(0, self._populate_creator)
        except Exception as e:
            self._log_ts(f"  Inject WAV error: {e}")

    def _do_inject_raw(self, index: int, raw_path: str):
        try:
            with open(raw_path, "rb") as f:
                data = f.read()

            self.creator_segments[index] = AudioSegment(data)
            self._log_ts(f"  Segment {index} injected OK  ({len(data)} bytes replaced)")
            self.root.after(0, self._populate_creator)
        except Exception as e:
            self._log_ts(f"  Inject RAW error: {e}")

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
