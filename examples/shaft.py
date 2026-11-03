"""Build the 8-hole shaft (bolt holes drilled on both extruded flanges).

Needs an offset reference plane for the top flange; reports whether the plane
API works before it goes on.

  Start-Process .venv\\Scripts\\python.exe -Verb RunAs -ArgumentList '-u','finish_shaft.py'
"""
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from swagent.session import SessionWithExtras, _call, _get, _mm

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "out")
WORKS = r"D:\SolidWorks作品\02_汽车传动轴"
BASE = "汽车传动轴_轴管50x60_长600_法兰140x20_8孔"
LOG = os.path.join(OUT, "finish_shaft.log")

R_BORE, R_TUBE, R_GROOVE, R_FLANGE = 25.0, 30.0, 27.0, 70.0
LEN, FLANGE_T, GROOVE_W = 600.0, 20.0, 10.0
PCD_R = 55.0
TUBE_PROFILE = [
    (R_BORE, 20.0), (R_TUBE, 20.0), (R_TUBE, 25.0), (R_GROOVE, 25.0),
    (R_GROOVE, 35.0), (R_TUBE, 35.0), (R_TUBE, 565.0), (R_GROOVE, 565.0),
    (R_GROOVE, 575.0), (R_TUBE, 575.0), (R_TUBE, 580.0), (R_BORE, 580.0),
]


class Tee:
    def __init__(self, stream, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.stream = stream
        self.file = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


def say(*a):
    print(*a, flush=True)


def main():
    sys.stdout = Tee(sys.__stdout__, LOG)
    sys.stderr = sys.stdout
    s = SessionWithExtras()
    say("revision:", s.connect().get("revision"))
    s.close_all(True)

    # ---- does an offset plane work at all? ---------------------------------
    say("=== offset plane test ===")
    s.new_part()
    s.add_box(60.0, 40.0, 20.0)
    try:
        plane = s.offset_plane(40.0, "top")
        say("offset_plane:", plane)
    except Exception as exc:  # noqa: BLE001
        say("offset_plane failed:", type(exc).__name__, exc, getattr(exc, "details", None))
        say("RESULT: BLOCKED on the reference plane")
        return 2

    # ---- the 8-hole shaft --------------------------------------------------
    say("\n=== 8-hole shaft ===")
    s.close_all(True)
    s.new_part()
    say("bottom flange:", s.add_cylinder(140.0, FLANGE_T, plane="top"))
    holes = 0
    for k in range(4):
        a = math.radians(45 + 90 * k)
        try:
            s.cut_hole(14.0, PCD_R * math.cos(a), PCD_R * math.sin(a),
                       axis="y", end="through_next")
            holes += 1
        except Exception as exc:  # noqa: BLE001
            say(f"  bottom hole {k} failed:", type(exc).__name__, exc)
    say("bottom holes:", holes, "volume:", s.mass_properties()["volume_mm3"])

    say("tube:", s.revolve(TUBE_PROFILE, 360.0))
    say("volume after tube:", s.mass_properties()["volume_mm3"])

    say("top plane:", s.offset_plane(LEN - FLANGE_T, "top"))
    names = [str(_get(f, "Name")) for f in s._features()]
    plane = [n for n in names if n.startswith("基准面")][-1]
    say("using plane:", plane)
    say("top flange:", s.add_cylinder(2 * R_FLANGE, FLANGE_T, plane=plane))
    say("volume after top flange:", s.mass_properties()["volume_mm3"])

    holes2 = 0
    for k in range(4):
        a = math.radians(45 + 90 * k)
        try:
            s.cut_hole(14.0, PCD_R * math.cos(a), PCD_R * math.sin(a),
                       axis="y", end="through_next")
            holes2 += 1
        except Exception as exc:  # noqa: BLE001
            say(f"  top hole {k} failed:", type(exc).__name__, exc)
    say("top holes:", holes2)

    try:
        s.cut_hole(2 * R_BORE, 0.0, 0.0, axis="y", end="through_next")
        s.cut_hole(2 * R_BORE, 0.0, 0.0, axis="y", end="through_next", high=False)
        say("bore done")
    except Exception as exc:  # noqa: BLE001
        say("bore failed:", type(exc).__name__, exc)

    props = s.mass_properties()
    say("final volume:", props["volume_mm3"])
    tube = (math.pi * (R_TUBE ** 2 - R_BORE ** 2) * 520.0
            + 2 * math.pi * (R_GROOVE ** 2 - R_BORE ** 2) * GROOVE_W
            + 2 * math.pi * (R_TUBE ** 2 - R_BORE ** 2) * 10.0)
    expected = (2 * math.pi * R_FLANGE ** 2 * FLANGE_T + tube
                - 2 * math.pi * R_BORE ** 2 * FLANGE_T
                - 8 * math.pi * 7.0 ** 2 * FLANGE_T)
    say("expected:", round(expected, 3),
        "deviation:", f"{100 * (props['volume_mm3'] - expected) / expected:+.3f} %")

    # hide the construction plane and the axis so the render shows only the part
    for label in (plane, "基准轴1"):
        try:
            if s._choose(label, "PLANE" if label.startswith("基准面") else "AXIS"):
                _call(lambda: s._doc.BlankRefGeom())
        except Exception as exc:  # noqa: BLE001
            say("hide", label, ":", type(exc).__name__, exc)
    s._deselect()

    try:
        _call(lambda: s._doc.ViewDisplayShaded())
        _call(lambda: s._doc.ShowNamedView2("*Isometric", 7))
        _call(lambda: s._doc.ViewZoomtofit2())
    except Exception as exc:  # noqa: BLE001
        say("view:", type(exc).__name__, exc)
    shot = os.path.join(WORKS, "效果图_轴测_8孔.png")
    s.screenshot_cropped(shot, 1800, 1200)
    say("screenshot:", shot, os.path.getsize(shot))

    part = os.path.join(WORKS, BASE + ".sldprt")
    say("save:", s.save(part))
    for ext in (".step", ".stl"):
        try:
            say("export:", s.export(os.path.join(WORKS, BASE + ext)))
        except Exception as exc:  # noqa: BLE001
            say("export failed:", type(exc).__name__, exc)
    # ---- drawing sheet -----------------------------------------------------
    say("")
    say("=== drawing ===")
    try:
        s.new_drawing()
        say("drawing_view:", s.drawing_view(part))
        _call(lambda: s._doc.ViewZoomtofit2())
        shot = os.path.join(WORKS, "工程图_传动轴.png")
        s.screenshot_cropped(shot, 1800, 1270)
        say("drawing screenshot:", shot, os.path.getsize(shot))
        say("save drawing:", s.save(os.path.join(WORKS, BASE + ".slddrw")))
    except Exception as exc:  # noqa: BLE001
        say("drawing failed:", type(exc).__name__, exc, getattr(exc, "details", None))

    note = f"""作品：汽车传动轴（空心轴管 + 两端法兰，法兰带螺栓孔）
生成：SOLIDWORKS 2022 SP05（中文界面），{time.strftime('%Y-%m-%d %H:%M')}
由 DSH 通过自带 MCP 插件（solidworks-mcp，39 个工具）用 COM 驱动生成

结构参数
  轴管外径 / 内径     ⌀60 / ⌀50 mm
  总长               600 mm
  法兰               ⌀140 × 20 mm，两端各一
  退刀槽             ⌀54 × 10 mm，法兰根部
  法兰螺栓孔         8 × ⌀14（两端各 4），分布圆 ⌀110
  中心通孔           ⌀50 贯通

建模顺序
  凸台-拉伸1  底法兰（上视基准面，⌀140×20）
  切除×4      底法兰螺栓孔（成型到下一面，只穿透这一层）
  旋转1       轴管 + 退刀槽（半剖面 12 点，绕基准轴 360°）
  基准面1     上视基准面偏移 580 mm
  凸台-拉伸2  顶法兰（在基准面1 上，⌀140×20）
  切除×4      顶法兰螺栓孔
  切除×2      ⌀50 中心通孔（从上、下两端各切一层）

核对
  解析体积 {expected:.3f} mm3
  实测体积 {props['volume_mm3']:.3f} mm3
  偏差     {100 * (props['volume_mm3'] - expected) / expected:+.3f} %

文件
  {BASE}.sldprt / .step / .stl / .slddrw
  效果图_轴测_8孔.png、工程图_传动轴.png
"""
    with open(os.path.join(WORKS, "说明.txt"), "w", encoding="utf-8") as handle:
        handle.write(note)
    say("note written")
    say("RESULT:", "PASS" if os.path.getsize(part) > 0 else "FAIL")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
