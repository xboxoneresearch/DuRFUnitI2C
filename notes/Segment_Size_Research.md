# ISD9160 VPE/DPCM Segment Size Research

## Flash Layout (all retail firmwares)

```
0x00000 - 0x0BF33   Firmware code + VPE codec + tables
0x0BF34 - 0x0BFE3   EPV header + segment table (9 entries x 8 bytes)
0x0BFE4 - varies     Audio data (9 segments)
varies  - 0x22FFF   FREE ERASED FLASH (size varies per firmware)
0x23000              Hard max audio boundary (enforced by flash programmer at 0x2EB4)
0x23400+             More firmware code (peripheral drivers, ISRs)
```

### Key addresses

| Address | Field | Notes |
|---------|-------|-------|
| 0x8000 | VPE_FIRMWARE_HEADER.magic | 0x1155AAFF |
| 0x8008 | lib_table_ptr | Points to EPV header (0xBF34) |
| 0x800C | data_boundary | End-of-audio marker, must be <= 0x23000 |
| 0x8014 | vpe_init_ptr | Function pointer for VPE init |
| 0x8018 | vpe_decode_ptr | Function pointer for VPE decode |
| 0xBF34 | EPV header | Magic "EPV" + 0xCF |
| 0xBF40 | seg_table_ptr | Points to 0xBF9C |
| 0xBF44 | segment_count | 9 in all firmwares (uint16) |
| 0xBF9C | segment_table | 9 entries, 8 bytes each (start_u32, end_u32) |

---

## Player Architecture (audio_playback_task @ 0x5208)

### Command protocol (byte[0] of I2C control struct)

| Cmd | Action |
|-----|--------|
| 0xAA | Set volume (param in byte[1], 0x2000 = unity gain) |
| 0xAB | Pause/stop playback |
| 0xAC | VPE external play (data from SPI/I2C) |
| 0xAD | Play segment by index |
| 0xAE | Stop playback |
| 0x01 | Play even segment from index register (byte[9] << 1) |
| 0x02 | Play odd segment from index register (byte[9]*2 + 1) |

### Key findings from decompilation

1. **Segment count is a soft bounds check**: `if (index < count) { play } else { error_status(8); stop }`. No hardcoded 9.
2. **Segments accessed by index only, never enumerated**: No code loops through all segments.
3. **Frame count computed from segment size**: `(seg_end - seg_start - 4) / bytes_per_frame`. Larger segment = more frames = longer audio.
4. **Codec detected per-segment from first byte**: `first_byte & 0x1F == 0x1D or 0x1E` = VPE, else = DPCM. Any segment can be either codec.
5. **Address validation**: Just checks segment start < 0x1000000 (16MB flash bounds).
6. **End-of-data marker at 0x800C**: Used only by the flash programmer (FUN_00002EB4). Capped at 0x23000. Not used by the player itself.

### Codec detection and dispatch

```
first_byte & 0x1F:
  0x1D, 0x1E -> VPE path (iVar13=1): header at seg+4, data at seg+0x10
  all others -> DPCM path (iVar13=0): codec_dispatcher_init(seg_start, seg_end)
```

### DPWM output config

| Subbands | Sample rate | DPWM divider | Samples/frame |
|----------|-------------|--------------|---------------|
| <= 14 | 16 kHz | 0x30 (48) | 320 |
| > 14 (28) | 32 kHz | 0x18 (24) | 640 |

---

## Cross-Firmware Segment Table Survey

### Segments 3/4/5/6 -- Invariant across all retail firmwares

These have identical sizes, codec parameters, and relative positioning in every non-devkit firmware:

| Seg | Codec | 1st byte | Size (bytes) | Bitrate | Bits/frm | Subbands | Samp/frm | Rate | Notes |
|-----|-------|----------|-------------|---------|----------|----------|----------|------|-------|
| 3 | VPE | 0xBD | 1,856 | ~4 kbps | 80 | 14 | 320 | 16 kHz | Dummy (Disc 1 no-op trigger) |
| 4 | VPE | 0xBD | 756 | ~4 kbps | 80 | 14 | 320 | 16 kHz | Dummy (Disc 2 no-op trigger) |
| 5 | VPE | 0xBD | 436 | ~4 kbps | 80 | 14 | 320 | 16 kHz | Dummy (Disc 3 no-op trigger) |
| 6 | VPE | 0xDE | 496 | 48 kbps | 960 | 28 | 640 | 32 kHz | 0.08s system tone |

**Exception**: Skype devkit has real audio in seg 3 (22,096 bytes), seg 4 (3,136), seg 5 (5,056), seg 6 (9,616) -- all VPE at 48kbps/32kHz.

### Segments 7/8 -- Also invariant across retail firmwares

| Seg | Codec | 1st byte | Size (bytes) | Notes |
|-----|-------|----------|-------------|-------|
| 7 | DPCM | 0xDC | 12,905 | System sound, identical across retail FWs |
| 8 | DPCM | 0xDA | 512-640 | System sound, slight size variation |

### Segments 0/1/2 -- The "branded" audio (varies per firmware)

| Firmware | Seg 0 | Seg 1 | Seg 2 | Total 0-2 |
|----------|-------|-------|-------|-----------|
| full.bin | DPCM 21,015 | DPCM 16,482 | DPCM 30,377 | 67,874 |
| tacobell | VPE 11,776 | DPCM 16,482 | DPCM 30,376 | 58,634 |
| halo5 | VPE 16,456 | DPCM 17,560 | VPE 18,856 | 52,872 |
| cod_aw | VPE 16,816 | VPE 11,776 | VPE 14,656 | 43,248 |
| minecraft | VPE 15,736 | VPE 9,376 | VPE 9,136 | 34,248 |
| forza6 | VPE 16,576 | VPE 5,776 | VPE 12,136 | 34,488 |
| skype_dev | VPE 10,096 | VPE 5,536 | VPE 6,496 | 22,128 |

---

## Free Flash Per Firmware

Free flash = 0x23000 (hard max) minus end-of-data marker (at 0x800C).

| Firmware | End marker | Free flash | Dummy seg 3/4/5 | Total reclaimable |
|----------|-----------|------------|-----------------|-------------------|
| full.bin | 0x20B9C | 9,316 | 3,048 | **12,364** |
| tacobell | 0x1E7FC | 18,436 | 3,048 | **21,484** |
| halo5 | 0x1D17C | 24,196 | 3,048 | **27,244** |
| cod_aw | 0x1ABDC | 34,596 | 3,048 | **37,644** |
| minecraft | 0x18834 | 39,372 | 3,048 | **42,420** |
| forza6 | 0x189A4 | 39,516 | 3,048 | **42,564** |
| skype_dev | 0x2296C | 1,684 | N/A (real audio) | **1,684** |

### What the reclaimable space buys (at various bitrates)

| Bitrate | Bytes/frame | full.bin (12KB) | halo5 (27KB) | forza6 (42KB) |
|---------|-------------|-----------------|--------------|---------------|
| 16 kbps (16 kHz) | 40 | 6.2s | 13.6s | 21.3s |
| 32 kbps (16 kHz) | 80 | 3.1s | 6.8s | 10.6s |
| 48 kbps (32 kHz) | 120 | 2.1s | 4.5s | 7.1s |

---

## Segment Table Binary Format

Located at 0xBF9C. Each entry is 8 bytes:

```
struct segment_entry {
    uint32_t start_addr;   // First byte of segment (inclusive)
    uint32_t end_addr;     // Last byte of segment (inclusive)
};
```

### full.bin segment table (hex)

```
BF9C: e4 bf 00 00  fa 11 01 00   // Seg 0: 0xBFE4 - 0x111FA
BFA4: fc 11 01 00  5d 52 01 00   // Seg 1: 0x111FC - 0x1525D
BFAC: 60 52 01 00  08 c9 01 00   // Seg 2: 0x15260 - 0x1C908
BFB4: 0c c9 01 00  4b d0 01 00   // Seg 3: 0x1C90C - 0x1D04B  <-- dummy
BFBC: 4c d0 01 00  3f d3 01 00   // Seg 4: 0x1D04C - 0x1D33F  <-- dummy
BFC4: 40 d3 01 00  f3 d4 01 00   // Seg 5: 0x1D340 - 0x1D4F3  <-- dummy
BFCC: f4 d4 01 00  e3 d6 01 00   // Seg 6: 0x1D4F4 - 0x1D6E3
BFD4: e4 d6 01 00  4d 09 02 00   // Seg 7: 0x1D6E4 - 0x2094D
BFDC: 50 09 02 00  50 0b 02 00   // Seg 8: 0x20950 - 0x20B50
```

---

## VPE Segment Header Format

For segments where `first_byte & 0x1F == 0x1D or 0x1E`:

```
Offset  Size   Field
0x00    1      Codec byte (& 0x1F: 0x1D or 0x1E = VPE)
0x01    3      Padding / flags
0x04    4      Bitrate (uint32 LE) -- e.g., 0x0000BB80 = 48000
0x08    2      Codec subtype (int16 LE)
0x0A    2      Bits per frame (int16 LE) -- e.g., 0x03C0 = 960
0x0C    2      Num subbands (int16 LE) -- 14 (16kHz) or 28 (32kHz)
0x0E    2      Samples per frame (int16 LE) -- 320 (16kHz) or 640 (32kHz)
0x10    ...    Compressed frame data
```

Frame count = `(seg_size - 16) / (bits_per_frame / 8)`

Duration = `frame_count * samples_per_frame / sample_rate`

---

## Reorganization Strategy

### Shrinking dummy segments 3/4/5

Minimum viable VPE segment: 16-byte header + 1 frame. At 80 bits/frame (10 bytes), minimum = **26 bytes**.

To shrink:
1. Keep the first 26 bytes of each dummy segment intact (valid header + 1 frame)
2. Update segment table end pointers: `new_end = start + 25`
3. The freed bytes (between new ends and segment 6 start) become available

### Extending into free flash

1. Point a segment's end into the free flash region (up to 0x22FFF)
2. Update the end-of-data marker at 0x800C to the new boundary
3. Fill the new space with valid encoded frames during injection

### Combined approach

1. Shrink seg 3/4/5 to 26-byte stubs (frees 3,048 - 78 = 2,970 bytes)
2. Extend a target segment into the freed dummy space AND/OR into the free flash
3. Update segment table pointers + end marker
4. Only the segment table (0xBF9C, 72 bytes) and 1 header word (0x800C) need patching

### Notes

- Codec type is per-segment (first byte), so any segment can be converted VPE <-> DPCM
- The player auto-detects codec from first byte -- no global "codec mode"
- Segment order in the table does NOT need to match physical flash order
- The flash programmer validates data_boundary <= 0x23000 but the player itself doesn't check this limit
