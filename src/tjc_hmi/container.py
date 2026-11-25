"""Read resource tables and replace payloads while preserving their slots."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import struct
import tempfile


@dataclass(frozen=True)
class Resource:
    name: str
    offset: int
    length: int
    metadata: bytes


def read_directory(raw, offset=0):
    if offset < 0 or offset + 4 > len(raw):
        raise ValueError("Resource directory is outside the file")
    count = struct.unpack_from("<I", raw, offset)[0]
    if count > 100000 or offset + 4 + count * 28 > len(raw):
        raise ValueError("Resource directory is truncated or has an unsupported count")
    resources = {}
    table_end = offset + 4 + count * 28
    for index in range(count):
        start = offset + 4 + index * 28
        name_bytes, position, length = struct.unpack_from("<16sII", raw, start)
        metadata = bytes(raw[start + 24:start + 28])
        if metadata[0] or not name_bytes[0]:
            continue
        name = name_bytes.split(b"\0", 1)[0].decode("ascii")
        if name in resources:
            raise ValueError(f"Duplicate resource {name}")
        if position < table_end or position + length > len(raw):
            raise ValueError(f"Resource outside payload bounds: {name}")
        resources[name] = Resource(name, position, length, metadata)
    spans = sorted((item.offset, item.offset + item.length) for item in resources.values())
    for (_, previous_end), (start, _) in zip(spans, spans[1:]):
        if previous_end > start:
            raise ValueError("Resource payloads overlap")
    return resources


class HmiProject:
    def __init__(self, raw):
        self.raw = bytes(raw)
        self.resources = read_directory(self.raw)

    @classmethod
    def load(cls, path):
        return cls(Path(path).read_bytes())

    def payload(self, name):
        item = self.resources[name]
        return self.raw[item.offset:item.offset + item.length]

    def manifest(self, kind):
        main = self.payload("main.HMI")
        if len(main) < 96 or (len(main) - 96) % 16:
            raise ValueError("Unsupported main.HMI manifest layout")
        names = [main[i + 8:i + 16].rstrip(b"\0").decode("ascii")
                 for i in range(96, len(main), 16)
                 if main[i:i + 8].rstrip(b"\0") == kind]
        if len(names) != len(set(names)) or any(name not in self.resources for name in names):
            raise ValueError("Invalid manifest references")
        return names

    def replace(self, replacements):
        result = bytearray(self.raw)
        for name, payload in replacements.items():
            item = self.resources[name]
            if len(payload) > item.length:
                raise ValueError(f"Resource {name} needs {len(payload)} bytes, slot has {item.length}")
            result[item.offset:item.offset + item.length] = payload.ljust(item.length, b"\0")
        return HmiProject(result)

    def validate_mirrors(self):
        for offset in (0x80000, 0x380000):
            if offset + 4 > len(self.raw):
                continue
            count = struct.unpack_from("<I", self.raw, offset)[0]
            if count in (0, 0xffffffff):
                continue
            if read_directory(self.raw, offset) != self.resources:
                raise ValueError(f"Resource mirror mismatch at {offset:#x}")

    def export(self, destination):
        destination = Path(destination)
        for name in self.resources:
            if name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise ValueError(f"Unsafe resource filename {name!r}")
        destination.mkdir(parents=True, exist_ok=True)
        for name in self.resources:
            atomic_write(destination / name, self.payload(name))


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()

