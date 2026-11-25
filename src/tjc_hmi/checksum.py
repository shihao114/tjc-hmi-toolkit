"""Observed type-10 page checksum, with a 32-bit update for every byte."""

import struct

POLYNOMIAL = 0x04C11DB7
INITIAL = 0xFFFFFFFF


def _table():
    values = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) ^ (POLYNOMIAL if value & 0x80000000 else 0)) & INITIAL
        values.append(value)
    return values


TABLE = _table()


def crc_type10_update(seed, payload):
    value = seed & INITIAL
    for byte in payload:
        value ^= byte
        for _ in range(4):
            value = ((value << 8) ^ TABLE[value >> 24]) & INITIAL
    return value


def page_crc(payload):
    if len(payload) < 56:
        raise ValueError("Page header is truncated")
    return crc_type10_update(INITIAL, payload[4:] + payload[4:8] + payload[12:16] + payload[20:22])


def finalize_page(payload, capacity):
    if len(payload) > capacity:
        raise ValueError(f"Page needs {len(payload)} bytes, slot has {capacity}")
    result = bytearray(payload)
    result.extend(bytes(capacity - len(result)))
    struct.pack_into("<I", result, 4, capacity)
    struct.pack_into("<I", result, 0, page_crc(result))
    return bytes(result)

