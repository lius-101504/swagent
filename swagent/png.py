"""Minimal BMP -> PNG conversion, standard library only.

SolidWorks' `IModelDoc2::SaveBMP` is the only screenshot primitive it exposes.
BMP is not a format the harness can display, PNG is, and pulling in Pillow just
for this would put the whole server behind a pip install - so the two format
pieces we actually need are implemented here: read an uncompressed BMP, write a
PNG with stored (filter 0) scanlines.
"""
import struct
import zlib


def _read_bmp(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size < 40:
        raise ValueError("unsupported BMP header")
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if compression != 0:
        raise ValueError(f"compressed BMP not supported (compression={compression})")
    if bpp not in (24, 32):
        raise ValueError(f"unsupported BMP depth {bpp}")

    bottom_up = height > 0
    height = abs(height)
    stride = ((width * bpp + 31) // 32) * 4
    rows = []
    for y in range(height):
        start = offset + y * stride
        row = data[start:start + stride]
        pixels = bytearray()
        for x in range(width):
            b = row[x * (bpp // 8) + 0]
            g = row[x * (bpp // 8) + 1]
            r = row[x * (bpp // 8) + 2]
            pixels += bytes((r, g, b))
        rows.append(bytes(pixels))
    if bottom_up:
        rows.reverse()
    return width, height, rows


def _chunk(tag, payload):
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def write_png(path, width, height, rows):
    raw = b"".join(b"\x00" + row for row in rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw, 6))
    png += _chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def bmp_to_png(bmp_path, png_path):
    width, height, rows = _read_bmp(bmp_path)
    write_png(png_path, width, height, rows)
    return width, height


def crop_bmp(bmp_path, png_path, pad=0.05, threshold=238):
    """Crop a SolidWorks BMP to the model and write a PNG.

    `SaveBMP` renders the whole window, which includes the FeatureManager tree
    and leaves the part off-centre; cropping to the non-background pixels gives
    a picture that is actually about the part.
    """
    width, height, rows = _read_bmp(bmp_path)
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        row = rows[y]
        for x in range(width):
            base = x * 3
            if (row[base] + row[base + 1] + row[base + 2]) // 3 < threshold:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0:
        write_png(png_path, width, height, rows)
        return width, height
    span_x = right - left + 1
    span_y = bottom - top + 1
    pad_x = int(span_x * pad)
    pad_y = int(span_y * pad)
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width - 1, right + pad_x)
    bottom = min(height - 1, bottom + pad_y)
    cropped = [rows[y][left * 3:(right + 1) * 3] for y in range(top, bottom + 1)]
    out_w = right - left + 1
    out_h = bottom - top + 1
    write_png(png_path, out_w, out_h, cropped)
    return out_w, out_h
