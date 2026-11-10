"""Load and verify firmware with explicit addresses / 加载及校验明确地址的固件."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile
from intelhex import IntelHex

MAX_IMAGE = 32 * 1024 * 1024


def load_image(path: str, address: int | None = None) -> tuple[list[tuple[int, bytes]], str]:
    """Parse ELF/HEX/BIN before connecting hardware / 连接硬件前解析固件."""
    source = Path(path).resolve()
    if not source.is_file() or source.stat().st_size > MAX_IMAGE:
        raise ValueError("Firmware missing or exceeds 32 MiB")
    original = source.read_bytes()
    segments = []
    suffix = source.suffix.lower()
    if suffix in (".elf", ".axf"):
        with BytesIO(original) as stream:
            elf = ELFFile(stream)
            if elf["e_machine"] != "EM_ARM" or not elf.little_endian or elf.elfclass != 32:
                raise ValueError("Expected 32-bit little-endian ARM ELF")
            for segment in elf.iter_segments():
                if segment["p_type"] == "PT_LOAD" and segment["p_filesz"]:
                    if segment["p_filesz"] > MAX_IMAGE:
                        raise ValueError("ELF load segment exceeds 32 MiB")
                    data = segment.data()
                    if len(data) != segment["p_filesz"]:
                        raise ValueError("Truncated ELF load segment")
                    segments.append((int(segment["p_paddr"]), data))
    elif suffix == ".hex":
        image = IntelHex(str(source))
        if sum(end - start for start, end in image.segments()) > MAX_IMAGE:
            raise ValueError("HEX payload exceeds 32 MiB")
        segments = [(start, bytes(image.tobinarray(start=start, end=end - 1))) for start, end in image.segments()]
    elif suffix == ".bin":
        if address is None:
            raise ValueError("BIN firmware requires an explicit flash_address")
        segments = [(address, original)]
    else:
        raise ValueError("Supported firmware: .elf, .axf, .hex, .bin")
    if not segments or sum(len(data) for _, data in segments) > MAX_IMAGE:
        raise ValueError("Empty or oversized firmware payload")
    segments.sort()
    previous_end = 0
    for start, data in segments:
        # Initial implementation deliberately supports STM32 internal main flash.
        if not (0x08000000 <= start < start + len(data) <= 0x0A000000):
            raise ValueError(f"Load range {start:#x}+{len(data):#x} is outside STM32 internal flash")
        if start < previous_end:
            raise ValueError("Overlapping load segments")
        previous_end = start + len(data)
    return segments, hashlib.sha256(original).hexdigest()


def flash_verified(link: Any, segments: list[tuple[int, bytes]]) -> dict[str, Any]:
    """Flash and independently read back every byte / 烧录后逐字节独立回读校验."""
    link.halt()
    if not link.halted():
        raise RuntimeError("Target did not halt; refusing to program")
    programmed = 0
    for address, data in segments:
        link.flash(data, address)
        programmed += len(data)
    # Verify the complete image after all downloads, including shared erase sectors.
    for address, data in segments:
        for offset in range(0, len(data), 4096):
            expected = data[offset : offset + 4096]
            actual = bytes(link.memory_read8(address + offset, len(expected)))
            if actual != expected:
                mismatch = next(
                    (i for i, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1]),
                    min(len(actual), len(expected)),
                )
                raise RuntimeError(f"Flash verification failed at {address + offset + mismatch:#x}; target left halted")
    return {
        "bytes_programmed": programmed,
        "verified": True,
        "ranges": [{"address": address, "size": len(data)} for address, data in segments],
    }
