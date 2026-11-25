"""RGB565 run blocks for the observed 480x272 picture resource variant."""

from io import BytesIO
import struct

from PIL import Image


def rgb565(color):
    red, green, blue = bytes.fromhex(color.removeprefix("#"))
    return (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3


def encode_pixels(pixels, width, height):
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        raise ValueError("Invalid image dimensions or pixel count")
    chunks, indices, rows = bytearray(), bytearray(), []
    for y in range(height):
        rows.append((20 + len(chunks), len(indices)))
        runs = []
        for color in pixels[y * width:(y + 1) * width]:
            if runs and color == runs[-1][1] and runs[-1][0] < 14:
                runs[-1][0] += 1
            else:
                runs.append([1, color])
        runs.extend([[0, 0] for _ in range((-len(runs)) % 8)])
        for offset in range(0, len(runs), 8):
            group = runs[offset:offset + 8]
            lengths = [item[0] for item in group]
            indices.append(sum(lengths))
            chunks.extend(bytes(lengths[i] | lengths[i + 1] << 4 for i in range(0, 8, 2)))
            chunks.extend(struct.pack("<8H", *(item[1] for item in group)))
    row_table = 20 + len(chunks)
    index_start = row_table + height * 8
    table = b"".join(struct.pack("<II", block, index_start + index) for block, index in rows)
    return struct.pack("<5I", 1, row_table, 0, 0, 0) + chunks + table + indices


def decode_pixels(body, width, height):
    if width <= 0 or height <= 0 or len(body) < 20:
        raise ValueError("Invalid picture dimensions or header")
    row_table = struct.unpack_from("<I", body, 4)[0]
    table_end = row_table + height * 8
    if row_table < 20 or table_end > len(body):
        raise ValueError("Picture row table is outside resource")
    pixels = []
    for y in range(height):
        position, index = struct.unpack_from("<II", body, row_table + y * 8)
        row = []
        while len(row) < width:
            if position < 20 or position + 20 > row_table or not table_end <= index < len(body):
                raise ValueError("Picture run block is outside resource")
            tags = body[position:position + 4]
            lengths = [(tags[i // 2] >> (4 * (i % 2))) & 15 for i in range(8)]
            if lengths[0] == 15:
                lengths[0] = 1
            if not sum(lengths) or sum(lengths) != body[index]:
                raise ValueError("Picture run count mismatch")
            for count, color in zip(lengths, struct.unpack_from("<8H", body, position + 4)):
                row.extend([color] * count)
            if len(row) > width:
                raise ValueError("Picture row exceeds width")
            position += 20
            index += 1
        pixels.extend(row)
    return pixels


def image_pixels(image):
    data = image.convert("RGB").tobytes()
    return [(red >> 3) << 11 | (green >> 2) << 5 | blue >> 3
            for red, green, blue in zip(data[0::3], data[1::3], data[2::3])]


def compile_picture(image, colors):
    indexed = image.convert("RGB").quantize(colors=colors, dither=Image.Dither.NONE)
    stream = BytesIO()
    indexed.save(stream, "PNG", optimize=True)
    png = stream.getvalue()
    pixels = image_pixels(indexed)
    width, height = indexed.size
    body = encode_pixels(pixels, width, height)
    if decode_pixels(body, width, height) != pixels:
        raise ValueError("Picture encoding roundtrip failed")
    runtime = struct.pack("<4sIIHHII", b"\x0a\x60\x01\x03", 0, 24, width, height, len(body), 0) + body
    source = struct.pack("<4sIIHHII", b"\x0a\x64\x01\x01", 0, 27, width, height, len(png), 0) + b"png" + png
    return runtime, source
