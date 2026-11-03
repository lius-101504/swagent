"""Checks that need no SolidWorks, no licence and no network beyond one TCP connect.

The point is to separate "the code is broken" from "the licence server is
unreachable". Run this first whenever something looks wrong; if it passes, any
remaining failure is the environment.
"""
import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swagent import tools
from swagent.errors import SwError
from swagent.png import bmp_to_png
from swagent.session import Session

PASS, FAIL = [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def write_test_bmp(path, width=4, height=3):
    """A tiny 24bpp bottom-up BI_RGB BMP, the shape SolidWorks' SaveBMP produces."""
    row_pad = (4 - (width * 3) % 4) % 4
    pixels = b""
    for y in range(height):
        row = b""
        for x in range(width):
            row += bytes((x * 40 % 256, y * 60 % 256, 128))
        pixels += row + b"\x00" * row_pad
    size = 14 + 40 + len(pixels)
    header = b"BM" + struct.pack("<IHHI", size, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    with open(path, "wb") as fh:
        fh.write(header + info + pixels)


def test_png(tmp):
    bmp = os.path.join(tmp, "test.bmp")
    png = os.path.join(tmp, "test.png")
    write_test_bmp(bmp)
    width, height = bmp_to_png(bmp, png)
    with open(png, "rb") as fh:
        signature = fh.read(8)
    check("bmp_to_png returns dimensions", (width, height) == (4, 3), f"got {(width, height)}")
    check("bmp_to_png writes a PNG signature", signature == b"\x89PNG\r\n\x1a\n", repr(signature))
    check("bmp_to_png writes a non-empty file", os.path.getsize(png) > 50)


def test_tool_schemas():
    names = [t["name"] for t in tools.TOOLS]
    check("tool names are unique", len(names) == len(set(names)), f"duplicates in {names}")
    check("39 tools are exposed", len(names) == 39, f"got {len(names)}")
    import inspect
    source = inspect.getsource(tools.call)
    missing = [name for name in names if f'"{name}"' not in source]
    check("every tool has a dispatch branch", not missing, f"missing: {missing}")
    problems = []
    for tool in tools.TOOLS:
        schema = tool.get("inputSchema") or {}
        if schema.get("type") != "object":
            problems.append(f"{tool['name']}: inputSchema is not an object")
        properties = schema.get("properties") or {}
        for required in schema.get("required", []):
            if required not in properties:
                problems.append(f"{tool['name']}: required {required} is not a declared property")
        if not tool.get("description"):
            problems.append(f"{tool['name']}: no description")
    check("every tool schema is well formed", not problems, "; ".join(problems))
    check("every tool name is prefixed sw_", all(n.startswith("sw_") for n in names))


def test_output_guard(tmp):
    inside = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out.step")
    check("accepts a path inside the workspace", tools.resolve_output(inside) == os.path.abspath(inside))
    try:
        tools.resolve_output(r"C:\Windows\Temp\escape.step")
    except SwError as exc:
        check("rejects a path outside the workspace", exc.code == "path_not_allowed", exc.code)
    else:
        check("rejects a path outside the workspace", False, "no error raised")
    try:
        tools.resolve_output(r"D:\d_evil\x.step")
    except SwError:
        check("does not accept a sibling prefix like D:\\d_evil", True)
    else:
        check("does not accept a sibling prefix like D:\\d_evil", False, "prefix match was too loose")


def test_license_probe():
    probe = Session.license_probe()
    check("licence probe returns a dict", isinstance(probe, dict))
    if probe.get("configured"):
        check("licence probe reports a server", bool(probe.get("server")), json.dumps(probe))
        print(f"       licence server {probe.get('server')} reachable={probe.get('reachable')}")
    else:
        print("       no SolidNetWork licence configured on this machine")


def test_error_payloads():
    exc = SwError("boom", code="test_code", hint="try again", details={"k": 1})
    payload = exc.to_dict()
    check(
        "SwError serialises to a complete payload",
        payload["code"] == "test_code" and payload["hint"] == "try again" and payload["details"] == {"k": 1},
        json.dumps(payload),
    )
    check("SwError marks itself not ok", payload["ok"] is False)


def main():
    print("== offline checks ==")
    with tempfile.TemporaryDirectory() as tmp:
        test_png(tmp)
        test_output_guard(tmp)
        test_license_probe()
        test_error_payloads()
    test_tool_schemas()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for failure in FAIL:
        print("  -", failure)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
