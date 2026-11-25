"""Project inspection and validation of observed pages and RGB565 backgrounds."""

from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import struct

from PIL import Image

from .checksum import page_crc
from .image import decode_pixels, image_pixels
from .page import parse_page_data


def inspect_project(project):
    pages = []
    for index, name in enumerate(project.manifest(b"pa")):
        page = parse_page_data(project.payload(name))
        pages.append({"id": index, "entry": name, "name": page.page_name,
                      "controls": [{"name": block.objname, "type": block.type_code,
                                    "events": block.event_tokens} for block in page.blocks]})
    return {"size": len(project.raw), "sha256": sha256(project.raw).hexdigest(),
            "resources": [asdict(item) | {"metadata": item.metadata.hex()}
                          for item in project.resources.values()], "pages": pages}


def validate_project(project, greenhouse=False, pictures=False):
    project.validate_mirrors()
    pages = []
    for entry in project.manifest(b"pa"):
        payload = project.payload(entry)
        page = parse_page_data(payload)
        if page.magic != page_crc(payload):
            raise ValueError(f"Page checksum mismatch: {entry}")
        names = [block.objname for block in page.blocks]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate control names: {entry}")
        for index, block in enumerate(page.blocks):
            field = block.get_field("id")
            if field and int.from_bytes(field.value, "little") != index:
                raise ValueError(f"Control ID mismatch: {entry}/{block.objname}")
        pages.append(page)
    if greenhouse:
        validate_greenhouse(pages)
    checked_pictures = 0
    if pictures:
        entries = project.manifest(b"i")
        seen = set()
        for page in pages:
            field = page.blocks[0].get_field("pic")
            if field is None:
                continue
            index = int.from_bytes(field.value, "little")
            if index >= len(entries):
                raise ValueError(f"Picture reference outside manifest: {page.page_name}")
            key = entries[index]
            if key not in seen:
                validate_picture(project, key)
                seen.add(key)
                checked_pictures += 1
    return {"valid": True, "pages": len(pages), "pictures_compared": checked_pictures,
            "greenhouse": greenhouse}


def validate_picture(project, key):
    runtime, source = project.payload(key), project.payload(key + "s")
    if runtime[:4] != b"\x0a\x60\x01\x03" or source[24:27] != b"png":
        raise ValueError(f"Unsupported picture variant: {key}")
    width, height = struct.unpack_from("<HH", runtime, 12)
    with Image.open(BytesIO(source[27:])) as image:
        image.load()
        if image.size != (width, height):
            raise ValueError(f"Picture dimensions mismatch: {key}")
        expected = image_pixels(image)
    if decode_pixels(runtime[24:], width, height) != expected:
        raise ValueError(f"Runtime and preview pixels differ: {key}")


def validate_greenhouse(pages):
    expected = [f"page{index}" for index in range(4)]
    if [page.page_name for page in pages[:4]] != expected:
        raise ValueError("Greenhouse requires page0..page3 at logical IDs 0..3")
    required_fields = ({"t0", "t1", "t2", "t6"}, {f"t{i}" for i in range(6)},
                       {"t7", "t8", "t9"}, {"t10", "t11"})
    graph, commands = {}, set()
    for page, fields in zip(pages[:4], required_fields):
        names = {block.objname for block in page.blocks}
        if not fields <= names:
            raise ValueError(f"Missing dynamic fields: {page.page_name}")
        root_events = page.blocks[0].event_tokens
        if root_events.count("sendme") != 1:
            raise ValueError(f"Missing or repeated sendme: {page.page_name}")
        graph[page.page_name] = set()
        for block in page.blocks:
            geometry = [block.get_field(key) for key in ("x", "y", "w", "h")]
            if any(value is None for value in geometry):
                raise ValueError(f"Missing geometry: {block.objname}")
            x, y, width, height = [int.from_bytes(field.value, "little") for field in geometry]
            if not (0 <= x < x + width <= 480 and 0 <= y < y + height <= 272):
                raise ValueError(f"Control outside screen: {block.objname}")
            for token in block.event_tokens:
                if token.startswith("page "):
                    target = token[5:]
                    if target not in expected:
                        raise ValueError(f"Invalid page target: {target}")
                    graph[page.page_name].add(target)
                elif token.startswith("printh "):
                    packet = bytes.fromhex(token[7:])
                    if len(packet) != 6 or packet[1:] != b"\0\0\xff\xff\xff":
                        raise ValueError(f"Invalid command frame: {block.objname}")
                    commands.add(packet[0])
    if commands != set(range(1, 8)):
        raise ValueError("Greenhouse command set must contain 01 through 07")
    for start in expected:
        pending, reached = [start], set()
        while pending:
            current = pending.pop()
            if current not in reached:
                reached.add(current)
                pending.extend(graph[current] - reached)
        if reached != set(expected):
            raise ValueError(f"Incomplete navigation from {start}")

