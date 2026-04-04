// ImHex pattern
// VPE structure in ISD9160 firmware

#include <std/core.pat>
#include <std/io.pat>
#include <std/hash.pat>

import type.base;

#define VPE_START 0x8000

struct VPEHeader {
  type::Hex<u32> magic;              // 0x1155AAFF
  type::Hex<u32> unknown;
  type::Hex<u32> library_offset;     // Offset to AudioLibraryHeader
  type::Hex<u32> total_size;         // Total size of VPE structure
  type::Hex<u32> funcptr_init;       // Function pointer to init routine
  type::Hex<u32> funcptr_setup;      // Function pointer to setup routine
  type::Hex<u32> funcptr_decode;     // Function pointer to decode routine
};

struct ExtendedMetadata {
  type::Hex<u32> reserved_0;         // Reserved/padding
  type::Hex<u32> total_size_dup;     // Total size (duplicates VPE header)
  type::Hex<u32> reserved_1;         // Reserved
  type::Hex<u32> reserved_2;         // Reserved
  type::Hex<u16> index_counter;      // Index or counter (typically 0)
  type::Hex<s16> sentinel;           // Sentinel value (-1/0xFFFF)
  type::Hex<u32> end_offset;         // End offset of VPE data
  type::Hex<u32> start_offset;       // Start offset (typically 0x8000)
  type::Hex<u32> reserved_3;         // Reserved
  type::Hex<u32> config_data[9];     // Configuration/metadata bytes
};

struct AudioLibraryHeader {
  char magic[4];          // "EPV" + 0xCF
  type::Hex<u64> unknown_data;     // Unknown/version/flags
  type::Hex<u32> audio_offset;       // Offset to segments array
  u32 segment_count;      // Number of segments
  ExtendedMetadata metadata;  // Extended metadata (84 bytes)
};

struct Segment {
  u32 start_offset;       // Start offset of segment data
  u32 end_offset;         // End offset (NOT size!)
  u8 data[end_offset - start_offset + 1] @ start_offset;
};

fn segmentSize(Segment seg) {
    return seg.end_offset - seg.start_offset + 1;
};

// Main structure layout
VPEHeader header @ VPE_START;

// Code section containing the actual functions
u8 code_section[header.library_offset - VPE_START - sizeof(VPEHeader)] @ VPE_START + sizeof(VPEHeader);

// Audio library structure
AudioLibraryHeader audio_lib @ header.library_offset;

// Segments array
Segment segments[audio_lib.segment_count] @ audio_lib.audio_offset;

char version_string[] @ segments[audio_lib.segment_count - 1].end_offset + 1;

u8 to_be_checksummed[header.total_size - VPE_START - 2] @ VPE_START[[hidden]];

type::Hex<u16> checksum @ header.total_size - 2;
type::Hex<u16> checksum_calc = std::hash::crc16(to_be_checksummed, 0xFFFF, 0x1021, 0, false, false);

// byteswap checksum
checksum_calc = ((checksum_calc & 0xFF00) >> 8) | ((checksum_calc & 0xFF) << 8);
if (checksum == checksum_calc) {
    std::print("[+] Hash valid :)");
} else {
    std::print("INVALID hash :( Got: {:#x}, expected: {:#x}", checksum_calc, checksum);
}
