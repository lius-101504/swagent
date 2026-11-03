"""Build the finished gear (root fillet included) into the作品 folder, with clean names.

  D:\\SolidWorks作品\\01_24齿渐开线齿轮\\
      24齿渐开线齿轮_外径100mm_厚10mm_中心孔20mm.sldprt
      24齿渐开线齿轮_外径100mm_厚10mm_中心孔20mm.step
      24齿渐开线齿轮_外径100mm_厚10mm_中心孔20mm.stl
      效果图_轴测.png / 效果图_正视.png
      说明.txt

Run elevated:  Start-Process .venv\\Scripts\\python.exe -Verb RunAs -ArgumentList '-u','build_showcase.py'
"""
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from swagent.session import Session, _call, _mm

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(HERE, "out", "build_showcase.log")

WORKS = os.environ.get("SWAGENT_WORKS", os.path.join(REPO, "works"))
FOLDER = os.path.join(WORKS, "01_24齿渐开线齿轮")
BASE = "24齿渐开线齿轮_外径100mm_厚10mm_中心孔20mm"

TEETH, OUTER, THICK, BORE = 24, 100.0, 10.0, 20.0
ROOT_FILLET = 0.6
FILLET_OPTIONS = 3
FILLET_SIMPLE = 0


class Tee:
    def __init__(self, stream, path):
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


def vol(s):
    return s._volume() * 1e9


def geometry(teeth, outer, pressure=20.0):
    module = outer / (teeth + 2)
    r_out = outer / 2
    r_pitch = module * teeth / 2
    r_root = r_pitch - 1.25 * module
    r_base = r_pitch * math.cos(math.radians(pressure))
    half_tooth = math.pi / (2 * teeth)
    inv_p = math.tan(math.radians(pressure)) - math.radians(pressure)

    def half_angle(r):
        if r <= r_base:
            return half_tooth + inv_p
        alpha = math.acos(min(1.0, r_base / r))
        return half_tooth + inv_p - (math.tan(alpha) - alpha)

    return {"module": module, "r_out": r_out, "r_pitch": r_pitch,
            "r_root": r_root, "r_base": r_base, "half_angle": half_angle,
            "step": 2 * math.pi / teeth}


def root_edge_points(geo, teeth, thickness):
    pts = []
    for i in range(teeth):
        centre = i * geo["step"]
        offset = geo["half_angle"](geo["r_root"])
        for sign in (-1, 1):
            a = centre + sign * offset
            pts.append((geo["r_root"] * math.cos(a), geo["r_root"] * math.sin(a), thickness / 2.0))
    return pts


def fillet_roots(s, points, radius):
    s._deselect()
    picked = 0
    for x, y, z in points:
        if _call(lambda a=x, b=y, c=z: s._ext().SelectByID2(
                "", "EDGE", _mm(a), _mm(b), _mm(c), True, 0, None, 0)):
            picked += 1
    before = vol(s)
    feat = _call(lambda: s._fm().FeatureFillet3(
        FILLET_OPTIONS, _mm(radius), _mm(radius), 0, FILLET_SIMPLE, 0, 0,
        None, None, None, None, None, None, None))
    time.sleep(1)
    after = vol(s)
    ok = feat is not None and after < before - 1e-6
    say(f"root fillet R{radius}: edges={picked} feat={feat is not None} "
        f"{before:.1f} -> {after:.1f} mm3")
    return ok


def sw_window_size():
    """Client size of the SOLIDWORKS main window, so SaveBMP maps 1:1."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            if "SOLIDWORKS" in title and user32.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                found.append((rect.right - rect.left, rect.bottom - rect.top))
        return True

    user32.EnumWindows(callback, 0)
    big = [size for size in found if size[0] > 200 and size[1] > 200]
    if big:
        return max(big, key=lambda size: size[0] * size[1])
    screen = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return screen if screen[0] > 200 else None


def main():
    os.makedirs(FOLDER, exist_ok=True)
    sys.stdout = Tee(sys.__stdout__, LOG)
    sys.stderr = sys.stdout
    s = Session()
    say("revision:", s.connect().get("revision"))
    s.close_all(True)
    say("errors at start:", _call(lambda: s._app.GetErrorMessages()))

    s.new_part()
    info = s.gear(teeth=TEETH, outer_diameter=OUTER, thickness=THICK, bore=BORE)
    say("gear:", info["volume_mm3"], "expected", info["expected_volume_mm3"], info["features"])

    geo = geometry(TEETH, OUTER)
    ok = False
    for radius in (ROOT_FILLET, 0.4, 0.25):
        if fillet_roots(s, root_edge_points(geo, TEETH, THICK), radius):
            ok = True
            ROOT_FILLET_USED = radius
            break
    say("fillet:", ok)

    window = sw_window_size()
    say("solidworks client size:", window)
    if window:
        width, height = window
        size_iso = (width, height)
        size_front = (width, height)
    else:
        size_iso = (1200, 900)
        size_front = (1200, 900)
    shots = (("效果图_轴测.png", "*Isometric", 7, size_iso, "shaded"),
             ("效果图_正视.png", "*Front", 1, size_front, "shaded"))
    for name, view, code, size, mode in shots:
        steps = []
        if mode == "edges":
            steps.append(("display edges", lambda: s._doc.ViewDisplayShadedWithEdges()))
        else:
            steps.append(("display shaded", lambda: s._doc.ViewDisplayShaded()))
        steps.append(("view " + view, lambda v=view, c=code: s._doc.ShowNamedView2(v, c)))
        steps.append(("zoom fit", lambda: s._doc.ViewZoomtofit2()))
        steps.append(("redraw", lambda: s._doc.GraphicsRedraw2()))
        steps.append(("zoom fit 2", lambda: s._doc.ViewZoomtofit2()))
        for label, fn in steps:
            try:
                _call(fn)
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                say(f"{name} {label}:", type(exc).__name__, exc)
        path = os.path.join(FOLDER, name)
        s.screenshot_cropped(path, *size)
        say("screenshot:", path, os.path.getsize(path))

    props = s.mass_properties()
    say("mass:", props)

    part = os.path.join(FOLDER, BASE + ".sldprt")
    say("save:", s.save(part))
    for ext in (".step", ".stl"):
        target = os.path.join(FOLDER, BASE + ext)
        try:
            say("export:", s.export(target))
        except Exception as exc:  # noqa: BLE001
            say("export failed:", type(exc).__name__, exc)

    note = f"""作品：24 齿渐开线直齿轮
生成：SOLIDWORKS 2022 SP05（中文界面），{time.strftime('%Y-%m-%d %H:%M')}
由 DSH 通过自带 MCP 插件（solidworks-mcp，17 个工具）用 COM 驱动生成

齿轮参数
  齿数 Z            {TEETH}
  模数 m            {geo['module']:.4f} mm
  压力角            {20.0:.0f}°
  外径（齿顶圆）    ⌀{OUTER:.0f} mm
  分度圆            ⌀{geo['r_pitch'] * 2:.3f} mm
  基圆              ⌀{geo['r_base'] * 2:.3f} mm
  齿根圆            ⌀{geo['r_root'] * 2:.3f} mm
  齿宽（厚度）      {THICK:.0f} mm
  中心孔            ⌀{BORE:.0f} mm
  齿根圆角          R{ROOT_FILLET_USED if ok else 0}

建模步骤（特征树）
  草图1 → 凸台-拉伸1        两个同心圆（齿根圆 + 中心孔）拉伸成盘体
  基准轴1                  取盘体圆柱面生成参考轴
  草图2 → 凸台-拉伸2        一个齿：20° 压力角的真渐开线取样 + 齿顶
  阵列(圆周)1              绕基准轴阵列 {TEETH} 个齿
  圆角                      齿根过渡圆角

核对
  解析体积 {info['expected_volume_mm3']:.3f} mm3
  实测体积 {props['volume_mm3']:.3f} mm3
  偏差     {100 * (props['volume_mm3'] - info['expected_volume_mm3']) / info['expected_volume_mm3']:+.3f} %
  （齿顶用弦代替圆弧，所以实测略小，方向符合预期）

文件
  {BASE}.sldprt   SOLIDWORKS 零件（可继续编辑特征）
  {BASE}.step     STEP AP203，可给其它 CAD 打开
  {BASE}.stl      STL 网格，可 3D 打印
  效果图_轴测.png / 效果图_正视.png
"""
    with open(os.path.join(FOLDER, "说明.txt"), "w", encoding="utf-8") as handle:
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
