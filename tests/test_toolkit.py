"""Synthetic fixtures keep the default suite independent of vendor binaries."""

from io import BytesIO, StringIO
import json
from pathlib import Path
import struct
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from PIL import Image

from tjc_hmi.builder import build_file, clone_control
from tjc_hmi.checksum import crc_type10_update, finalize_page, page_crc
from tjc_hmi.cli import main
from tjc_hmi.container import HmiProject
from tjc_hmi.image import compile_picture, decode_pixels, encode_pixels
from tjc_hmi.page import BlockField, PageBlock, PageFile, parse_page_data
from tjc_hmi.project import validate_project


def control(name, kind, index):
    values = {"objname": name.encode(), "type": kind.encode(), "id": index.to_bytes(2, "little")}
    values.update({key: value.to_bytes(2, "little") for key, value in
                   {"x": 0, "y": 0, "w": 480, "h": 272, "endx": 479, "endy": 271,
                    "pic": 0, "font": 0, "bco": 0, "pco": 65535, "xcen": 0, "ycen": 0,
                    "txt_maxl": 24}.items()})
    values["txt"] = b"--"
    return PageBlock("attr", 0, [BlockField(key, value, None) for key, value in values.items()],
                     ["codesup-1", "old.invalid=1"])


def page_payload(name="page0", capacity=16000):
    header = bytearray(56)
    struct.pack_into("<I", header, 8, 56)
    header[21] = 0x55
    blocks = [control(name, "y", 0), control("t0", "t", 1), control("m0", "m", 2)]
    page = PageFile(0, 56, len(blocks), bytes(header), name, blocks)
    return finalize_page(page.serialize(), capacity)


def fixture():
    entries = {f"{i}.pa": page_payload(f"page{i}") for i in range(4)}
    manifest = bytearray(96)
    for kind, suffix in ((b"pa", ".pa"), (b"i", ".i")):
        for index in range(4):
            manifest.extend(kind.ljust(8, b"\0") + f"{index}{suffix}".encode().ljust(8, b"\0"))
    entries["main.HMI"] = bytes(manifest)
    entries["Program.s"] = b" " * 426
    for index in range(4):
        entries[f"{index}.i"] = b"\0" * 180000
        entries[f"{index}.is"] = b"\0" * 100000
    raw = bytearray(b"\xff" * 0x700000)
    struct.pack_into("<I", raw, 0, len(entries))
    for index, (name, payload) in enumerate(entries.items()):
        struct.pack_into("<16sIII", raw, 4 + index * 28, name.encode(), len(raw), len(payload), 0)
        raw.extend(payload)
    return bytes(raw)


class ToolkitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = fixture()

    def test_container_resource_count(self):
        self.assertEqual(len(HmiProject(self.raw).resources), 14)

    def test_directory_truncated(self):
        with self.assertRaises(ValueError):
            HmiProject(b"\xff" * 4)

    def test_directory_payload_outside_file(self):
        raw = bytearray(self.raw)
        struct.pack_into("<I", raw, 20, len(raw) + 1)
        with self.assertRaises(ValueError):
            HmiProject(raw)

    def test_directory_overlapping_payloads(self):
        raw = bytearray(self.raw)
        raw[48:52] = raw[20:24]
        with self.assertRaises(ValueError):
            HmiProject(raw)

    def test_slot_overflow_rejected(self):
        with self.assertRaises(ValueError):
            HmiProject(self.raw).replace({"Program.s": b"x" * 427})

    def test_replace_preserves_unrelated_bytes(self):
        project = HmiProject(self.raw)
        result = project.replace({"Program.s": b"baud=115200"})
        slot = project.resources["Program.s"]
        self.assertEqual(project.raw[:slot.offset], result.raw[:slot.offset])
        self.assertEqual(project.raw[slot.offset + slot.length:], result.raw[slot.offset + slot.length:])

    def test_page_control_byte_roundtrip(self):
        page = parse_page_data(page_payload())
        for block in page.blocks:
            parsed = parse_page_data(finalize_page(PageFile(0, 56, 1, page.header_bytes,
                                                          page.page_name, [block]).serialize(), 16000))
            self.assertEqual(block.serialize(), parsed.blocks[0].serialize())

    def test_page_checksum_damage(self):
        payload = bytearray(page_payload())
        expected = int.from_bytes(payload[:4], "little")
        payload[-1] ^= 1
        self.assertNotEqual(page_crc(payload), expected)

    def test_page_invalid_count(self):
        payload = bytearray(page_payload())
        struct.pack_into("<I", payload, 12, 100000)
        with self.assertRaises(ValueError):
            parse_page_data(payload)

    def test_page_invalid_block_bounds(self):
        payload = bytearray(page_payload())
        struct.pack_into("<I", payload, 56, len(payload))
        with self.assertRaises(ValueError):
            parse_page_data(payload)

    def test_page_slot_overflow(self):
        with self.assertRaises(ValueError):
            finalize_page(page_payload(), 32)

    def test_clone_clears_inherited_events(self):
        original = control("old", "t", 1)
        cloned = clone_control(original, "new", 2)
        self.assertEqual(cloned.event_tokens, ["codesup-0"])
        self.assertEqual(original.event_tokens, ["codesup-1", "old.invalid=1"])

    def test_string_encoding_is_strict(self):
        with self.assertRaises(UnicodeEncodeError):
            control("t0", "t", 1).set_string("objname", "中文")

    def test_crc_independent_bitwise_reference(self):
        expected = 0xffffffff
        payload = bytes(range(256))
        for value in payload:
            expected ^= value
            for _ in range(32):
                expected = ((expected << 1) ^ (0x04c11db7 if expected & 0x80000000 else 0)) & 0xffffffff
        self.assertEqual(crc_type10_update(0xffffffff, payload), expected)

    def test_image_run_boundaries_and_roundtrip(self):
        pixels = [1] * 28 + [2] * 15 + list(range(21))
        body = encode_pixels(pixels, 32, 2)
        self.assertEqual(decode_pixels(body, 32, 2), pixels)

    def test_editor_literal_marker(self):
        body = bytearray(encode_pixels(list(range(8)), 8, 1))
        body[20] = (body[20] & 0xf0) | 15
        self.assertEqual(decode_pixels(body, 8, 1), list(range(8)))

    def test_image_corrupt_index(self):
        body = bytearray(encode_pixels([1] * 8, 8, 1))
        body[-1] ^= 1
        with self.assertRaises(ValueError):
            decode_pixels(body, 8, 1)

    def test_image_truncated(self):
        with self.assertRaises(ValueError):
            decode_pixels(b"\0" * 19, 8, 1)

    def test_image_resource_headers(self):
        runtime, source = compile_picture(Image.new("RGB", (16, 8), "#abcdef"), 32)
        self.assertEqual(runtime[:4], b"\x0a\x60\x01\x03")
        self.assertEqual(source[24:27], b"png")
        with Image.open(BytesIO(source[27:])) as image:
            self.assertEqual(image.size, (16, 8))

    def test_export_rejects_traversal(self):
        raw = bytearray(self.raw)
        raw[4:20] = b"../escape".ljust(16, b"\0")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                HmiProject(raw).export(directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cli_errors_are_nonzero(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["validate", "missing.HMI"]), 1)

    def test_cli_inspect_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.HMI"
            source.write_bytes(self.raw)
            stream = StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(["inspect", str(source)]), 0)
            self.assertEqual(len(json.loads(stream.getvalue())["pages"]), 4)

    def test_build_entry_point_and_determinism(self):
        layout = Path(__file__).resolve().parents[1] / "examples/greenhouse/layout.json"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "template.HMI"
            source.write_bytes(self.raw)
            output = Path(directory) / "generated.HMI"
            result = build_file(source, source, layout, output)
            self.assertEqual(source.read_bytes(), self.raw)
            self.assertEqual(output.read_bytes(), result.raw)
            self.assertTrue(validate_project(result, True, True)["valid"])
            repeated = build_file(source, source, layout, output)
            self.assertEqual(repeated.raw, result.raw)

    def test_build_rejects_input_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "template.HMI"
            source.write_bytes(self.raw)
            with self.assertRaises(ValueError):
                build_file(source, source, "missing.json", source)
            self.assertEqual(source.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
