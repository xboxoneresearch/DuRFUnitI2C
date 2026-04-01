# ISD9160 Firmware Reverse Engineering Notes

## Overview

This firmware is for a Nuvoton ISD9160 Soundcorder chip (ARM Cortex-M0).

## Memory Map

| Region      | Address Range           | Size  | Description               |
|-------------|------------------------ |-------|---------------------------|
| APROM       | 0x00000000 - 0x00007FFF | 32KB  | Application code          |
| VPE         | 0x00008000 - 0x000233FF | 111KB | Audio data + decoder code |
| LDROM       | 0x00023400 - 0x000243FF | 4KB   | Loader ROM                |
| SRAM        | 0x20000000 - 0x20002FFF | 12KB  | RAM                       |
| Peripherals | 0x40000000+             | -     | DPWM, I2S, ADC, etc.      |

## VPE Structure

### VPE Header (0x8000)

| Offset|  Size|  Value       | Description                                |
| ------| ---- | -------------| ------------------------------------------ |
| 0x00  |  4   |  FF AA 55 11 | Magic signature (custom)                   |
| 0x04  |  2   |  80 00       | Version/flags                              |
| 0x06  |  2   |  22 11       | Config                                     |
| 0x08  |  4   |  34 BF 00 00 | Offset to audio index (0xBF34)             |
| 0x0C  |  4   |  9C 0B 02 00 | Total VPE data size (0x20B9C)              |
| 0x10  |  4   |  1D 80 00 00 | Function ptr: decoder init (0x801D, Thumb) |
| 0x14  |  4   |  4D 80 00 00 | Function ptr: buffer setup (0x804D, Thumb) |
| 0x18  |  4   |  93 80 00 00 | Function ptr: decode frame (0x8093, Thumb) |
| 0x1C  |  -   |  ...         | Embedded ARM Thumb decoder code            |

### Audio Index Table (0xBF34)

| Offset | Size | Value       | Description                              |
| ------ | ---- | ----------- | -----------------------------------------|
| 0x00   |    4 |45 50 56 CF  | Signature "EPV" + 0xCF                   |
| 0x04   |    2 |      0C 00  | Unknown (12)                             |
| 0x06   |    2 |      20 09  | Unknown (0x0920)                         |
| 0x08   |    4 |83 00 00 00  | Unknown (131)                            |
| 0x0C   |    4 |9C BF 00 00  | Pointer to segment index table (0xBF9C)  |
| 0x10   |    4 |09 00 00 00  | Segment count: 9                         |
| 0x14   |    4 |00 00 00 00  | Reserved                                 |
| 0x18   |    4 |9C 0B 02 00  | Total data size (0x20B9C)                |

### Segment Index Table (0xBF9C)

Contains 9 pairs of (start_offset, end_offset) as 32-bit little-endian values.
Offsets are relative to VPE base (0x8000).

## Audio Segments

| Seg | VPE Offset | FW Address | Size | Codec | First Bytes |
|-----|------------|------------|------|-------|-------------|
| 0   |  0x3FE4    |  0xBFE4    |21014 |     6 | DC CA 04 06 |
| 1   |  0x91FC    | 0x111FC    |16481 |     6 | DC D5 EF 20 |
| 2   |  0xD260    | 0x15260    |30376 |     6 | CD 97 07 00 |
| 3   | 0x1490C    | 0x1C90C    | 1855 |     5 | BD FF B8 00 |
| 4   | 0x1504C    | 0x1D04C    |  755 |     5 | BD FF 4A 00 |
| 5   | 0x15340    | 0x1D340    |  435 |     5 | BD FF 2A 00 |
| 6   | 0x154F4    | 0x1D4F4    |  495 |     6 | DE FF 04 00 |
| 7   | 0x156E4    | 0x1D6E4    |12905 |     6 | DC D5 00 00 |
| 8   | 0x18950    | 0x20950    |  512 |     6 | DA 00 00 18 |

## Codec Format

### First Byte Encoding

```
Bit 7-5: Codec type (0-7)
Bit 4-3: Configuration bits
Bit 2-0: Sub-type / bit depth indicator
```

### Codec Types Used

- **Codec 5** (0xBD prefix): Used for segments 3, 4, 5
  - Header structure: `BD FF <len_lo> <len_hi> A0 0F 00 00 58 1B 50 00 0E 00 40 01`
  - VPE/Siren7-based encoding requiring emulator for decode
  - lower5 = 0x1D indicates VPE format

- **Codec 6** (0xDC/0xCD/0xDE/0xDA prefix): Used for segments 0, 1, 2, 6, 7, 8
  - DPCM encoding with variable bit depths
  - Frame size: 256 samples per frame at 16kHz
  - Uses delta prediction with step table

### Lower 5 Bits Behavior (Critical!)

The lower 5 bits of the first byte control how `decode_frame_main` reads control bytes:

| lower5 | Behavior                                          | Segments |
|--------|---------------------------------------------------|----------|
| 0x1C   | Control bytes read from data stream (stream mode) |  0, 1, 7 |
| 0x0D   | First byte reused as control byte for all frames  |        2 |
| 0x1A   | First byte reused as control byte for all frames  |        8 |
| 0x1D   | VPE format (requires emulator)                    |  3, 4, 5 |
| 0x1E   | VPE format (requires emulator)                    |        6 |

### Control Byte Format (decode_frame_main)

```
Bits 4-3 (mode):
  0x00 = End check / continue
  0x08 = Set parameters mode (reads 2 extra bytes)
  0x10 = DPCM frame mode (uses switch table for bits_per_sample)
  0x18 = Simple frame mode (direct bit expansion)

Bits 2-0 (value): Mode-specific parameter
  Mode 0x10: value -> bits_per_sample: {0:6, 1:7, 2:6, 3:6, 4:6, 5:8, 6:8, 7:8}
  Mode 0x18: value -> bits_per_sample: {0:8, 1:10, 2:16, 3:12}
```

### Decoding Flow (from cc_task @ 0x5208)

1. Check magic at 0x8000 matches expected value
2. Load audio library info from 0x8008
3. For each segment:
   - Read first byte to determine codec type
   - Call codec dispatcher (FUN_000002c2)
   - Dispatcher uses upper 3 bits as switch index
   - Appropriate decoder is called based on codec type
4. Decoded PCM samples output to DPWM peripheral

## Key Functions (renamed in Ghidra)

| Address | Name                    | Description                                   |
|---------|-------------------------|-----------------------------------------------|
| 0x5208  | cc_task                 | Main audio playback task                      |
| 0x02C2  | codec_dispatcher_init   | Codec dispatcher (switch on first byte >> 5)  |
| 0x0626  | decode_frame_main       | Frame decoder with control byte parsing       |
| 0x0428  | decode_frame_dpcm       | DPCM decoder with prediction                  |
| 0x0392  | decode_frame_simple     | Simple PCM decoder (direct bit expansion)     |
| 0x05E0  | decode_frame_initial    | Initial frame decoder with prediction setup   |
| 0x033A  | bitreader_read_bits     | Reads n bits from bitstream (MSB first)       |
| 0x03EE  | decode_delta_step       | Delta decoder using DPCM_STEP_TABLE           |
| 0x804C  | vpe_decoder_init        | VPE/Siren7 decoder initialization             |
| 0x8092  | vpe_decode_frame        | VPE/Siren7 frame decoder                      |

### DPCM Step Table (0x71D0)

8 x 16-bit step values for delta decoding:
```
{0x0000, 0x0084, 0x018C, 0x039C, 0x07BC, 0x0FFC, 0x207C, 0x417C}
```

## DPWM Audio Output

- DPWM register base: 0x40070000
- Sample rates: 16000 Hz or 32000 Hz
- Output format: 16-bit signed PCM
- Uses DMA

## Notes

- Segment 0 contains mostly `0x22` bytes after header - likely silence or very quiet audio
- The decoder code is partially embedded in VPE region (0x801C onwards)
