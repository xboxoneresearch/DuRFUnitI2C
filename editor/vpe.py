"""
ISD9160 VPE/DPCM Audio Extractor
Extracts audio segments from Nuvoton ISD9160 firmware dumps and converts to WAV.

Supports:
  - VPE/Siren7 codec (segment first byte & 0x1F == 0x1D or 0x1E)
  - DPCM codec (all other segment types)

Usage:
    python vpe_extract.py <firmware.bin> [output_dir]
"""
from __future__ import annotations
import argparse
import copy
import math
import os
import struct
import traceback
import wave
from dataclasses import dataclass
from enum import Enum

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from scipy.fft import dct as scipy_dct
    from scipy.signal import resample_poly as scipy_resample_poly

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# ============================================================================
# Constants from firmware analysis
# ============================================================================

# VPE Firmware Header at 0x8000
VPE_HEADER_ADDR = 0x8000
VPE_DATA_BOUNDARY_ADDR = VPE_HEADER_ADDR + 0x0C
VPE_AUDIO_DATA_LIMIT = 0x23000
VPE_MAGIC = 0x1155AAFF
DATA_SECTION_END = 0x233FF

# Key table addresses within firmware (mapped from Ghidra analysis)
ADDR_QUANTIZER_STEPS = 0x9354  # 8 bytes: step sizes for categories 0-7
ADDR_SF_HUFFMAN_TABLE = 0x8D4C  # Scale factor difference Huffman table
ADDR_HUFFMAN_TREE_PTRS = 0xA730  # 7 x uint32 pointers to category Huffman trees
ADDR_COEFS_PER_VECTOR = 0xA7CC  # 8 bytes: coefficients per vector per category
ADDR_HUFFMAN_LENGTHS = 0xA7D4  # 8 bytes: Huffman code lengths per category
ADDR_DEQUANT_TABLE = 0x9254  # Dequantization reconstruction levels
ADDR_NOISE_FILL = 0xAF74  # Noise fill level values
ADDR_DCT_COEF_MATRIX = 0xAF7C  # 10-point DCT coefficient matrix
ADDR_WINDOW_COEFS_32K = 0xA7F4  # Synthesis window for 32kHz
ADDR_WINDOW_COEFS_16K = 0xAA74  # Synthesis window for 16kHz
ADDR_IMDCT_TWIDDLE_16K = 0xB184  # IMDCT twiddle factors 16kHz
ADDR_IMDCT_TWIDDLE_32K = 0xB2C4  # IMDCT twiddle factors 32kHz
ADDR_BUTTERFLY_PTRS = 0xB544  # 6 x uint32 pointers to butterfly stage data
ADDR_SUBBAND_COEFS = 0x9000  # Subband coefficient lookup tables
ADDR_QUANTIZER_TABLE = 0xA74C  # Quantizer/scale factor lookup table

# DPCM tables
ADDR_DPCM_TABLES = 0x7090  # DPCM step/prediction tables (from DAT_00000674)

# ============================================================================
# Dataclasses
# ============================================================================


class Codec(Enum):
    UNKNOWN = 0
    VPE = 1
    DPCM = 2

@dataclass
class LibrarySegEntry:
    start: int
    end: int

    @classmethod
    def from_bytes(cls, buf: bytes) -> "LibrarySegEntry":
        deserialized = struct.unpack_from(LibrarySegEntry.struct_format(), buf, 0)
        return cls(*deserialized)

    def to_bytes(self) -> bytes:
        return struct.pack(
            LibrarySegEntry.struct_format(),
            self.start,
            self.end,
        )

    @staticmethod
    def struct_format() -> str:
        return "<II"

    def __len__(self) -> int:
        return 8

@dataclass
class AudioSegment:
    data: bytes

    def get_header(self) -> DPCMSegmentHeader | VpeSegmentHeader | None:
        if self.is_vpe:
            return VpeSegmentHeader.from_bytes(self.data)
        elif self.is_dpcm:
            return DPCMSegmentHeader.from_bytes(self.data)
        else:
            return None

    def __len__(self) -> int:
        return len(self.data)

    def is_empty(self) -> bool:
        return len(self) == 0

    @classmethod
    def empty(cls) -> AudioSegment:
        return cls(b"")

    @classmethod
    def vpe_stub(cls, profile: EncodingProfile) -> AudioSegment:
        header = VpeSegmentHeader(
            profile.first_byte,
            0xFF,
            1,
            profile.bitrate,
            profile.subtype,
            profile.bits_per_frame,
            profile.num_subbands,
            profile.samples_per_frame,
        )
        return cls(header.to_bytes() + (b"\xFF" * profile.bytes_per_frame))

    @property
    def codec_type(self) -> int:
        if len(self.data) > 0:
            return self.data[0] & 0x1F
        return 0

    @property
    def codec(self) -> Codec:
        if self.codec_type == 0:
            return Codec.UNKNOWN
        elif self.codec_type in (0x1D, 0x1E):
            return Codec.VPE
        else:
            return Codec.DPCM

    @property
    def is_vpe(self) -> bool:
        return self.codec == Codec.VPE

    @property
    def is_dpcm(self) -> bool:
        return self.codec == Codec.DPCM

@dataclass
class DPCMContext:
    bits_per_sample: int
    mask: int
    step_table_base: int
    adapt_table: int
    max_step_index: int
    coef_idx1: int
    coef_idx2: int
    step_index: int
    predictor: int
    current_sample: int
    prev_sample: int
    max_clamp: int
    min_clamp: int


@dataclass
class VpeFrameParams:
    scale_factors: list[int]
    quant_indices: list[int]
    shift: int
    power_cat: int
    cat_bits: int
    spectral_bits_budget: int
    categories: list[int]


@dataclass
class DPCMSegmentHeader:
    # Codec type → sample rate (from codec_dispatcher_init switch table)
    SAMPLE_RATES = {
        0: 4000,
        1: 5300,
        2: 6400,
        3: 8000,
        4: 12000,
        5: 16000,
        6: 32000,
        7: 16000,
    }

    first_byte: int

    @property
    def codec_type(self) -> int:
        return (self.first_byte >> 5) & 0x7

    @property
    def bottom_bits(self) -> int:
        return self.first_byte & 0x1F

    @property
    def samplerate(self) -> int:
        return self.SAMPLE_RATES.get(self.codec_type, 8000)

    @property
    def use_stream_control(self) -> bool:
        return self.bottom_bits == 0x1C

    @classmethod
    def from_bytes(cls, buf: bytes) -> "DPCMSegmentHeader":
        deserialized = struct.unpack_from(DPCMSegmentHeader.struct_format(), buf, 0)
        return cls(*deserialized)

    def to_bytes(self) -> bytes:
        return struct.pack(
            DPCMSegmentHeader.struct_format(),
            self.first_byte
        )

    @staticmethod
    def struct_format() -> str:
        return "<B"

    def __len__(self) -> int:
        return 1

@dataclass
class VpeSegmentHeader:
    # u8
    codec_byte: int
    # u8
    unknown: int
    # int16
    num_frames: int
    # int32
    bitrate: int
    # int16
    codec_subtype: int
    # int16
    bits_per_frame: int
    # int16
    num_subbands: int
    # int16
    samples_per_frame: int

    @property
    def samplerate(self) -> int:
        return 32000 if self.num_subbands > 14 else 16000

    @property
    def duration_secs(self) -> float:
        if self.samplerate:
            return self.num_frames * self.samples_per_frame / self.samplerate
        return 0.0

    @property
    def bytes_per_frame(self) -> int:
        return self.bits_per_frame // 8

    @classmethod
    def from_bytes(cls, buf: bytes) -> "VpeSegmentHeader":
        deserialized = struct.unpack_from(VpeSegmentHeader.struct_format(), buf, 0)
        return cls(*deserialized)

    def to_bytes(self) -> bytes:
        return struct.pack(
            VpeSegmentHeader.struct_format(),
            self.codec_byte,
            self.unknown,
            self.num_frames,
            self.bitrate,
            self.codec_subtype,
            self.bits_per_frame,
            self.num_subbands,
            self.samples_per_frame
        )

    @staticmethod
    def struct_format() -> str:
        return "<2BhI4h"

    def __len__(self) -> int:
        return 16

@dataclass
class EncodingProfile:
    first_byte: int
    subtype: int
    bitrate: int
    num_subbands: int
    bits_per_frame: int
    samples_per_frame: int
    # scale factor offset
    sf_offset: float

    @property
    def samplerate(self) -> int:
        return 32000 if self.num_subbands > 14 else 16000

    @property
    def bytes_per_frame(self) -> int:
        return self.bits_per_frame // 8

    @property
    def bytes_per_second(self) -> float:
        return self.bytes_per_frame * self.samplerate / self.samples_per_frame

    def estimate_duration_secs(self, available_bytes: int) -> float:
        payload_bytes = max(0, available_bytes - len(VpeSegmentHeader(0, 0, 0, 0, 0, 0, 0, 0)))
        if self.bytes_per_frame <= 0 or self.samples_per_frame <= 0 or self.samplerate <= 0:
            return 0.0
        frame_count = payload_bytes // self.bytes_per_frame
        return frame_count * self.samples_per_frame / self.samplerate

ENCODING_32KHZ = EncodingProfile(0xDE, 14000, 48000, 28, 960, 640, 1.0)
ENCODING_16KHZ = EncodingProfile(0xBD, 14000, 16000, 14, 320, 320, 1.0)
ENCODING_PRESETS = {
    "32 kHz / 48 kbps VPE": ENCODING_32KHZ,
    "16 kHz / 16 kbps VPE": ENCODING_16KHZ,
}
ENCODING_BEST = ENCODING_32KHZ

# ============================================================================
# Bitstream Reader (matches vpe_read_bit behavior)
# ============================================================================


class VPEBitstreamReader:
    """MSB-first 16-bit word bitstream reader matching VPE firmware behavior."""

    def __init__(self, data):
        self.data = data
        self.pos = 0  # byte position in data
        self.bits_left = 0  # bits remaining in current word
        self.current_word = 0  # current 16-bit word
        self.total_bits = len(data) * 8

    def read_bit(self) -> int:
        if self.bits_left == 0:
            if self.pos + 1 >= len(self.data):
                return 0
            self.current_word = struct.unpack_from("<H", self.data, self.pos)[0]
            self.pos += 2
            self.bits_left = 16
        self.bits_left -= 1
        bit = (self.current_word >> self.bits_left) & 1
        return bit

    def read_bits(self, n) -> int:
        val = 0
        for _ in range(n):
            val = (val << 1) | self.read_bit()
        return val


class VPEBitstreamWriter:
    """MSB-first 16-bit word bitstream writer matching VPE firmware behavior.

    The firmware frames appear to be padded with 1-bits (0xFF fill) for any unused tail bits.
    We match that by initializing the buffer to 0xFF and only clearing bits as needed.
    """

    def __init__(self, total_bits):
        self.total_bits = int(total_bits)
        self.bitpos = 0
        self.data = bytearray([0xFF] * ((self.total_bits + 7) // 8))

    def write_bit(self, bit) -> None:
        if self.bitpos >= self.total_bits:
            raise ValueError("Bitstream overflow")
        b = 1 if bit else 0

        word_idx = self.bitpos // 16
        bit_in_word = 15 - (self.bitpos % 16)
        if bit_in_word >= 8:
            byte_idx = word_idx * 2 + 1
            bit_in_byte = bit_in_word - 8
        else:
            byte_idx = word_idx * 2
            bit_in_byte = bit_in_word

        mask = 1 << bit_in_byte
        if b:
            self.data[byte_idx] |= mask
        else:
            self.data[byte_idx] &= (~mask) & 0xFF

        self.bitpos += 1

    def write_bits(self, value, n) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        # MSB-first
        for i in range(n - 1, -1, -1):
            self.write_bit((value >> i) & 1)

    def get_bytes(self) -> bytes:
        return bytes(self.data)


class DPCMBitstreamReader:
    """MSB-first byte-level bitstream reader for DPCM codec."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.bits_left = 8

    def read_bits(self, n: int) -> int:
        result = 0
        bits_needed = n
        while bits_needed > 0:
            if self.pos >= len(self.data):
                break  # Return partial result (zero-padded) at end of data
            if bits_needed <= self.bits_left:
                shift = self.bits_left - bits_needed
                result |= (self.data[self.pos] >> shift) & ((1 << bits_needed) - 1)
                self.bits_left -= bits_needed
                if self.bits_left == 0:
                    self.pos += 1
                    self.bits_left = 8
                bits_needed = 0
            else:
                result |= (self.data[self.pos] & ((1 << self.bits_left) - 1)) << (
                    bits_needed - self.bits_left
                )
                bits_needed -= self.bits_left
                self.pos += 1
                self.bits_left = 8
        return result

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.data)


# ============================================================================
# VPE/Siren7 Decoder
# ============================================================================


class VPEDecoder:
    """
    VPE (Voice Processing Engine) decoder for Nuvoton ISD9160.
    Implements the Siren7/G.722.1-compatible codec from decompiled firmware.
    """

    # Bits per subband for each category (0-7), from firmware at 0x9354
    QUANT_STEPS = [52, 47, 43, 37, 29, 22, 16, 0]

    # Noise fill levels per category: [cat5, cat6, cat7] from firmware at 0xAF74
    NOISE_LEVELS = {5: 5793, 6: 8192, 7: 23170}

    def __init__(self, firmware_data: bytes, debug_enabled: bool = False):
        self.fw = firmware_data
        self.debug_enabled = debug_enabled
        self.last_debug_report = None
        self._load_tables()

    def _read_i8(self, addr) -> int:
        v = self.fw[addr]
        return v if v < 128 else v - 256

    def _read_u8(self, addr) -> int:
        return self.fw[addr]

    def _read_i16(self, addr) -> int:
        return struct.unpack_from("<h", self.fw, addr)[0]

    def _read_u16(self, addr) -> int:
        return struct.unpack_from("<H", self.fw, addr)[0]

    def _read_u32(self, addr) -> int:
        return struct.unpack_from("<I", self.fw, addr)[0]

    def _load_tables(self) -> None:
        """Load all VPE codec tables from firmware."""
        # Huffman tree pointers (7 categories, for cats 0-6)
        self.huffman_tree_addrs = [
            self._read_u32(ADDR_HUFFMAN_TREE_PTRS + i * 4) for i in range(7)
        ]

        # Coefficients per vector for each category
        self.coefs_per_vector = [
            self._read_u8(ADDR_COEFS_PER_VECTOR + i) for i in range(8)
        ]

        # Huffman code lengths (num vectors) per category
        self.huffman_lengths = [
            self._read_u8(ADDR_HUFFMAN_LENGTHS + i) for i in range(8)
        ]

        # Butterfly twiddle factor pointers (6 stages)
        self.butterfly_ptrs = [
            self._read_u32(ADDR_BUTTERFLY_PTRS + i * 4) for i in range(6)
        ]

    def _clamp16(self, val) -> int:
        if val > 32767:
            return 32767
        if val < -32768:
            return -32768
        return int(val)

    def _i16(self, val) -> int:
        """Truncate to signed 16-bit."""
        val = int(val) & 0xFFFF
        return val if val < 32768 else val - 65536

    def _format_debug_report(self, summary, flagged_frames, first_frame) -> str:
        lines = [
            "VPE Debug Report",
            f"bitrate={summary['bitrate']}",
            f"subbands={summary['num_subbands']}",
            f"bits_per_frame={summary['bits_per_frame']}",
            f"samples_per_frame={summary['samples_per_frame']}",
            f"num_frames={summary['num_frames']}",
            f"bytes_per_frame={summary['bytes_per_frame']}",
            f"decode_errors={summary['decode_errors']}",
            f"flagged_frames={summary['flagged_frames']}",
            f"flag_zero_tail={summary['flag_zero_tail']}",
            f"flag_negative_bits={summary['flag_negative_bits']}",
            f"flag_sf_oob={summary['flag_sf_oob']}",
            f"spectral_errors={summary['spectral_errors']}",
            f"min_bits_after_spectral={summary['min_bits_after_spectral']}",
            f"max_bits_after_spectral={summary['max_bits_after_spectral']}",
            "",
            "First Frame",
        ]
        if first_frame is None:
            lines.append("missing=1")
        else:
            lines.extend(
                [
                    f"frame_idx={first_frame['frame_idx']}",
                    f"frame_bytes_hex={first_frame['frame_bytes_hex']}",
                    f"bits_left_after_sf={first_frame['bits_left_after_sf']}",
                    f"power_cat={first_frame['power_cat']}",
                    f"available_bits={first_frame['available_bits']}",
                    f"offset={first_frame['offset']}",
                    f"shift={first_frame['shift']}",
                    f"bits_after_spectral={first_frame['bits_after_spectral']}",
                    f"tail_zero_seen={int(first_frame['tail_zero_seen'])}",
                    f"neg_bits={int(first_frame['neg_bits'])}",
                    f"sf_oob={int(first_frame['sf_oob'])}",
                    f"spectral_error={int(first_frame['spectral_error'])}",
                    f"scale_factors={','.join(str(v) for v in first_frame['scale_factors'])}",
                    f"quant_indices={','.join(str(v) for v in first_frame['quant_indices'])}",
                    f"categories={','.join(str(v) for v in first_frame['categories'])}",
                    f"reorder={','.join(str(v) for v in first_frame['reorder'])}",
                    f"spectral_head={','.join(str(v) for v in first_frame['spectral_head'])}",
                    f"output_head={','.join(str(v) for v in first_frame['output_head'])}",
                    f"history_head={','.join(str(v) for v in first_frame['history_head'])}",
                ]
            )
        lines.extend(
            [
                "",
                "frame_idx,bits_after_spectral,tail_zero_seen,neg_bits,sf_oob,spectral_error,shift,power_cat",
            ]
        )
        for frame in flagged_frames:
            lines.append(
                f"{frame['frame_idx']},{frame['bits_after_spectral']},"
                f"{int(frame['tail_zero_seen'])},{int(frame['neg_bits'])},"
                f"{int(frame['sf_oob'])},{int(frame['spectral_error'])},"
                f"{frame['shift']},{frame['power_cat']}"
            )
        return "\n".join(lines) + "\n"

    # --- Scale Factor Decoding (vpe_decode_scale_factors) ---

    def _decode_scale_factors(self, bs, num_subbands, bits_per_frame):
        """Decode scale factor indices and quantizer levels from bitstream."""
        # Read first scale factor: 5 bits, subtract 7
        val = 0
        for _ in range(5):
            val = val * 2 + bs.read_bit()
        bits_consumed = 5
        sf_diffs = [0] * num_subbands
        sf_diffs[0] = val - 7

        # Decode remaining via Huffman (delta coding)
        for i in range(1, num_subbands):
            node = 0
            while True:
                bit = bs.read_bit()
                bits_consumed += 1
                idx = bit + node * 2 + i * 46
                node = self._read_i8(ADDR_SF_HUFFMAN_TABLE + idx)
                if node <= 0:
                    break
            sf_diffs[i] = -node

        # Convert diffs to absolute scale factors
        scale_factors = [0] * num_subbands
        scale_factors[0] = sf_diffs[0]
        for i in range(1, num_subbands):
            scale_factors[i] = self._i16(scale_factors[i - 1] + sf_diffs[i] - 12)

        # Compute shift factor for dequantization power levels
        max_sf = 0
        total_bits_used = 0
        for i in range(num_subbands):
            sf_idx = self._i16(scale_factors[i] + 24)
            if sf_idx > max_sf:
                max_sf = sf_idx
            total_bits_used = self._i16(
                total_bits_used + self._read_i16(ADDR_QUANTIZER_TABLE + sf_idx * 2)
            )

        shift = 9
        while shift >= 0:
            if total_bits_used < 8 and max_sf <= 28:
                break
            shift -= 1
            total_bits_used >>= 1
            max_sf = self._i16(max_sf - 2)

        # Compute quantizer indices (dequantization power levels)
        quant_indices = [0] * num_subbands
        for i in range(num_subbands):
            idx = self._i16(scale_factors[i] + shift * 2 + 24)
            quant_indices[i] = self._read_i16(ADDR_QUANTIZER_TABLE + idx * 2)

        bits_remaining = bits_per_frame - bits_consumed
        return scale_factors, quant_indices, shift, bits_remaining

    # --- MLT Categorization (vpe_find_quantizer + vpe_compute_sf_indices) ---

    def _find_categorization_offset(
        self, scale_factors, num_subbands, available_bits
    ) -> int:
        """Binary search for MLT categorization offset (vpe_find_quantizer).
        Returns offset value used to compute per-subband categories."""
        low = -32
        range_val = 32
        while range_val != 0:
            mid = self._i16(low + range_val)
            total = 0
            for i in range(num_subbands):
                cat = self._i16(mid - scale_factors[i]) >> 1
                cat = max(0, min(7, cat))
                total = self._i16(total + self.QUANT_STEPS[cat])
            if total - available_bits + 32 >= 0:
                low = mid
            range_val >>= 1
        return self._i16(low)

    def _compute_categories(self, scale_factors, num_subbands, offset) -> list[int]:
        """Compute initial per-subband categories 0-7 (vpe_compute_sf_indices)."""
        categories = [0] * num_subbands
        for i in range(num_subbands):
            cat = (offset - scale_factors[i]) >> 1
            categories[i] = max(0, min(7, cat))
        return categories

    # --- Bit Balancing (vpe_balance_bits) ---

    def _balance_bits(
        self,
        categories,
        scale_factors,
        offset,
        available_bits,
        num_subbands,
        num_iterations,
    ) -> list:
        """Iteratively rebalance bit allocation across subbands.
        Modifies categories in-place. Returns reorder sequence."""
        # Firmware meaning: lower category => more bits, higher category => fewer bits.
        #
        # This follows Halo's vpe_balance_bits:
        # - Maintain two copies:
        #   - down_cats (in-place `categories`): decreases allocate more bits
        #   - up_cats (local copy): increases free bits
        # - Fill an index reorder buffer around the midpoint.
        up_cats = categories[:]

        total_down = sum(self.QUANT_STEPS[c] for c in categories)
        total_up = total_down

        reorder_buf = [0] * (num_iterations * 2)
        up_idx = num_iterations  # upward writes start at midpoint
        down_idx = num_iterations - 1  # downward writes start just below midpoint

        for _ in range(num_iterations - 1):
            # Halo condition: (total_down + total_up - 2*available_bits < 1)
            if (total_down + total_up - (available_bits * 2)) < 1:
                # Under budget: allocate more bits by *decreasing* a category in down_cats.
                best_score = 99
                best_band = 0
                for band in range(num_subbands):
                    cat = categories[band]
                    if cat > 0:
                        score = self._i16(offset - scale_factors[band] - cat * 2)
                        if score < best_score:
                            best_score = score
                            best_band = band

                reorder_buf[down_idx] = best_band
                down_idx -= 1

                old_step = self.QUANT_STEPS[categories[best_band]]
                categories[best_band] -= 1
                new_step = self.QUANT_STEPS[categories[best_band]]
                total_down = self._i16(total_down - old_step + new_step)
            else:
                # Over budget: free bits by *increasing* a category in up_cats.
                best_score = -99
                best_band = 0
                for band in range(num_subbands - 1, -1, -1):
                    cat = up_cats[band]
                    if cat < 7:
                        score = self._i16(offset - scale_factors[band] - cat * 2)
                        if score > best_score:
                            best_score = score
                            best_band = band

                reorder_buf[up_idx] = best_band
                up_idx += 1

                old_step = self.QUANT_STEPS[up_cats[best_band]]
                up_cats[best_band] += 1
                if up_cats[best_band] > 7:
                    up_cats[best_band] = 7
                new_step = self.QUANT_STEPS[up_cats[best_band]]
                total_up = self._i16(total_up - old_step + new_step)

        # Extract final reorder sequence.
        start = down_idx + 1
        return reorder_buf[start : start + (num_iterations - 1)]

    # --- Power Category Adjustment (vpe_build_histogram) ---

    def _apply_power_category(self, power_cat, categories, reorder) -> None:
        """Apply power_category adjustments to categories using reorder sequence."""
        for i in range(power_cat):
            if i < len(reorder):
                band = reorder[i]
                if band < len(categories):
                    categories[band] = min(7, categories[band] + 1)

    # --- Spectral Decoding (vpe_decode_spectral) ---

    def _decode_spectral(
        self,
        bs,
        num_subbands,
        quant_indices,
        categories,
        spectral,
        prng_state,
        bits_remaining,
    ) -> tuple[int, bool]:
        """Decode spectral coefficients per subband."""
        error_flag = False

        for band in range(num_subbands):
            cat = categories[band]
            q_level = quant_indices[band]
            out_offset = band * 20  # 20 coefficients per subband

            if cat < 7 and not error_flag:
                # Huffman decode for categories 0-6
                tree_addr = self.huffman_tree_addrs[cat]
                num_vectors = self.huffman_lengths[cat]
                cpv = self.coefs_per_vector[cat]

                for vec in range(num_vectors):
                    if error_flag:
                        break
                    # Walk Huffman tree
                    node = 0
                    while True:
                        if bits_remaining < 1:
                            error_flag = True
                            break
                        bit = bs.read_bit()
                        bits_remaining -= 1
                        if bit == 0:
                            val = self._read_i16(tree_addr + node * 4)
                        else:
                            val = self._read_i16(tree_addr + node * 4 + 2)
                        if val <= 0:
                            symbol = -val
                            break
                        node = val

                    if error_flag:
                        break

                    # Unpack coefficients from symbol
                    coefs = self._unpack_coefs(symbol, cat)

                    # Count sign bits needed
                    sign_bits_needed = sum(1 for c in coefs if c != 0)
                    if bits_remaining < sign_bits_needed:
                        error_flag = True
                        break

                    # Read sign bits as single value
                    sign_val = 0
                    if sign_bits_needed > 0:
                        sign_val = bs.read_bits(sign_bits_needed)
                        bits_remaining -= sign_bits_needed

                    # Apply signs and dequantize
                    sign_mask = (
                        1 << (sign_bits_needed - 1) if sign_bits_needed > 0 else 0
                    )
                    for c_idx in range(cpv):
                        dq_val = self._i16(
                            (q_level * self._get_dequant(cat, coefs[c_idx])) >> 12
                        )
                        if dq_val != 0:
                            if (sign_val & sign_mask) == 0:
                                dq_val = -dq_val
                            sign_mask >>= 1
                        pos = out_offset + vec * cpv + c_idx
                        if pos < len(spectral):
                            spectral[pos] = dq_val

                # On error, set remaining subbands to cat 7
                if error_flag:
                    for b in range(band + 1, num_subbands):
                        categories[b] = 7

            # Noise fill for categories 5-6 (after Huffman decode)
            if cat in (5, 6) and not error_flag:
                noise_level = self._i16((q_level * self.NOISE_LEVELS[cat]) >> 15)
                neg_noise = self._i16(-noise_level)
                # Fill zeros with PRNG noise, 10 at a time (2 PRNG calls)
                for half in range(2):
                    rv = self._prng(prng_state)
                    for c_idx in range(10):
                        pos = out_offset + half * 10 + c_idx
                        if pos < len(spectral) and spectral[pos] == 0:
                            spectral[pos] = noise_level if (rv & 1) else neg_noise
                            rv >>= 1

            # Category 7 or error: pure noise fill
            if cat == 7 or (error_flag and cat < 7):
                noise_level = self._i16((q_level * self.NOISE_LEVELS[7]) >> 15)
                neg_noise = self._i16(-noise_level)
                # 2 PRNG calls, 10 coefs each
                for half in range(2):
                    rv = self._prng(prng_state)
                    for c_idx in range(10):
                        pos = out_offset + half * 10 + c_idx
                        if pos < len(spectral):
                            spectral[pos] = noise_level if (rv & 1) else neg_noise
                            rv >>= 1

        return bits_remaining, error_flag

    # Number of quantization levels per category, derived from dequant table at 0x9254.
    # Each category's dequant subtable has this many valid entries (indices 0 to N-1).
    NUM_LEVELS = [14, 10, 7, 5, 4, 3, 2, 2]

    def _unpack_coefs(self, symbol, category) -> list[int]:
        """Unpack Huffman-decoded symbol into coefficient vector using base-N decoding.
        Coefficients are packed MSB-first: the most significant digit of the
        base-N representation corresponds to the first coefficient in the vector.
        The number of levels N per category is determined by the dequant table structure."""
        cpv = self.coefs_per_vector[category]
        coefs = [0] * cpv

        if category >= 7:
            return coefs

        divisor = self.NUM_LEVELS[category]
        if divisor <= 1:
            return coefs

        for i in range(cpv - 1, -1, -1):
            coefs[i] = symbol % divisor
            symbol //= divisor

        return coefs

    def _get_dequant(self, category: int, index: int) -> int:
        """Get dequantization value for a category and index.
        Table layout: 8 categories x 16 entries x 2 bytes = 256 bytes at 0x9254."""
        if category < 8 and 0 <= index < 16:
            return self._read_i16(ADDR_DEQUANT_TABLE + category * 0x20 + index * 2)
        return 0

    def _prng(self, state: list[int]) -> int:
        """Pseudo-random number generator (vpe_prng).
        state is a list of 4 signed int16 values, modified in place.
        Returns int value for use as bit source."""
        new_val = self._i16(state[0] + state[3])
        if (new_val << 16) < 0:  # check bit 15 of 16-bit value
            new_val = self._i16(new_val + 1)
        state[3] = state[2]
        state[2] = state[1]
        state[1] = state[0]
        state[0] = new_val
        return new_val & 0xFFFF  # return as unsigned for bit extraction

    # --- IMDCT (DCT-IV) ---

    def _build_dct4_matrix(self, N):
        """Pre-compute DCT-IV basis matrix for given transform size."""
        if HAS_NUMPY:
            n = np.arange(N).reshape(-1, 1)
            k = np.arange(N).reshape(1, -1)
            return np.cos(math.pi / N * (n + 0.5) * (k + 0.5))
        else:
            matrix = []
            for n in range(N):
                row = []
                for k in range(N):
                    row.append(math.cos(math.pi / N * (n + 0.5) * (k + 0.5)))
                matrix.append(row)
            return matrix

    def _imdct(self, spectral, num_samples: int) -> list[int]:
        """Inverse MDCT via direct DCT-IV computation.
        Mathematically equivalent to the firmware's split-radix butterfly."""
        N = num_samples

        # Cache the DCT-IV matrix for this transform size
        if not hasattr(self, "_dct4_cache"):
            self._dct4_cache = {}
        if N not in self._dct4_cache:
            self._dct4_cache[N] = self._build_dct4_matrix(N)

        matrix = self._dct4_cache[N]

        # The firmware's split-radix IMDCT has implicit gain from:
        # - Forward butterflies (stage 0 halves, stages 1-3 double each) = ~4x
        # - 10-point DCT with int16 coefficients (~29400) / 32768 = ~0.9x per element
        # - Reverse butterflies with int16 twiddles * 4 / 65536 = ~1.1x per stage
        # Net gain is approximately sqrt(N/2) to match standard DCT-IV normalization

        if HAS_NUMPY:
            x = np.zeros(N, dtype=np.float64)
            slen = min(len(spectral), N)
            x[:slen] = np.array(spectral[:slen], dtype=np.float64)
            result = matrix @ x
            return [self._clamp16(int(round(v))) for v in result]
        else:
            x = [float(spectral[i]) if i < len(spectral) else 0.0 for i in range(N)]
            output = [0] * N
            for n in range(N):
                acc = 0.0
                for k in range(N):
                    acc += x[k] * matrix[n][k]
                output[n] = self._clamp16(int(round(acc)))
            return output

    # --- Synthesis Filter (vpe_synthesis_filter) ---

    # OLA gain factors derived via linear regression against MC reference.
    # The firmware's split-radix butterfly IMDCT combines windowing into
    # the butterfly stages, producing asymmetric effective gains for each
    # term (IMDCT*w[i], hist*w[319-i], etc.) rather than a single uniform
    # multiplier. These gain factors (as multiples of 1/65536) are stable
    # across all frame ranges (R^2 > 0.993):
    #   First half:  imdct*w[i]=4.645, hist*w[319-i]=6.137, hist*w[i]=4.102
    #   Second half: imdct*w[i]=6.189, hist*w[319-i]=-4.570, imdct*w[319-i]=4.029
    # Using fixed-point scaled by 1024 for integer arithmetic (>> 26 total).
    OLA_GAINS_1H = (4757, 6284, 4200)  # (imdct*w[i], hist*w[319-i], hist*w[i]) * 1024
    OLA_GAINS_2H = (
        6338,
        -4680,
        4126,
    )  # (imdct*w[i], -hist*w[319-i], imdct*w[319-i]) * 1024

    def _synthesis_filter(
        self, spectral, history, num_samples: int, shift: int
    ) -> tuple[list[int], list[int]]:
        """Synthesis filter bank: IMDCT + shift + windowed overlap-add.
        The shift normalizes amplitude across frames: q_level scales by
        2^shift (via quantizer table index += shift*2), and the post-IMDCT
        right-shift compensates, keeping all frames at consistent amplitude."""
        # Apply IMDCT
        imdct_out = self._imdct(spectral, num_samples)

        # Apply shift factor (normalizes amplitude across varying q_level scales)
        if shift > 0:
            for i in range(num_samples):
                imdct_out[i] = imdct_out[i] >> shift

        # Windowed overlap-add
        half = num_samples // 2
        if num_samples == 640:
            win_addr = ADDR_WINDOW_COEFS_32K
        else:
            win_addr = ADDR_WINDOW_COEFS_16K

        output = [0] * num_samples
        g1a, g1b, g1d = self.OLA_GAINS_1H
        g2a, g2b, g2c = self.OLA_GAINS_2H

        # First half: 3-term formula with asymmetric gains
        # output[i] = g1a * imdct[319-i]*w[i] + g1b * hist[i]*w[319-i] + g1d * hist[i]*w[i]
        for i in range(half):
            w_fwd = self._read_i16(win_addr + i * 2)  # w[i]
            w_rev = self._read_i16(win_addr + (half - 1 - i) * 2)  # w[319-i]
            h = history[i] if i < len(history) else 0
            imdct_val = imdct_out[half - 1 - i]

            acc = g1a * imdct_val * w_fwd + g1b * h * w_rev + g1d * h * w_fwd
            output[i] = self._clamp16((acc + (1 << 25)) >> 26)

        # Second half: 3-term formula with asymmetric gains
        # output[320+i] = g2a * imdct[i]*w[i] + g2b * hist[319-i]*w[319-i] + g2c * imdct[i]*w[319-i]
        for i in range(half):
            w_fwd = self._read_i16(win_addr + i * 2)  # w[i]
            w_rev = self._read_i16(win_addr + (half - 1 - i) * 2)  # w[319-i]
            h = (
                history[half - 1 - i]
                if half - 1 - i >= 0 and half - 1 - i < len(history)
                else 0
            )
            imdct_val = imdct_out[i]

            acc = g2a * imdct_val * w_fwd + g2b * h * w_rev + g2c * imdct_val * w_rev
            output[half + i] = self._clamp16((acc + (1 << 25)) >> 26)

        # Update history with second half of IMDCT output
        for i in range(half):
            if i < len(history):
                history[i] = imdct_out[half + i] if half + i < num_samples else 0

        return output, history

    # --- Main Frame Decoder ---

    def decode_segment(self, segment: AudioSegment) -> tuple[list[int], int]:
        """Decode a complete VPE audio segment to PCM samples."""
        self.last_debug_report = None

        if not segment.is_vpe:
            raise ValueError("Not a VPE segment")

        # Parse segment header
        header = VpeSegmentHeader.from_bytes(segment.data)
        assert header.unknown == 0xFF, (
            f"Unexpected value in header[1] byte. Expected: 0xFF, Got: {header.unknown:#02x}"
        )

        print(f"    Codec byte: {header.codec_byte}")
        print(f"    Unknown: {header.unknown}")
        print(f"    Codec subtype: {header.codec_subtype}")
        print(f"    Num Frames: {header.num_frames}")
        print(f"    Bitrate: {header.bitrate} bps")
        print(f"    Subbands: {header.num_subbands}")
        print(f"    Bits/frame: {header.bits_per_frame}")
        print(f"    Samples/frame: {header.samples_per_frame}")

        # Compressed data starts at offset +16
        compressed = segment.data[16:]
        bytes_per_frame = len(compressed) // header.num_frames

        print(f"    Bytes/frame: {bytes_per_frame}")
        print(f"    Segment length (w/o header): {len(compressed)}")

        # Determine sample rate from subbands
        if header.num_subbands <= 14:
            sample_rate = 16000
        else:
            sample_rate = 32000

        # Mode-dependent constants (from vpe_bitstream_decode)
        if header.num_subbands == 28:
            num_iterations = 32
            cat_bits = 5
        else:
            num_iterations = 16
            cat_bits = 4

        # Initialize decoder state
        history = [0] * (header.samples_per_frame // 2)
        prng_state = [1, 1, 1, 1]  # PRNG shift register state
        all_samples = []
        debug_summary = {
            "bitrate": header.bitrate,
            "num_subbands": header.num_subbands,
            "bits_per_frame": header.bits_per_frame,
            "samples_per_frame": header.samples_per_frame,
            "num_frames": header.num_frames,
            "bytes_per_frame": bytes_per_frame,
            "decode_errors": 0,
            "flagged_frames": 0,
            "flag_zero_tail": 0,
            "flag_negative_bits": 0,
            "flag_sf_oob": 0,
            "spectral_errors": 0,
            "min_bits_after_spectral": None,
            "max_bits_after_spectral": None,
        }
        flagged_frames = []
        first_frame = None

        for frame_idx in range(header.num_frames):
            frame_start = frame_idx * bytes_per_frame
            frame_end = frame_start + bytes_per_frame
            if frame_end > len(compressed):
                break

            frame_data = compressed[frame_start:frame_end]
            bs = VPEBitstreamReader(frame_data)

            try:
                # Step 1: Decode scale factors
                scale_factors, quant_indices, shift, bits_left = (
                    self._decode_scale_factors(
                        bs, header.num_subbands, header.bits_per_frame
                    )
                )

                # Step 2: Read power category bits
                power_cat = bs.read_bits(cat_bits)
                bits_left -= cat_bits

                # Step 3: Subband setup - compute available bits and cap surplus
                available_bits = bits_left
                base_samples = header.samples_per_frame  # 320 or 640
                if available_bits > base_samples:
                    surplus = (available_bits - base_samples) * 5
                    available_bits = self._i16(self._i16(surplus >> 3) + base_samples)

                # Step 4: Find categorization offset via binary search
                offset = self._find_categorization_offset(
                    scale_factors, header.num_subbands, available_bits
                )

                # Step 5: Compute initial categories (0-7)
                categories = self._compute_categories(
                    scale_factors, header.num_subbands, offset
                )

                # Step 6: Balance bits iteratively
                reorder = self._balance_bits(
                    categories,
                    scale_factors,
                    offset,
                    available_bits,
                    header.num_subbands,
                    num_iterations,
                )

                # Step 7: Apply power_category adjustments
                self._apply_power_category(power_cat, categories, reorder)

                # Step 8: Decode spectral coefficients
                spectral = [0] * (header.num_subbands * 20)
                bits_after_spectral, spectral_error = self._decode_spectral(
                    bs,
                    header.num_subbands,
                    quant_indices,
                    categories,
                    spectral,
                    prng_state,
                    bits_left,
                )

                tail_zero_seen = False
                if bits_after_spectral > 0:
                    for _ in range(bits_after_spectral):
                        if bs.read_bit() == 0:
                            tail_zero_seen = True
                            break

                neg_bits = bits_after_spectral < 0 and (
                    (power_cat - num_iterations) + 1 < 0
                )
                sf_oob = any((sf > 24 or sf < -15) for sf in scale_factors)
                if (
                    debug_summary["min_bits_after_spectral"] is None
                    or bits_after_spectral < debug_summary["min_bits_after_spectral"]
                ):
                    debug_summary["min_bits_after_spectral"] = bits_after_spectral
                if (
                    debug_summary["max_bits_after_spectral"] is None
                    or bits_after_spectral > debug_summary["max_bits_after_spectral"]
                ):
                    debug_summary["max_bits_after_spectral"] = bits_after_spectral

                if tail_zero_seen or neg_bits or sf_oob or spectral_error:
                    debug_summary["flagged_frames"] += 1
                    debug_summary["flag_zero_tail"] += int(tail_zero_seen)
                    debug_summary["flag_negative_bits"] += int(neg_bits)
                    debug_summary["flag_sf_oob"] += int(sf_oob)
                    debug_summary["spectral_errors"] += int(spectral_error)
                    if len(flagged_frames) < 64:
                        flagged_frames.append(
                            {
                                "frame_idx": frame_idx,
                                "bits_after_spectral": bits_after_spectral,
                                "tail_zero_seen": tail_zero_seen,
                                "neg_bits": neg_bits,
                                "sf_oob": sf_oob,
                                "spectral_error": spectral_error,
                                "shift": shift,
                                "power_cat": power_cat,
                            }
                        )

                # Step 9: Synthesis filter (IMDCT + window + overlap-add)
                output, history = self._synthesis_filter(
                    spectral, history, header.samples_per_frame, shift
                )

                if frame_idx == 0:
                    first_frame = {
                        "frame_idx": frame_idx,
                        "frame_bytes_hex": frame_data[:32].hex(),
                        "bits_left_after_sf": bits_left + cat_bits,
                        "power_cat": power_cat,
                        "available_bits": available_bits,
                        "offset": offset,
                        "shift": shift,
                        "bits_after_spectral": bits_after_spectral,
                        "tail_zero_seen": tail_zero_seen,
                        "neg_bits": neg_bits,
                        "sf_oob": sf_oob,
                        "spectral_error": spectral_error,
                        "scale_factors": scale_factors[:],
                        "quant_indices": quant_indices[:],
                        "categories": categories[:],
                        "reorder": reorder[:],
                        "spectral_head": spectral[:40],
                        "output_head": output[:32],
                        "history_head": history[:32],
                    }

                # Note: firmware clears 2 LSBs (& 0xFFFC) but this adds quantization
                # noise that gets amplified by WAV normalization; skip for cleaner output

                all_samples.extend(output)

            except Exception as e:
                # On decode error, output silence for this frame
                debug_summary["decode_errors"] += 1
                all_samples.extend([0] * header.samples_per_frame)
                if frame_idx < 5:
                    print(f"    Frame {frame_idx} decode error: {e}")
                    traceback.print_exc()

        if self.debug_enabled:
            self.last_debug_report = self._format_debug_report(
                debug_summary, flagged_frames, first_frame
            )
        return all_samples, sample_rate


# ============================================================================
# VPE/Siren7 Encoder (experimental, in-place replace)
# ============================================================================


class VPEEncoder:
    """
    VPE/Siren7 (G.722.1 / Annex C) encoder for in-place segment replacement.

    Current strategy (pragmatic, stable for patching fixed-size slots):
      - Reuse per-frame scale-factor coding and power_cat values from the original firmware frames
        to keep the bit-allocation behavior identical to the target firmware.
      - Compute MLT/IMDCT-domain samples from PCM using the decoder's overlap-add model (1-frame lookahead),
        then invert the DCT-IV to obtain target spectral coefficients.
      - Quantize+Huffman encode spectral vectors to fit within the per-frame bit budget, padding tail bits with 1s.
    """

    def __init__(self, firmware_data):
        if not HAS_NUMPY:
            raise RuntimeError("VPE encoding requires numpy")
        if not HAS_SCIPY:
            raise RuntimeError("VPE encoding requires scipy (scipy.fft.dct)")
        self.fw: bytes = firmware_data
        self.dec = VPEDecoder(firmware_data, debug_enabled=False)
        self._build_huffman_encode_tables()
        self._build_sf_encode_tables()

        # Cache window coefficients
        self._win_32k = np.array(
            [self.dec._read_i16(ADDR_WINDOW_COEFS_32K + i * 2) for i in range(320)],
            dtype=np.float64,
        )
        self._win_16k = np.array(
            [self.dec._read_i16(ADDR_WINDOW_COEFS_16K + i * 2) for i in range(160)],
            dtype=np.float64,
        )

        # Cache dequant values as int64 for vectorized math.
        self._dequant = np.zeros((8, 16), dtype=np.int64)
        for cat in range(8):
            for idx in range(16):
                self._dequant[cat, idx] = self.dec._get_dequant(cat, idx)

    # --- WAV IO ---

    @staticmethod
    def _read_wav_mono_16(path: str) -> tuple[np.ndarray, int]:
        with wave.open(path, "rb") as wav:
            ch = wav.getnchannels()
            sw = wav.getsampwidth()
            sr = wav.getframerate()
            n = wav.getnframes()
            if sw != 2:
                raise ValueError(f"WAV must be 16-bit PCM (got {sw * 8}-bit): {path}")
            raw = wav.readframes(n)

        samples = np.frombuffer(raw, dtype="<i2")
        if ch == 1:
            return samples.astype(np.int16, copy=True), int(sr)
        if ch == 2:
            if samples.size % 2:
                samples = samples[:-1]
            stereo = samples.reshape(-1, 2).astype(np.int32)
            mono = np.clip((stereo[:, 0] + stereo[:, 1]) // 2, -32768, 32767)
            return mono.astype(np.int16), int(sr)

        raise ValueError(f"WAV must be mono or stereo (got {ch} channels): {path}")

    @staticmethod
    def _resample_pcm(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if src_rate == dst_rate:
            return pcm.astype(np.int16, copy=True)
        if pcm.size == 0:
            return np.array([], dtype=np.int16)
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError(f"Invalid sample rate conversion {src_rate} -> {dst_rate}")

        if HAS_SCIPY:
            gcd = math.gcd(src_rate, dst_rate)
            up = dst_rate // gcd
            down = src_rate // gcd
            out = scipy_resample_poly(
                pcm.astype(np.float64),
                up,
                down,
                window=("kaiser", 5.0),
            )
            return np.clip(np.rint(out), -32768, 32767).astype(np.int16)

        dst_len = int(round(pcm.size * dst_rate / src_rate))
        dst_len = max(dst_len, 1)
        src_x = np.arange(pcm.size, dtype=np.float64)
        dst_x = np.linspace(0.0, pcm.size - 1, dst_len, dtype=np.float64)
        out = np.interp(dst_x, src_x, pcm.astype(np.float64))
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)

    # --- Huffman encode tables ---

    def _build_huffman_encode_tables(self):
        # Spectral Huffman: cats 0-6.
        self._spec_code_bits = [{} for _ in range(7)]  # sym -> (bits:int, nbits:int)
        self._spec_symbols = [[] for _ in range(7)]  # idx -> sym
        self._spec_bits = [[] for _ in range(7)]  # idx -> bits
        self._spec_nbits = [[] for _ in range(7)]  # idx -> nbits
        self._spec_indices = [None for _ in range(7)]  # idx -> base-N digits (np int)
        self._spec_nonzero = [
            None for _ in range(7)
        ]  # idx -> count of nonzero digits (np int)

        for cat in range(7):
            tree_addr = self.dec.huffman_tree_addrs[cat]
            codes = {}

            def walk(node, code, nbits):
                left = self.dec._read_i16(tree_addr + node * 4)
                right = self.dec._read_i16(tree_addr + node * 4 + 2)
                for bit, val in ((0, left), (1, right)):
                    c2 = (code << 1) | bit
                    n2 = nbits + 1
                    if val <= 0:
                        sym = -val
                        codes[sym] = (c2, n2)
                    else:
                        walk(val, c2, n2)

            walk(0, 0, 0)
            self._spec_code_bits[cat] = codes

            syms = sorted(codes.keys())
            self._spec_symbols[cat] = syms
            self._spec_bits[cat] = [codes[s][0] for s in syms]
            self._spec_nbits[cat] = [codes[s][1] for s in syms]

            # Precompute unpacked coefficient indices for each symbol.
            cpv = self.dec.coefs_per_vector[cat]
            idx_mat = np.zeros((len(syms), cpv), dtype=np.int64)
            nonzero = np.zeros((len(syms),), dtype=np.int64)
            for i, s in enumerate(syms):
                digits = self.dec._unpack_coefs(s, cat)
                idx_mat[i, :] = digits
                nonzero[i] = sum(1 for d in digits if d != 0)
            self._spec_indices[cat] = idx_mat
            self._spec_nonzero[cat] = nonzero

    def _build_sf_encode_tables(self):
        # Scale factor Huffman: one tree per subband.
        self._sf_codes = []  # per band: dict diff->(bits,nbits)
        for band in range(64):  # only first num_subbands used; allocate enough
            base = ADDR_SF_HUFFMAN_TABLE + band * 46
            codes = {}

            def walk(node, code, nbits, seen):
                if node in seen:
                    return
                seen.add(node)
                for bit in (0, 1):
                    child = self.dec._read_i8(base + node * 2 + bit)
                    c2 = (code << 1) | bit
                    n2 = nbits + 1
                    if child <= 0:
                        diff = -child
                        codes[diff] = (c2, n2)
                    else:
                        walk(child, c2, n2, seen)

            walk(0, 0, 0, set())
            self._sf_codes.append(codes)

    # --- Frame template decode (from original firmware frame) ---

    def _decode_frame_template(
        self,
        frame_bytes: bytes,
        bits_per_frame: int,
        num_subbands: int,
        samples_per_frame: int,
    ) -> VpeFrameParams:
        bs = VPEBitstreamReader(frame_bytes)
        scale_factors, quant_indices, shift, bits_left = self.dec._decode_scale_factors(
            bs, num_subbands, bits_per_frame
        )

        if num_subbands == 28:
            cat_bits = 5
            num_iterations = 32
        else:
            cat_bits = 4
            num_iterations = 16

        power_cat = bs.read_bits(cat_bits)
        bits_left -= cat_bits

        available_bits = bits_left
        base_samples = samples_per_frame
        if available_bits > base_samples:
            surplus = (available_bits - base_samples) * 5
            available_bits = self.dec._i16(self.dec._i16(surplus >> 3) + base_samples)

        offset = self.dec._find_categorization_offset(
            scale_factors, num_subbands, available_bits
        )
        categories = self.dec._compute_categories(scale_factors, num_subbands, offset)
        reorder = self.dec._balance_bits(
            categories,
            scale_factors,
            offset,
            available_bits,
            num_subbands,
            num_iterations,
        )
        self.dec._apply_power_category(power_cat, categories, reorder)

        return VpeFrameParams(
            [int(v) for v in scale_factors],
            [int(v) for v in quant_indices],
            int(shift),
            int(power_cat),
            int(cat_bits),
            int(bits_left),
            [int(v) for v in categories],
        )

    # --- Analysis: derive per-frame IMDCT-domain halves from PCM ---

    @staticmethod
    def _reg_div(num, den, eps):
        """Element-wise division with Tikhonov regularisation.

        Where |den| < eps the result is smoothly damped toward zero instead
        of blowing up.  This prevents the Cramer's-rule solve from
        amplifying noise at window-edge positions where the 2×2 system
        is nearly singular (common with high-energy transients).
        """
        abs_den = np.abs(den)
        _safe = abs_den > eps
        # Tikhonov: x = num*den / (den² + eps²)  ≈  num/den when |den|>>eps
        den_sq = den * den
        return num * den / (den_sq + eps * eps)

    def _analysis_imdct_halves(self, pcm, num_frames: int, num_samples: int):
        N = int(num_samples)
        half = N // 2
        if N == 640:
            w = self._win_32k
        elif N == 320:
            w = self._win_16k
        else:
            raise ValueError(f"Unsupported num_samples={N}")

        g1a, g1b, g1d = VPEDecoder.OLA_GAINS_1H
        g2a, g2b, g2c = VPEDecoder.OLA_GAINS_2H
        SCALE = float(1 << 26)

        p = np.arange(half, dtype=np.int64)
        i0 = (half - 1) - p  # index into output first half (reversed)
        w_i0 = w[i0].astype(np.float64)
        w_p = w[p].astype(np.float64)

        A0 = float(g1a) * w_i0
        B0 = float(g1b) * w_p + float(g1d) * w_i0
        A1 = float(g2a) * w_p + float(g2c) * w_i0
        B1 = float(g2b) * w_i0
        D = (A0 * B1) - (A1 * B0)

        # Regularisation threshold: fraction of the peak determinant.
        # Prevents blowup at window edges where D→0 (transient-heavy audio).
        D_eps = max(np.max(np.abs(D)) * 1e-4, 1.0)
        A_eps = max(np.max(np.abs(A0)) * 1e-4, np.max(np.abs(A1)) * 1e-4, 1.0)

        uA = np.zeros((num_frames, half), dtype=np.float64)
        uB = np.zeros((num_frames, half), dtype=np.float64)

        # Frame 0 assumes decoder history = 0.
        y0 = pcm[0:N].astype(np.float64)
        Y0 = y0[i0] * SCALE
        Y1 = y0[half + p] * SCALE
        u_from_0 = self._reg_div(Y0, A0, A_eps)
        u_from_1 = self._reg_div(Y1, A1, A_eps)
        uA[0, :] = 0.5 * (u_from_0 + u_from_1)

        # Use 1-frame lookahead to solve (uA[m+1], history[m+1] == uB[m]).
        for m in range(num_frames - 1):
            y = pcm[(m + 1) * N : (m + 2) * N].astype(np.float64)
            Y0 = y[i0] * SCALE
            Y1 = y[half + p] * SCALE

            u_next = self._reg_div(Y0 * B1 - Y1 * B0, D, D_eps)
            h_next = self._reg_div(A0 * Y1 - A1 * Y0, D, D_eps)

            uA[m + 1, :] = u_next
            uB[m, i0] = h_next  # history index is i0

        # Last history block is unknown; zero pad.
        uB[num_frames - 1, :] = 0.0
        return uA, uB

    # --- Spectral quantization + encode ---

    # --- Analysis-driven parameter computation (replaces template copying) ---

    def _calibrate_sf_offset(
        self,
        segment: AudioSegment,
        num_subbands: int,
        bits_per_frame: int,
        samples_per_frame: int,
    ):
        """Determine the constant offset between ``2*log2(RMS)`` of the
        unshifted analysis spectral and the firmware's actual scale factors.

        Decodes the original segment to PCM, re-analyses it, and compares
        each subband's RMS to the scale factors stored in the original
        bitstream.  Returns the median offset (typically 8-14 depending on
        the firmware's quantizer table).
        """
        header = VpeSegmentHeader.from_bytes(segment.data)
        compressed = segment.data[16:]
        if header.num_frames < 1:
            return 10.0  # safe default

        # Decode original segment to PCM
        vpe_dec = VPEDecoder(self.fw, debug_enabled=False)
        try:
            samples, sr = vpe_dec.decode_segment(segment)
        except Exception:
            return 10.0
        if not samples:
            return 10.0

        N = int(samples_per_frame)
        total_pcm = header.num_frames * N
        if len(samples) < total_pcm:
            # Pad if decode produced fewer samples than expected
            samples = list(samples) + [0] * (total_pcm - len(samples))
        pcm = np.array(samples[:total_pcm], dtype=np.float64)

        # Run the same analysis path the encoder uses
        uA, uB = self._analysis_imdct_halves(pcm, header.num_frames, N)

        # Sample up to 10 frames spread across the segment
        step = max(1, header.num_frames // 10)
        sample_indices = list(range(0, header.num_frames, step))[:10]

        offsets = []
        for fi in sample_indices:
            fr = compressed[fi * header.bytes_per_frame : (fi + 1) * header.bytes_per_frame]
            try:
                tmpl = self._decode_frame_template(fr, bits_per_frame, num_subbands, N)
            except Exception:
                continue

            u = np.concatenate((uA[fi, :], uB[fi, :]), axis=0)
            spec = scipy_dct(u, type=4, norm=None) / float(N)

            for band in range(num_subbands):
                orig_sf = tmpl.scale_factors[band]
                coefs = spec[band * 20 : (band + 1) * 20]
                sum_sq = float(np.sum(coefs**2))
                if sum_sq < 1.0:
                    continue
                rms = math.sqrt(sum_sq / 20.0)
                raw = 2.0 * math.log2(max(1.0, rms))
                offsets.append(orig_sf - raw)

        if offsets:
            return float(np.median(offsets))
        return 10.0

    def _compute_scale_factors_from_spectral(
        self, spectral, num_subbands: int, sf_offset=0.0
    ):
        """Compute per-subband scale factors from spectral coefficients.

        Each subband has 20 spectral coefficients.  The G.722.1/Siren7
        convention is:
            sf ≈ round(2 * log2(RMS) + offset)
        where RMS = sqrt(mean(coef²)) and *offset* is a firmware-specific
        constant obtained via ``_calibrate_sf_offset``.
        """
        scale_factors = []
        for band in range(num_subbands):
            coefs = spectral[band * 20 : (band + 1) * 20]
            sum_sq = float(np.sum(coefs**2))
            if sum_sq < 1.0:
                sf = -7
            else:
                rms = math.sqrt(sum_sq / 20.0)
                sf = int(round(2.0 * math.log2(max(1.0, rms)) + sf_offset))
                sf = max(-7, min(24, sf))
            scale_factors.append(sf)
        return scale_factors

    def _ensure_sf_encodable(self, scale_factors, num_subbands):
        """Adjust scale factors so every consecutive delta is Huffman-encodable.

        First SF must satisfy -7 <= sf <= 24  (5-bit field with +7 offset).
        Each subsequent delta+12 must exist in the per-band Huffman codebook.
        """
        result = [max(-7, min(24, scale_factors[0]))]
        for band in range(1, num_subbands):
            target = max(-7, min(24, scale_factors[band]))
            prev = result[-1]
            diff = (target - prev) + 12
            if diff in self._sf_codes[band]:
                result.append(target)
            else:
                best_sf = prev
                best_err = float("inf")
                for d in self._sf_codes[band]:
                    candidate = prev + d - 12
                    candidate = max(-7, min(24, candidate))
                    err = abs(candidate - target)
                    if err < best_err:
                        best_err = err
                        best_sf = candidate
                result.append(best_sf)
        return result

    def _compute_shift_and_quant(self, scale_factors, num_subbands: int):
        """Compute shift factor and quant_indices from scale factors.

        Mirrors the decoder's algorithm in _decode_scale_factors (post-SF
        section).  Returns (shift, quant_indices).
        """
        i16 = self.dec._i16
        max_sf = 0
        total_bits_used = 0
        for i in range(num_subbands):
            sf_idx = i16(scale_factors[i] + 24)
            if sf_idx > max_sf:
                max_sf = sf_idx
            total_bits_used = i16(
                total_bits_used + self.dec._read_i16(ADDR_QUANTIZER_TABLE + sf_idx * 2)
            )

        shift = 9
        while shift >= 0:
            if total_bits_used < 8 and max_sf <= 28:
                break
            shift -= 1
            total_bits_used >>= 1
            max_sf = i16(max_sf - 2)

        shift = max(0, shift)
        quant_indices = []
        for i in range(num_subbands):
            idx = i16(scale_factors[i] + shift * 2 + 24)
            qi = self.dec._read_i16(ADDR_QUANTIZER_TABLE + idx * 2)
            quant_indices.append(int(qi))
        return shift, quant_indices

    def _estimate_sf_bits(self, scale_factors, num_subbands):
        """Estimate bits consumed by scale-factor Huffman encoding."""
        bits = 5  # first SF: 5 raw bits
        prev = scale_factors[0]
        for band in range(1, num_subbands):
            diff = (scale_factors[band] - prev) + 12
            codes = self._sf_codes[band]
            if diff in codes:
                bits += codes[diff][1]
            else:
                bits += 12  # conservative fallback
            prev = scale_factors[band]
        return bits

    def _compute_frame_params(
        self,
        spectral_unshifted,
        num_subbands: int,
        bits_per_frame: int,
        samples_per_frame: int,
        sf_offset=0.0,
    ) -> VpeFrameParams:
        """Compute all encoding parameters from unshifted spectral coefficients.

        Replaces template-copying with proper analysis: derives scale factors,
        shift, quant_indices, and categories from the actual spectral content
        of the new audio.
        """
        i16 = self.dec._i16

        if num_subbands == 28:
            cat_bits = 5
            num_iterations = 32
        else:
            cat_bits = 4
            num_iterations = 16

        # 1. Derive scale factors from spectral energy
        raw_sf = self._compute_scale_factors_from_spectral(
            spectral_unshifted, num_subbands, sf_offset
        )
        scale_factors = self._ensure_sf_encodable(raw_sf, num_subbands)

        # 2. Derive shift and quant_indices
        shift, quant_indices = self._compute_shift_and_quant(
            scale_factors, num_subbands
        )

        # 3. Spectral bit budget = total - SF bits - power_cat bits
        sf_bits = self._estimate_sf_bits(scale_factors, num_subbands)
        bits_left = bits_per_frame - sf_bits - cat_bits

        # 4. Available-bits with surplus cap (matches decoder logic)
        available_bits = bits_left
        base_samples = samples_per_frame
        if available_bits > base_samples:
            surplus = (available_bits - base_samples) * 5
            available_bits = i16(i16(surplus >> 3) + base_samples)

        # 5. Categorization offset via binary search
        offset = self.dec._find_categorization_offset(
            scale_factors, num_subbands, available_bits
        )

        # 6. Initial per-subband categories
        categories = self.dec._compute_categories(scale_factors, num_subbands, offset)

        # 7. Iterative bit balancing
        reorder = self.dec._balance_bits(
            categories,
            scale_factors,
            offset,
            available_bits,
            num_subbands,
            num_iterations,
        )

        # 8. power_cat = 0 gives maximum quality; lambda search handles
        #    budget fitting.  If the QUANT_STEPS estimate already exceeds
        #    the budget, bump power_cat until it fits.
        power_cat = 0
        total_qs = sum(self.dec.QUANT_STEPS[c] for c in categories)
        if total_qs > bits_left:
            for pc in range(1, min(num_iterations, len(reorder) + 1)):
                band = reorder[pc - 1] if pc - 1 < len(reorder) else 0
                if band < len(categories):
                    categories[band] = min(7, categories[band] + 1)
                total_qs = sum(self.dec.QUANT_STEPS[c] for c in categories)
                if total_qs <= bits_left:
                    power_cat = pc
                    break
            else:
                power_cat = min(num_iterations - 1, (1 << cat_bits) - 1)

        return VpeFrameParams(
            [int(sf) for sf in scale_factors],
            [int(qi) for qi in quant_indices],
            int(shift),
            power_cat,
            cat_bits,
            int(bits_left),
            [int(c) for c in categories],
        )

    def _encode_scale_factors(
        self, bw: VPEBitstreamWriter, scale_factors, num_subbands: int
    ):
        if num_subbands > len(self._sf_codes):
            raise ValueError(f"num_subbands too large for SF tables: {num_subbands}")
        # First SF: 5 bits, stored as (sf0 + 7)
        sf0 = int(scale_factors[0])
        val = sf0 + 7
        if not (0 <= val <= 31):
            raise ValueError(f"scale_factors[0]={sf0} out of encodable range")
        bw.write_bits(val, 5)

        prev = sf0
        for band in range(1, num_subbands):
            sf = int(scale_factors[band])
            diff = (sf - prev) + 12
            codes = self._sf_codes[band]
            if diff not in codes:
                raise ValueError(f"SF diff {diff} not encodable at band {band}")
            bits, nbits = codes[diff]
            bw.write_bits(bits, nbits)
            prev = sf

    def _quantize_band_vectors(
        self, cat, q_level, target, bits_lambda, perceptual_weight=1.0
    ):
        """
        Quantize one band (20 coeffs) into symbols/signbits for a given category.
        Returns (sym_idx_list, sign_pairs_list, bits_used).

        *perceptual_weight* scales the distortion term: higher values make the
        quantizer fight harder to preserve this band at the expense of bits.
        """
        if cat >= 7:
            return [], [], 0
        cpv = int(self.dec.coefs_per_vector[cat])
        num_vec = int(self.dec.huffman_lengths[cat])

        idx_mat = self._spec_indices[cat]  # (S, cpv)
        code_len = np.array(self._spec_nbits[cat], dtype=np.int64)  # (S,)
        nonzero = self._spec_nonzero[cat]  # (S,)
        bit_cost = code_len + nonzero  # (S,)

        deq = self._dequant[cat]  # (16,)
        q = int(q_level)

        sym_idx_list = []
        sign_pairs = []
        bits_used = 0

        pw = float(perceptual_weight)

        # Quantize each vector independently (additive cost).
        for v in range(num_vec):
            vec = target[v * cpv : (v + 1) * cpv]
            mags = np.abs(vec).astype(np.float64)

            # Reconstruction magnitudes for all symbols: (S, cpv)
            recon = (q * deq[idx_mat]) >> 12
            recon = recon.astype(np.float64)

            err = np.sum((recon - mags) ** 2, axis=1)
            cost = pw * err + (bits_lambda * bit_cost.astype(np.float64))
            s_idx = int(np.argmin(cost))

            digits = idx_mat[s_idx, :]
            sign_val = 0
            sign_len = 0
            for j in range(cpv):
                if int(digits[j]) != 0:
                    sign_val = (sign_val << 1) | (1 if vec[j] >= 0 else 0)
                    sign_len += 1

            sym_idx_list.append(s_idx)
            sign_pairs.append((sign_val, sign_len))
            bits_used += int(bit_cost[s_idx])

        return sym_idx_list, sign_pairs, bits_used

    # Perceptual weighting curve.  Higher weight → quantizer preserves
    # that band more aggressively.  Bass/mid (bands 0-5) matter most;
    # high-frequency noise is less audible.  Loosely follows ISO 226
    # equal-loudness contours scaled to the subband grid.
    _PERCEPTUAL_WEIGHTS = None  # lazily built per num_subbands

    def _get_perceptual_weights(self, num_subbands: int):
        """Return an array of perceptual importance weights, one per subband."""
        # Simple model: weight = 1 / (1 + band * decay).
        # Bands 0-5 get roughly 1.0–0.5, upper bands taper to ~0.25.
        decay = 0.15
        return np.array(
            [1.0 / (1.0 + b * decay) for b in range(num_subbands)], dtype=np.float64
        )

    def _encode_spectral(
        self,
        bw: VPEBitstreamWriter,
        spectral,
        template: VpeFrameParams,
        num_subbands: int,
    ):
        categories = template.categories
        quant_indices = template.quant_indices
        budget = int(template.spectral_bits_budget)
        pw = self._get_perceptual_weights(num_subbands)

        # First try pure distortion minimization. If it overflows, increase lambda until it fits.
        def try_lambda(lmb):
            plan = []
            signs = []
            used = 0
            for band in range(num_subbands):
                cat = int(categories[band])
                q = int(quant_indices[band])
                band_spec = spectral[band * 20 : (band + 1) * 20]
                sym_idx, sign_pairs, bits_used = self._quantize_band_vectors(
                    cat, q, band_spec, lmb, perceptual_weight=float(pw[band])
                )
                plan.append((cat, sym_idx))
                signs.append((cat, sign_pairs))
                used += bits_used
            return used, plan, signs

        used, plan, signs = try_lambda(0.0)
        if used > budget:
            lo = 0.0
            hi = 1.0
            used_hi, _, _ = try_lambda(hi)
            while used_hi > budget and hi < 1e12:
                hi *= 2.0
                used_hi, _, _ = try_lambda(hi)
            # Binary search for smallest lambda that fits.
            best = None
            for _ in range(28):
                mid = (lo + hi) * 0.5
                used_mid, plan_mid, signs_mid = try_lambda(mid)
                if used_mid > budget:
                    lo = mid
                else:
                    hi = mid
                    best = (used_mid, plan_mid, signs_mid)
            if best is None:
                raise RuntimeError(
                    f"Unable to fit spectral bits into budget (budget={budget})"
                )
            used, plan, signs = best

        # Write spectral codes.
        bits_written = 0
        for band in range(num_subbands):
            cat = int(categories[band])
            if cat >= 7:
                continue
            sym_idx_list = plan[band][1]
            sign_pairs = signs[band][1]

            bits_arr = self._spec_bits[cat]
            nbits_arr = self._spec_nbits[cat]
            for s_idx, (sign_val, sign_len) in zip(sym_idx_list, sign_pairs):
                bw.write_bits(int(bits_arr[s_idx]), int(nbits_arr[s_idx]))
                bits_written += int(nbits_arr[s_idx])
                if sign_len > 0:
                    bw.write_bits(int(sign_val), int(sign_len))
                    bits_written += int(sign_len)

        if bits_written > budget:
            # Should not happen; guard for mismatched accounting.
            raise RuntimeError(
                f"Spectral bit budget exceeded: used={bits_written} budget={budget}"
            )

    def _encode_frame(
        self,
        spectral_560,
        template: VpeFrameParams,
        bits_per_frame: int,
        num_subbands: int,
    ):
        bw = VPEBitstreamWriter(bits_per_frame)
        self._encode_scale_factors(bw, template.scale_factors, num_subbands)
        bw.write_bits(int(template.power_cat), int(template.cat_bits))
        # Use actual remaining bits as spectral budget (more accurate than
        # the pre-computed estimate which may differ by a few bits).
        actual = copy.deepcopy(template)
        actual.spectral_bits_budget = bits_per_frame - bw.bitpos
        self._encode_spectral(bw, spectral_560, actual, num_subbands)
        return bw.get_bytes()

    def encode_vpe_segment_frames_from_wav(self, wav_path: str, profile: EncodingProfile = ENCODING_BEST) -> tuple[bytes, int]:
        """Encode a WAV file into VPE compressed frames for in-place segment
        replacement.

        Instead of copying scale factors / categories from the original
        firmware frames (which describe *different* audio), this derives
        all encoding parameters from the new WAV's spectral content:
          1. Analysis filter  -> IMDCT-domain halves from PCM
          2. Inverse DCT-IV   -> unshifted spectral coefficients
          3. Per-subband RMS   -> scale factors (log2-energy)
          4. Standard pipeline -> shift, quant_indices, categories
          5. Shift + quantize  -> Huffman-coded bitstream
        """

        # WAV is preprocessed to mono and resampled to the target profile rate.
        pcm, sr = self._read_wav_mono_16(wav_path)
        pcm = self._resample_pcm(pcm, sr, profile.samplerate)

        samples_count = len(pcm)
        num_frames = max(1, math.ceil(samples_count / profile.samples_per_frame))
        padded_count = num_frames * profile.samples_per_frame
        if samples_count < padded_count:
            pcm = np.pad(pcm, (0, padded_count - samples_count), mode="constant")
        elif samples_count > padded_count:
            pcm = pcm[:padded_count]

        print(f"    SF calibration offset: {profile.sf_offset:.2f}")

        # Analysis: compute IMDCT-domain halves from PCM.
        pcm_f = pcm.astype(np.float64)
        uA, uB = self._analysis_imdct_halves(
            pcm_f, num_frames, profile.samples_per_frame
        )

        # Encode each frame with parameters derived from its spectral content.
        out = bytearray()
        N = int(profile.samples_per_frame)
        for fi in range(num_frames):
            u = np.concatenate((uA[fi, :], uB[fi, :]), axis=0)

            # Unshifted spectral via DCT-IV
            spec_unshifted = scipy_dct(u, type=4, norm=None) / float(N)
            spec_region = spec_unshifted[: profile.num_subbands * 20]

            # Derive encoding parameters from spectral content
            params = self._compute_frame_params(
                spec_region,
                profile.num_subbands,
                profile.bits_per_frame,
                profile.samples_per_frame,
                profile.sf_offset,
            )

            # Scale spectral by shift (decoder expects shifted-domain values)
            shift = params.shift
            if shift > 0:
                spec_shifted = spec_region * float(1 << shift)
            else:
                spec_shifted = np.array(spec_region, dtype=np.float64)

            # Clamp each subband's spectral to the quantizer's representable
            # range.  Values beyond this just become distortion; clamping lets
            # the quantizer pick the closest valid symbol instead of railing.
            for band in range(profile.num_subbands):
                qi = abs(params.quant_indices[band])
                cat = params.categories[band]
                if cat < 7 and qi > 0:
                    max_dq = float(np.max(np.abs(self._dequant[cat, :])))
                    clip_val = (qi * max_dq / 4096.0) * 1.2
                    lo = band * 20
                    hi = lo + 20
                    spec_shifted[lo:hi] = np.clip(
                        spec_shifted[lo:hi], -clip_val, clip_val
                    )

            frame_bytes = self._encode_frame(
                spec_shifted.astype(np.float64),
                params,
                profile.bits_per_frame,
                profile.num_subbands,
            )
            out.extend(frame_bytes)

        return bytes(out), num_frames

    @staticmethod
    def create_vpe_segment_header(profile: EncodingProfile, num_frames: int) -> VpeSegmentHeader:
        return VpeSegmentHeader(
            profile.first_byte,
            0xFF,
            num_frames,
            profile.bitrate,
            profile.subtype,
            profile.bits_per_frame,
            profile.num_subbands,
            profile.samples_per_frame
        )

    def encode_wav_into_audio_segment(self, wav_path: str, profile: EncodingProfile) -> AudioSegment:
        new_frames, num_frames = self.encode_vpe_segment_frames_from_wav(wav_path, profile)
        seg_header = self.create_vpe_segment_header(profile, num_frames)
        return AudioSegment(seg_header.to_bytes()+new_frames)

# ============================================================================
# DPCM Decoder
# ============================================================================


class DPCMDecoder:
    """
    DPCM/ADPCM decoder for ISD9160 non-VPE audio segments.
    Faithfully implements the firmware's codec_dispatcher_init, decode_frame_main,
    decode_frame_simple, decode_frame_dpcm, decode_frame_initial, decode_delta_step,
    compute_predictor, and decode_adpcm_sample functions.

    Frame modes (control_byte & 0x18):
      0x00: No-op (skip)
      0x08: ADPCM parameter setup + initial frame (sub_mode = bits_per_sample 2-5)
      0x10: DPCM delta frame (sub_mode selects bps 6-8 and prediction on/off)
      0x18: Simple PCM frame (sub_mode: 0=8bit, 1=10bit, 2=16bit, 3=12bit)
    """

    SAMPLES_PER_FRAME = 256

    # Mode 0x10 sub_mode → (bits_per_sample, use_prediction)
    DPCM_MODES = {
        0: (6, False),
        1: (7, False),
        2: (8, False),
        4: (6, True),
        5: (7, True),
        6: (8, True),
    }

    # Mode 0x18 sub_mode → bits_per_sample
    PCM_BPS = {0: 8, 1: 10, 2: 16, 3: 12}

    # Mode 0x08 sub_mode → step_table_base address in firmware
    ADPCM_STEP_BASES = {
        2: 0x6F88,  # DAT_0000079c
        3: 0x7110,  # DAT_00000798 + 0x80
        4: 0x7090,  # DAT_00000798
        5: 0x6F90,  # DAT_0000079c + 8
    }

    # DPCM/ADPCM table block anchor (retail/full reference layout).
    #
    # Some builds relocate this whole block; the bytes at the legacy addresses
    # are 0xFF (erased flash). Anchor off the 16-byte predictor coef table
    # pattern and apply known relative offsets.
    _COEF_TABLE_PATTERN = (
        b"\x08\x0a\x0c\x0e\x10\x12\x14\x16\x80\x82\x84\x86\x00\x02\x04\x06"
    )
    _COEF_TABLE_DEFAULT_ADDR = 0x6F78
    _OFF_STEP_BASE_2 = 0x6F88 - 0x6F78
    _OFF_STEP_BASE_5 = 0x6F90 - 0x6F78
    _OFF_STEP_BASE_4 = 0x7090 - 0x6F78
    _OFF_STEP_BASE_3 = 0x7110 - 0x6F78
    _OFF_ADPCM_STEP_SIZES = 0x7150 - 0x6F78
    _OFF_DELTA_STEP_TABLE = 0x71D0 - 0x6F78

    def __init__(self, firmware_data: bytes):
        self.fw = firmware_data
        self._resolve_table_addrs()
        self._load_tables()

    def _read_i8(self, addr):
        v = self.fw[addr]
        return v if v < 128 else v - 256

    def _read_i16(self, addr):
        return struct.unpack_from("<h", self.fw, addr)[0]

    def _read_u16(self, addr):
        return struct.unpack_from("<H", self.fw, addr)[0]

    @staticmethod
    def _is_erased(blob):
        return bool(blob) and all(b == 0xFF for b in blob)

    def _resolve_table_addrs(self):
        base = self._COEF_TABLE_DEFAULT_ADDR
        default_blob = self.fw[base : base + len(self._COEF_TABLE_PATTERN)]
        if default_blob != self._COEF_TABLE_PATTERN:
            idx = self.fw.find(self._COEF_TABLE_PATTERN)
            if idx != -1:
                base = idx

        self.coef_table_addr = base
        self.adpcm_step_sizes_addr = base + self._OFF_ADPCM_STEP_SIZES
        self.delta_step_table_addr = base + self._OFF_DELTA_STEP_TABLE
        self.adpcm_step_bases: dict[int, int] = {
            2: base + self._OFF_STEP_BASE_2,
            3: base + self._OFF_STEP_BASE_3,
            4: base + self._OFF_STEP_BASE_4,
            5: base + self._OFF_STEP_BASE_5,
        }

        # If derived addrs still look erased, fall back to legacy constants.
        if self._is_erased(self.fw[self.coef_table_addr : self.coef_table_addr + 16]):
            self.coef_table_addr = 0x6F78
        if self._is_erased(
            self.fw[self.adpcm_step_sizes_addr : self.adpcm_step_sizes_addr + 16]
        ):
            self.adpcm_step_sizes_addr = 0x7150
        if self._is_erased(
            self.fw[self.delta_step_table_addr : self.delta_step_table_addr + 16]
        ):
            self.delta_step_table_addr = 0x71D0
        for k, v in list(self.adpcm_step_bases.items()):
            if self._is_erased(self.fw[v : v + 16]):
                legacy = {2: 0x6F88, 3: 0x7110, 4: 0x7090, 5: 0x6F90}
                self.adpcm_step_bases[k] = legacy[k]

    def _load_tables(self) -> None:
        """Load codec tables from firmware."""
        # Delta step table: 8 int16 values.
        self.delta_step_table = [
            self._read_i16(self.delta_step_table_addr + i * 2) for i in range(8)
        ]
        # ADPCM step size table: 64 int16 values.
        self.adpcm_step_sizes = [
            self._read_i16(self.adpcm_step_sizes_addr + i * 2) for i in range(64)
        ]
        # Prediction coefficient tables: 16 bytes.
        self.coef_table = list(
            self.fw[self.coef_table_addr : self.coef_table_addr + 16]
        )

    def _clamp(self, val: int, lo: int, hi: int) -> int:
        if val > hi:
            return hi
        if val < lo:
            return lo
        return val

    def _to_i16(self, val: int):
        """Truncate to signed 16-bit."""
        val = int(val) & 0xFFFF
        return val if val < 32768 else val - 65536

    # --- decode_delta_step (firmware 0x3EE) ---

    def _decode_delta_step(self, encoded_value: int, bits_per_sample: int):
        """Decode a DPCM delta value using the step table.
        Encoded format: bit0=sign, bits1-3=step_index, bits4+=magnitude.
        shift_amount = bits_per_sample (from r12 in firmware)."""
        sign = encoded_value & 1
        step_index = (encoded_value >> 1) & 7
        magnitude = encoded_value >> 4

        # delta = step_table[index] + (magnitude << (8 - bps)) << (index + 3)
        scaled = (magnitude << (8 - bits_per_sample)) << (step_index + 3)
        delta = self._to_i16(self.delta_step_table[step_index] + scaled)

        if sign:
            delta = self._to_i16(-delta)
        return delta

    # --- compute_predictor (firmware 0x288-style) ---

    def _compute_predictor(
        self, sample_0e, sample_10, coef_idx1, coef_idx2, max_clamp, min_clamp
    ):
        """Compute weighted predictor from two previous samples using
        coefficient tables. Implements bit-serial fixed-point multiplication."""
        coef1 = self.coef_table[coef_idx1]  # from table at 0x6F78
        coef2 = self.coef_table[8 + coef_idx2]  # from table at 0x6F80

        # Bit-serial multiply for coef1 * sample_0e
        iVar3 = sample_0e
        result = 0
        if (coef1 << 27) & 0x80000000:  # check bit 4
            result = iVar3
        for bit_mask in [8, 4, 2]:  # bits 3, 2, 1
            iVar3 >>= 1
            if coef1 & bit_mask:
                result += iVar3

        # Bit-serial multiply for coef2 * sample_10
        iVar5 = sample_10
        if coef2 & 0x80:  # check bit 7 (sign)
            result -= iVar5 >> 1
        for bit_mask in [8, 4, 2]:  # bits 3, 2, 1
            iVar5 >>= 1
            if coef2 & bit_mask:
                result += iVar5

        return self._clamp(result, min_clamp, max_clamp)

    # --- decode_adpcm_sample (firmware 0x??? at DAT_00000674+0xC0) ---

    def _decode_adpcm_sample(self, sample_code, ctx: DPCMContext):
        """ADPCM sample decoder with adaptive step size.
        Uses step_sizes table indexed by step_index, reconstructs from
        magnitude bits, adds to predictor, adapts step_index."""
        # Get current step size
        step_size = self.adpcm_step_sizes[self._clamp(ctx.step_index, 0, 63)]

        bps = ctx.bits_per_sample
        mask = ctx.mask

        # Reconstruct delta from magnitude bits
        # Start from MSB-1 (skip sign bit which is the MSB)
        test_bit = 1 << (bps - 2)  # highest magnitude bit
        delta = 0
        for _ in range(bps - 1):
            if sample_code & test_bit:
                delta += step_size
            test_bit >>= 1
            step_size >>= 1
        delta += step_size  # add half-step (rounding)

        # Check sign bit (MSB of the N-bit code)
        sign_bit = 1 << (bps - 1)
        if sample_code & sign_bit:
            delta = -delta

        # Adapt step_index using adaptation table
        adapt_idx = (mask >> 1) & sample_code
        adapt_offset = ctx.adapt_table + adapt_idx
        if adapt_offset < len(self.fw):
            adapt_val = self._read_i8(adapt_offset)
        else:
            adapt_val = 0
        new_step_idx = ctx.step_index + adapt_val
        ctx.step_index = self._clamp(new_step_idx, 0, ctx.max_step_index)

        # Update sample history
        ctx.prev_sample = ctx.current_sample

        # Add delta to predictor
        output = ctx.predictor + delta
        output = self._clamp(output, ctx.min_clamp, ctx.max_clamp)

        ctx.current_sample = self._to_i16(output)
        return output

    # --- Main decoder ---

    def decode_segment(self, segment: AudioSegment):
        """Decode a DPCM segment. Returns (samples, sample_rate)."""
        if len(segment.data) < 2:
            return [], 8000

        header = DPCMSegmentHeader.from_bytes(segment.data)

        # Initialize ADPCM context (matches FUN_00000288)
        default_step_base = self.adpcm_step_bases.get(4, 0x7090)
        ctx = DPCMContext(
            4,
            0x0F,
            default_step_base,
            default_step_base,
            64,
            4,
            4,
            0,
            0,
            0,
            0,
            32767,
            -32768,
        )

        # DPCM predictor (separate from ADPCM predictor)
        dpcm_predictor = 0

        # Set up bitstream on data after first byte
        bs = DPCMBitstreamReader(segment.data[1:])
        all_samples = []

        # Get first control byte
        if header.use_stream_control:
            # Variable mode: read control bytes from data stream each frame
            if bs.exhausted:
                return [], header.samplerate
            control_byte = self._read_byte(bs)
        else:
            # Fixed mode: first byte IS the repeating control byte
            control_byte = header.first_byte

        frame_count = 0
        while not bs.exhausted:
            mode = control_byte & 0x18
            sub_mode = control_byte & 0x07

            try:
                if mode == 0x00:
                    # No-op frame
                    pass

                elif mode == 0x08:
                    # ADPCM parameter setup + initial frame
                    samples = self._handle_adpcm_init(bs, ctx, sub_mode)
                    all_samples.extend(samples)

                elif mode == 0x10:
                    # DPCM delta frame
                    samples, dpcm_predictor = self._handle_dpcm_frame(
                        bs, ctx, sub_mode, dpcm_predictor
                    )
                    all_samples.extend(samples)

                elif mode == 0x18:
                    # Simple PCM frame
                    samples = self._handle_simple_pcm(bs, ctx, sub_mode)
                    all_samples.extend(samples)

                else:
                    break

            except Exception as e:
                if frame_count < 3:
                    print(f"    Frame {frame_count} error: {e}")
                    traceback.print_exc()
                # Output silence for this frame
                all_samples.extend([0] * self.SAMPLES_PER_FRAME)

            frame_count += 1

            # Get next control byte
            if bs.exhausted:
                break

            if header.use_stream_control:
                # Byte-align before reading next control byte
                self._byte_align(bs)
                if bs.exhausted:
                    break
                control_byte = self._read_byte(bs)
            # else: same control_byte repeats for fixed-mode segments

        print(f"    Decoded {frame_count} frames, {len(all_samples)} samples")
        return all_samples, header.samplerate

    def _read_byte(self, bs: DPCMBitstreamReader):
        """Read a full byte from the bitstream (used for control bytes)."""
        if bs.exhausted:
            return 0
        val = bs.data[bs.pos]
        bs.pos += 1
        bs.bits_left = 8
        return val

    def _byte_align(self, bs: DPCMBitstreamReader):
        """Align bitstream to next byte boundary (firmware realignment check)."""
        if bs.bits_left != 8:
            bs.pos += 1
            bs.bits_left = 8

    # --- Mode 0x18: Simple PCM (decode_frame_simple) ---

    def _handle_simple_pcm(self, bs: DPCMBitstreamReader, ctx: DPCMContext, sub_mode):
        """Decode a simple PCM frame. Reads N bits per sample, left-shifts to 16-bit."""
        bps = self.PCM_BPS.get(sub_mode, 8)
        samples = []

        for _ in range(self.SAMPLES_PER_FRAME):
            if bs.exhausted:
                samples.append(0)
                continue
            val = bs.read_bits(bps)
            # Left-shift to 16-bit range (matches firmware: val << (16 - bps))
            val = (val << (16 - bps)) & 0xFFFF
            if val >= 32768:
                val -= 65536
            samples.append(val)

        return samples

    # --- Mode 0x10: DPCM delta frame (decode_frame_dpcm) ---

    def _handle_dpcm_frame(
        self, bs: DPCMBitstreamReader, ctx: DPCMContext, sub_mode, predictor
    ):
        """Decode a DPCM delta frame using decode_delta_step."""
        mode_info = self.DPCM_MODES.get(sub_mode)
        if mode_info is None:
            return [0] * self.SAMPLES_PER_FRAME, predictor

        bps, use_prediction = mode_info
        samples = []

        for _ in range(self.SAMPLES_PER_FRAME):
            if bs.exhausted:
                samples.append(0)
                continue

            raw = bs.read_bits(bps)
            delta = self._decode_delta_step(raw & 0xFF, bps)

            if use_prediction:
                # output = delta*2 + predictor*4, clamped to ±32767
                output = delta * 2 + predictor * 4
                output = self._clamp(output, -32767, 32767)
                samples.append(self._to_i16(output))
                # predictor = output / 4 (with C-style rounding toward zero)
                if output < 0:
                    predictor = self._to_i16(
                        (((output >> 31) & 0xFFFFFFFF) >> 30) + output >> 2
                    )
                else:
                    predictor = self._to_i16(output >> 2)
            else:
                # Raw delta output
                samples.append(delta)

        # Store last sample / 4 as predictor for continuity
        if samples:
            last = samples[-1]
            if last < 0:
                predictor = self._to_i16((last + ((-last - 1) >> 30 & 3)) >> 2)
            else:
                predictor = self._to_i16(last >> 2)

        return samples, predictor

    # --- Mode 0x08: ADPCM init frame (decode_frame_initial) ---

    def _handle_adpcm_init(self, bs: DPCMBitstreamReader, ctx: DPCMContext, sub_mode):
        """Handle ADPCM parameter setup and decode an initial frame.
        Reads 2 config bytes, updates prediction parameters, then decodes
        256 samples using compute_predictor + decode_adpcm_sample."""
        # Update bits_per_sample from sub_mode
        ctx.bits_per_sample = sub_mode
        ctx.mask = (1 << sub_mode) - 1

        # Read config byte 1: max_step_index and mode_bits
        if bs.exhausted:
            return [0] * self.SAMPLES_PER_FRAME
        config1 = self._read_byte(bs)
        max_step_idx = config1 >> 2
        mode_bits = config1 & 3
        ctx.max_step_index = max_step_idx

        # Read config byte 2: prediction coefficient indices
        if bs.exhausted:
            return [0] * self.SAMPLES_PER_FRAME
        config2 = self._read_byte(bs)
        ctx.coef_idx2 = (config2 >> 4) & 0x0F
        ctx.coef_idx1 = config2 & 0x0F

        # Set step table base from sub_mode
        step_bases = getattr(self, "adpcm_step_bases", self.ADPCM_STEP_BASES)
        if sub_mode in step_bases:
            ctx.step_table_base = step_bases[sub_mode]

        # Compute adaptation table address
        ctx.adapt_table = ctx.step_table_base + (mode_bits << (sub_mode - 1))

        # Decode frame using ADPCM
        samples = []
        for _ in range(self.SAMPLES_PER_FRAME):
            if bs.exhausted:
                samples.append(0)
                continue

            raw = bs.read_bits(sub_mode)

            # Compute weighted predictor from last two samples
            ctx.predictor = self._compute_predictor(
                ctx.current_sample,
                ctx.prev_sample,
                ctx.coef_idx1,
                ctx.coef_idx2,
                ctx.max_clamp,
                ctx.min_clamp,
            )

            # Decode ADPCM sample (adds delta to predictor)
            decoded = self._decode_adpcm_sample(raw, ctx)

            # Output shifted left by 2 (14-bit to 16-bit conversion)
            output = self._to_i16(decoded << 2)
            samples.append(output)

        return samples


# ============================================================================
# Common Decoder context
# ============================================================================


class FirmwareDecoderContext:
    def __init__(self, fw_data: bytes):
        self.vpe_decoder = VPEDecoder(fw_data, debug_enabled=False)
        self.dpcm_decoder = DPCMDecoder(fw_data)

    def decode_segment(
        self, segment: AudioSegment
    ) -> tuple[list[int], int]:
        if segment.is_vpe:
            samples, sample_rate = self.vpe_decoder.decode_segment(segment)
        elif segment.is_dpcm:
            samples, sample_rate = self.dpcm_decoder.decode_segment(segment)
        else:
            # Invalid data
            return [], 0

        if not samples:
            raise Exception("No samples decoded")

        return samples, sample_rate


# ============================================================================
# Firmware Parser
# ============================================================================


class ISD9160Firmware:
    """Parser for ISD9160 firmware images with VPE audio library."""

    def __init__(self, fw_data: bytes):
        self.data = fw_data
        self.seg_entries: list[LibrarySegEntry] = []
        self.version_str: str = ""
        self.seg_count = 0
        self.seg_table_ptr = 0
        self._parse()

    @classmethod
    def from_filepath(cls, filepath: str) -> "ISD9160Firmware":
        with open(filepath, "rb") as f:
            data = f.read()
        return cls(data)

    @property
    def audiodata_start_offset(self) -> int:
        if len(self.seg_entries) > 0:
            return self.seg_entries[0].start
        return 0

    @property
    def audiodata_end_offset(self) -> int:
        if len(self.seg_entries) > 0:
            return self.seg_entries[-1].end
        return 0

    @property
    def audiodata_total_size(self) -> int:
        return self.audiodata_end_offset - self.audiodata_start_offset

    @property
    def audiodata_area_known(self) -> bool:
        return self.audiodata_total_size > 0

    def _parse(self):
        """Parse VPE library header and segment table."""
        # Read VPE firmware header
        if len(self.data) < 0x8020:
            raise ValueError("File too small to contain VPE firmware header")

        magic = struct.unpack_from("<I", self.data, VPE_HEADER_ADDR)[0]
        if magic != VPE_MAGIC:
            print(
                f"Warning: VPE magic mismatch (got 0x{magic:08X}, expected 0x{VPE_MAGIC:08X})"
            )

        # Library table pointer at header + 8
        lib_table_ptr = struct.unpack_from("<I", self.data, VPE_HEADER_ADDR + 8)[0]
        print(f"VPE Library Header at: 0x{lib_table_ptr:X}")

        # Read VPE library header
        vpe_magic = self.data[lib_table_ptr : lib_table_ptr + 4]
        print(f"VPE Magic: {vpe_magic!r}")


        self.seg_table_ptr = struct.unpack_from("<I", self.data, lib_table_ptr + 0x0C)[0]
        self.seg_count = (
            struct.unpack_from("<I", self.data, lib_table_ptr + 0x10)[0] & 0xFFFF
        )
        print(f"Segment Table at: 0x{self.seg_table_ptr:X}")
        print(f"Segment Count: {self.seg_count}")

        # Read version string pointer
        ver_str_ptr = struct.unpack_from("<I", self.data, lib_table_ptr + 0x28)[0]
        if ver_str_ptr < len(self.data):
            ver_end = (
                self.data.index(0, ver_str_ptr)
                if 0 in self.data[ver_str_ptr : ver_str_ptr + 128]
                else ver_str_ptr + 70
            )
            self.version_str = self.data[ver_str_ptr:ver_end].decode("ascii", errors="replace")
            print(f"Version: {self.version_str}")

        # Parse segment table
        for i in range(self.seg_count):
            entry_addr = self.seg_table_ptr + i * 8
            entry = LibrarySegEntry.from_bytes(self.data[entry_addr:])
            # Firmware segment table end offsets appear to be inclusive: next.start == prev.end + 1.
            if entry.start >= len(self.data) or entry.end >= len(self.data) or entry.start > entry.end:
                print(f"  Segment {i}: INVALID (0x{entry.start:X} - 0x{entry.end:X})")
                continue

            self.seg_entries.append(entry)
            print(
                f"  Segment {i}: 0x{entry.start:05X}-0x{entry.end:05X}"
            )
        assert len(self.seg_entries) == self.seg_count

    def extract_all(self, output_dir):
        """Extract all audio segments to WAV files."""
        os.makedirs(output_dir, exist_ok=True)

        decoder = FirmwareDecoderContext(self.data)

        for idx, entry in enumerate(self.seg_entries):
            seg = self.get_segment(idx)
            if not seg:
                print(f"Skipping unavailable segment {idx}")
                continue

            codec = seg.codec
            outpath = os.path.join(
                output_dir, f"segment_{idx:02d}_{codec.name.replace('/', '_')}.wav"
            )

            print(f"\nExtracting Segment {idx} ({codec}, {len(seg):,} bytes)...")

            try:
                samples, sample_rate = decoder.decode_segment(seg)

                # Write WAV — IMDCT gain compensation produces correct amplitude
                self._write_wav(outpath, samples, sample_rate, normalize=False)
                """
                duration = len(samples) / sample_rate
                print(f"  -> {outpath} ({len(samples)} samples, {sample_rate}Hz, {duration:.2f}s)")
                if seg.codec_type in (0x1D, 0x1E) and vpe_decoder.last_debug_report:
                    debug_path = os.path.join(
                        output_dir,
                        f"segment_{idx:02d}_{codec.replace('/', '_')}_debug.txt"
                    )
                    with open(debug_path, 'w', encoding='utf-8') as f:
                        f.write(vpe_decoder.last_debug_report)
                """
            except Exception as e:
                print(f"  Error: {e}")

            # Also save raw segment data for reference
            raw_path = os.path.join(output_dir, f"segment_{idx:02d}_raw.bin")
            with open(raw_path, "wb") as f:
                f.write(seg.data)

    def get_segment_library_entry(self, index: int) -> LibrarySegEntry:
        return self.seg_entries[index]

    def get_segment(self, index: int) -> AudioSegment:
        """Return segment dict for a given segment index, or None if missing."""
        entry = self.get_segment_library_entry(index)
        return AudioSegment(self.data[entry.start:entry.end + 1])

    def get_all_segments(self) -> list[AudioSegment]:
        return [self.get_segment(i) for i in range(self.seg_count)]

    def patch_with_new_segments(self, new_segments: list[AudioSegment]) -> bytes:
        if not self.audiodata_area_known:
            raise Exception("Audiodata area not known?!?")
        
        fw_buf = bytearray(self.data)
        if not fw_buf:
            raise Exception("No firmware loaded.. how did you get here?")

        lib_entry_ptr = self.seg_table_ptr
        data_offset = self.audiodata_start_offset
        audio_limit = min(VPE_AUDIO_DATA_LIMIT, len(fw_buf))

        if data_offset >= audio_limit:
            raise Exception(
                    f"Invalid audio data region: start=0x{data_offset:05X}, "
                    f"limit=0x{audio_limit:05X}"
            )

        # Zero out existing audio data
        fw_buf[data_offset:audio_limit] = b"\xFF" * (audio_limit - data_offset)
        current_pos = data_offset

        for idx, segment in enumerate(new_segments):
            if segment.is_empty():
                raise Exception(
                    f"Segment {idx} is empty. Inject WAV/RAW or use Make Stub first."
                )

            data_len = len(segment.data)
            next_pos = current_pos + data_len

            if next_pos > audio_limit:
                raise Exception(
                        f"Audio payload exceeds 0x{VPE_AUDIO_DATA_LIMIT:05X} limit "
                        f"while writing segment {idx}."
                )

            lib_entry = LibrarySegEntry(current_pos, next_pos - 1)

            # Overwrite entry in fw buffer
            lib_entry_offset = lib_entry_ptr + (idx * 8)
            fw_buf[lib_entry_offset:lib_entry_offset + 8] = lib_entry.to_bytes()

            # Overwrite audio data in fw buffer
            fw_buf[current_pos:next_pos] = segment.data
            current_pos = next_pos

        struct.pack_into("<I", fw_buf, VPE_DATA_BOUNDARY_ADDR, current_pos)
        return bytes(fw_buf)

    @staticmethod
    def _write_wav(
        filepath, samples, sample_rate, channels=1, sample_width=2, normalize=False
    ):
        """Write PCM samples to WAV file."""
        if normalize and samples:
            # Normalize to use ~90% of dynamic range
            peak = max(abs(min(samples)), abs(max(samples)))
            if peak > 0:
                scale = 29000.0 / peak  # Target ~90% of int16 range
                samples = [int(max(-32768, min(32767, s * scale))) for s in samples]

        with wave.open(filepath, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(sample_width)
            wav.setframerate(sample_rate)

            # Pack samples as signed 16-bit little-endian
            packed = struct.pack(
                f"<{len(samples)}h", *[int(max(-32768, min(32767, s))) for s in samples]
            )
            wav.writeframes(packed)


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Extract audio from ISD9160 VPE firmware dumps"
    )
    parser.add_argument("firmware", help="Path to firmware binary (e.g., full.bin)")
    parser.add_argument(
        "output",
        nargs="?",
        default="extracted_audio",
        help="Output directory (default: extracted_audio)",
    )
    parser.add_argument(
        "--segment",
        "-s",
        type=int,
        default=None,
        help="Extract only specific segment index",
    )
    parser.add_argument(
        "--vpe-debug",
        action="store_true",
        help="Write per-segment VPE validation/debug reports",
    )
    parser.add_argument(
        "--patch-out",
        default=None,
        help="Write a patched firmware image to this path (enables patch mode)",
    )
    parser.add_argument(
        "--inject-raw",
        action="append",
        nargs=2,
        metavar=("IDX", "RAW_BIN"),
        help="Patch: replace entire segment bytes (must match exact size). Can be repeated.",
    )
    parser.add_argument(
        "--inject-wav",
        action="append",
        nargs=2,
        metavar=("IDX", "FRAMES_BIN"),
        help="Patch: replace only VPE compressed frames (must match seg.size-16). Can be repeated.",
    )
    args = parser.parse_args()

    print("ISD9160 VPE Audio Extractor")
    print("=" * 50)
    print(f"Firmware: {args.firmware}")
    print(f"Output:   {args.output}")
    print()

    fw: ISD9160Firmware = ISD9160Firmware.from_filepath(args.firmware)

    if args.patch_out is not None:
        new_segments = fw.get_all_segments()
        inject_seg_raw: list[tuple[int, str]] = []
        inject_seg_wav: list[tuple[int, str]] = []
        if args.inject_raw:
            for idx_s, path in args.inject_raw:
                inject_seg_raw.append((int(idx_s, 0), path))
        if args.inject_wav:
            for idx_s, path in args.inject_wav:
                inject_seg_wav.append((int(idx_s, 0), path))
        if not inject_seg_raw and not inject_seg_wav:
            raise SystemExit(
                "--patch-out requires at least one --inject-raw or --inject-wav"
            )
        if inject_seg_raw and inject_seg_wav:
            seg_ids_raw = [i for (i, _) in inject_seg_raw]
            seg_ids_wav = [i for (i, _) in inject_seg_wav]
            # TODO: Check for overlaps

        if inject_seg_wav:
            encoder = VPEEncoder(fw.data)
            for idx, wav_path in inject_seg_wav:
                print(f"\nEncoding WAV -> VPE for segment {idx}: {wav_path}")
                audio_segment = encoder.encode_wav_into_audio_segment(wav_path, ENCODING_BEST)
                new_segments[idx] = audio_segment
        if inject_seg_raw:
            for idx, raw_path in inject_seg_raw:
                print(f"\nInjecting raw segment for index {idx}: {raw_path}")
                with open(raw_path, "rb") as f:
                    audio_segment = AudioSegment(f.read())
                new_segments[idx] =  audio_segment

        new_fw_data = fw.patch_with_new_segments(new_segments)
        with open(args.patch_out, "wb") as f:
            f.write(new_fw_data)

        print(f"\nDone! Wrote patched firmware to {args.patch_out}")
        return

    if args.segment is not None:
        # Extract single segment
        seg = fw.seg_entries[0]
        if seg is None:
            print(f"Segment {args.segment} not found!")
            return
        fw.seg_entries = [seg]
        fw.seg_count = 1

    fw.extract_all(args.output)
    print(f"\nDone! Extracted {fw.seg_count} segment(s) to {args.output}/")


if __name__ == "__main__":
    main()
