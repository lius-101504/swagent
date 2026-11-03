"""SolidWorks session: the operations an agent actually asks for.

Binding choice — this is the whole reason this file looks the way it does.
win32com's dynamic (late) binding cannot drive SOLIDWORKS 2022 on this machine:
the document's IDispatch answers `GetIDsOfNames("FirstFeature")` but `Invoke`
then fails with `DISP_E_MEMBERNOTFOUND`, and `EnsureDispatch` refuses with "this
COM object can not automate the makepy process". comtypes builds its bindings
straight from `sldworks.tlb` and calls through them, which works.

One more quirk is handled explicitly: methods whose return value is a SAFEARRAY
of interface pointers (`IPartDoc.GetBodies2`, `IFace2.GetEdges`) raise
`KeyError: 9` inside comtypes *after* the call has already run. So geometry is
enumerated through the non-array accessors instead (`IFeature.GetBody`,
`IBody2.GetFirstFace`/`IFace2.GetNextFace`, `IFace2.GetFirstLoop` ->
`ICoEdge.GetNext`), and calls whose result is unreadable but unnecessary are
issued through :func:`_call`.

Signatures used here were read out of the SOLIDWORKS 2022 type library
(`sldworks.tlb`), not recalled from documentation: 2022 differs from the
versions the public MCP servers target.
"""
import ctypes
import glob
import math
import os
import socket
import time

from .errors import NoDocument, NotConnected, SwError
from .png import bmp_to_png, crop_bmp

# --- constants, read from swconst.tlb (SOLIDWORKS 2022) -------------------
DOC_PART = 1
SAVE_AS_SILENT = 1
SAVE_AS_CURRENT_VERSION = 0
END_COND_BLIND = 0
END_COND_THROUGH_ALL = 1
END_COND_THROUGH_ALL_BOTH = 9
FILLET_PROPAGATE = 1
FILLET_UNIFORM_RADIUS = 2
FILLET_TYPE_SIMPLE = 0

TYPE_LIB = None   # resolved on first use by swagent.locate.solidworks_typelib()

EXPORT_FORMATS = {
    ".step": "STEP", ".stp": "STEP", ".stl": "STL", ".igs": "IGES", ".iges": "IGES",
    ".x_t": "Parasolid", ".x_b": "Parasolid", ".3mf": "3MF", ".pdf": "PDF",
    ".sldprt": "SOLIDWORKS part", ".sldasm": "SOLIDWORKS assembly",
}

_LIB = None


def lib():
    """The comtypes module generated from the SOLIDWORKS type library."""
    global _LIB, TYPE_LIB
    if _LIB is None:
        import comtypes.client

        from .locate import solidworks_typelib

        TYPE_LIB = TYPE_LIB or solidworks_typelib()
        comtypes.client.GetModule(TYPE_LIB)
        from comtypes.gen import SldWorks

        _LIB = SldWorks
    return _LIB


def _ptr(obj):
    """The raw COM pointer behind a comtypes dynamic dispatcher.

    comtypes 1.4 stores it as `_comobj`; older builds used `_comobj_`. Reading
    it with getattr() is not an option: on a dynamic dispatcher a missing
    attribute is forwarded to IDispatch::GetIDsOfNames, which raises COMError
    instead of AttributeError.
    """
    if obj is None:
        return None
    for attr in ("_comobj", "_comobj_"):
        try:
            value = object.__getattribute__(obj, attr)
        except AttributeError:
            continue
        if value is not None:
            return value
    return obj


def _is_dispatch(obj):
    """True for comtypes' dynamic dispatcher, whose members are auto-invoked."""
    return type(obj).__name__ in ("_Dispatch", "_DispatchBase")


def _addr(obj):
    """Identity for a COM object that survives allocator reuse.

    comtypes caches one ctypes pointer per (interface, address) pair, so the
    pointer object is stable for as long as the COM object lives; casting the
    pointer to c_void_p instead returns a recycled Python-level address and
    makes two different features look identical.
    """
    if obj is None:
        return None
    return id(_ptr(obj))



def _qi(obj, iface_name):
    """Query an object for another interface, or hand it back unchanged."""
    if obj is None:
        return None
    iface = getattr(lib(), iface_name, None)
    if iface is None:
        return obj
    try:
        return _ptr(obj).QueryInterface(iface)
    except Exception:  # noqa: BLE001
        return obj


def _get(obj, name, *args):
    """Read a member that may be a property, a method, or a COM object.

    comtypes exposes typelib properties as attributes and methods as callables,
    but objects returned as `IDispatch` arrive as dynamic dispatchers where even
    a method call may already be evaluated. This handles all three shapes.
    """
    if obj is None:
        return None
    try:
        value = getattr(obj, name)
    except AttributeError:
        value = getattr(_ptr(obj), name)
    if callable(value) and not _is_dispatch(value):
        try:
            return value(*args)
        except TypeError:
            return value
    return value


def _call(fn):
    """Run a call whose result is a SAFEARRAY comtypes cannot marshal.

    The COM call itself succeeds; comtypes then fails to build the Python value
    (`KeyError: 9`, VT_DISPATCH). For calls made purely for their side effect
    that is fine, so the exception is swallowed.
    """
    try:
        return fn()
    except KeyError:
        return None


def _mm(value):
    return float(value) / 1000.0


def _polygon_area(points):
    """Shoelace area in mm² for a closed polygon given as (x, y) pairs."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


class Session:
    """Core part modelling.  Optional tool families live in extra.ExtraTools."""

    """One SOLIDWORKS application instance plus the document being built."""

    def __init__(self, visible=True):
        self.visible = visible
        self._app = None
        self._rawdoc = None
        self._doc = None
        self._planes = None
        self._sketch_name = None

    # -- connection --------------------------------------------------------
    def connect(self):
        import comtypes.client

        if self._app is not None and self._doc is not None:
            return self.status()

        app = None
        try:
            app = comtypes.client.GetActiveObject("SldWorks.Application")
        except Exception:  # noqa: BLE001
            app = None

        if app is None:
            probe = self.license_probe()
            if probe["configured"] and not probe["reachable"]:
                raise NotConnected(
                    "SolidWorks cannot start: its licence server "
                    f"{probe['server']} does not answer from this machine.",
                    hint="Connect to the network or VPN that serves that licence "
                         "server, then try again.",
                    details={"license": probe},
                )
            try:
                app = comtypes.client.CreateObject("SldWorks.Application")
            except Exception as exc:  # noqa: BLE001
                raise NotConnected(
                    f"Could not start or attach to SOLIDWORKS: {exc}",
                    hint="Start SOLIDWORKS by hand, then retry.",
                ) from exc

        self._app = _qi(app, "ISldWorks")
        try:
            if self.visible:
                self._app.Visible = True
        except Exception:  # noqa: BLE001
            pass
        return self.status()

    def _require_doc(self):
        if self._doc is None:
            raise NoDocument("No document is open. Call sw_new_part or sw_open_part first.")
        return self._doc

    def _require_app(self):
        if self._app is None:
            self.connect()
        return self._app

    # -- environment -------------------------------------------------------
    @staticmethod
    def license_probe():
        """Where the licence comes from, and whether it answers right now."""
        endpoint = ""
        import winreg

        for root, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\FLEXlm License Manager"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\FLEXlm License Manager"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\FLEXlm License Manager"),
        ):
            try:
                with winreg.OpenKey(root, path) as key:
                    endpoint = winreg.QueryValueEx(key, "SW_D_LICENSE_FILE")[0]
                    break
            except OSError:
                continue
        result = {
            "raw": endpoint,
            "configured": bool(endpoint),
            "server": None,
            "port": None,
            "reachable": False,
            "kind": "unknown",
        }
        if not endpoint:
            return result
        result["kind"] = "network" if "@" in endpoint else "local"
        host_part = endpoint.split("@")[-1].strip()
        if ":" in host_part:
            host, port = host_part.rsplit(":", 1)
        else:
            host, port = host_part, "25734"
        result["server"] = host
        result["port"] = int(port) if port.isdigit() else port
        if result["kind"] == "network" and isinstance(result["port"], int):
            try:
                with socket.create_connection((host, result["port"]), timeout=4):
                    result["reachable"] = True
            except OSError:
                result["reachable"] = False
        return result

    @staticmethod
    def part_template():
        """A part template shipped with this installation."""
        override = os.environ.get("SWAGENT_PART_TEMPLATE")
        if override and os.path.exists(override):
            return override
        candidates = []
        for pattern in (
            r"C:\ProgramData\SOLIDWORKS\*\templates\*.prtdot",
            os.path.join(os.environ.get("APPDATA", ""), "SOLIDWORKS", "*", "templates", "*.prtdot"),
        ):
            candidates.extend(glob.glob(pattern))
        if not candidates:
            raise SwError(
                "No part template (.prtdot) found",
                code="no_template",
                hint="Set SWAGENT_PART_TEMPLATE to a .prtdot path.",
            )

        def rank(path):
            name = os.path.basename(path).lower()
            return (0 if "gb_part" in name else 1, 0 if "part" in name else 1, len(path))

        return sorted(candidates, key=rank)[0]

    # -- status ------------------------------------------------------------
    def status(self):
        app = self._require_app()
        revision = _get(app, "RevisionNumber")
        if callable(revision):
            revision = revision()
        info = {
            "connected": True,
            "revision": str(revision) if revision is not None else None,
            "document": None,
            "license": self.license_probe(),
        }
        if self._doc is not None:
            info["document"] = {
                "title": _get(self._doc, "GetTitle"),
                "path": _get(self._doc, "GetPathName") or None,
                "type": _get(self._doc, "GetType"),
            }
        return info

    # -- documents ---------------------------------------------------------
    def new_part(self, template=None):
        app = self._require_app()
        template = template or self.part_template()
        raw = app.NewDocument(template, 0, 0, 0)
        if raw is None:
            raise SwError("NewDocument returned nothing", code="new_document_failed")
        self._rawdoc = _qi(raw, "IPartDoc")
        self._doc = _qi(raw, "IModelDoc2")
        self._planes = None
        return {
            "document": {
                "title": _get(self._doc, "GetTitle"),
                "template": template,
            },
            "planes": self.planes(),
        }

    def open_part(self, path):
        app = self._require_app()
        if not os.path.exists(path):
            raise SwError(f"File not found: {path}", code="file_not_found")
        raw = None
        import ctypes as _ctypes
        errors = _ctypes.c_int()
        warnings = _ctypes.c_int()
        try:
            raw = app.OpenDoc6(path, DOC_PART, 0, "", _ctypes.byref(errors),
                               _ctypes.byref(warnings))
        except TypeError:
            try:
                raw = app.OpenDoc6(path, DOC_PART, 0, "", 0, 0)
            except Exception as exc:  # noqa: BLE001
                raise SwError(f"Could not open {path}: {exc}", code="open_failed") from exc
        except Exception as exc:  # noqa: BLE001
            raise SwError(f"Could not open {path}: {exc}", code="open_failed") from exc
        if isinstance(raw, (list, tuple)):
            raw = next((item for item in raw if item is not None and not isinstance(item, int)),
                       None)
        if raw is None:
            try:
                raw = app.ActivateDoc3(path, False, 0, _ctypes.byref(errors))
            except Exception as exc:  # noqa: BLE001
                raise SwError(f"Could not activate {path}: {exc}", code="open_failed") from exc
        if raw is None:
            raise SwError(f"SolidWorks refused to open {path}", code="open_failed",
                          details={"errors": errors.value, "warnings": warnings.value})
        self._rawdoc = _qi(raw, "IPartDoc")
        self._doc = _qi(raw, "IModelDoc2")
        self._planes = None
        return {"document": {"title": _get(self._doc, "GetTitle"), "path": path},
                "planes": self.planes()}

    def close(self, save=False):
        app = self._require_app()
        title = _get(self._doc, "GetTitle") or ""
        path = _get(self._doc, "GetPathName") or None
        if save and path:
            self.save(path)
        app.CloseDoc(title)
        self._doc = None
        self._rawdoc = None
        self._planes = None
        return {"closed": title, "saved": bool(save and path)}

    def close_all(self, include_unsaved=True):
        """Close every open document, so a build never leaves stray parts."""
        app = self._require_app()
        _call(lambda: app.CloseAllDocuments(bool(include_unsaved)))
        self._doc = None
        self._rawdoc = None
        self._planes = None
        return {"closed_all": True}

    def _deselect(self):
        doc = self._require_doc()
        return _call(lambda: doc.ClearSelection2(True))

    # -- feature tree ------------------------------------------------------
    def _features(self):
        """Walk the feature tree; comtypes returns features as dispatchers."""
        doc = self._require_doc()
        feat = _get(doc, "FirstFeature")
        if feat is None:
            return
        alive = []  # keep wrappers alive so their ids cannot be recycled
        seen = set()
        while feat is not None and len(alive) < 500:
            key = _addr(feat)
            if key in seen:
                return
            seen.add(key)
            alive.append(feat)
            yield feat
            feat = _get(feat, "GetNextFeature")

    def planes(self):
        """Map the three default planes to the names this install uses."""
        if self._planes is not None:
            return self._planes
        found = []
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) == "RefPlane":
                found.append(str(_get(feat, "Name")))
            if len(found) >= 3:
                break
        mapping = {}
        for alias, name in zip(("front", "top", "right"), found):
            mapping[alias] = name
        if not mapping:
            raise SwError(
                "No reference planes found in this document",
                code="plane_missing",
                hint="Open a part document that has the default planes.",
            )
        self._planes = mapping
        return mapping

    def _plane_name(self, alias):
        planes = self.planes()
        name = planes.get(alias)
        if name is not None:
            return name
        # a reference plane created at run time is addressed by its own name
        if str(alias) and str(alias) not in ("front", "top", "right"):
            for feat in self._features():
                if str(_get(feat, "GetTypeName2")) in ("RefPlane", "Plane") and \
                        str(_get(feat, "Name")) == str(alias):
                    return str(alias)
        raise SwError(
            f"Plane {alias!r} is not available; known planes: {sorted(planes)}",
            code="plane_missing",
        )

    def list_features(self):
        self._require_doc()
        items = []
        for index, feat in enumerate(self._features()):
            items.append({
                "index": index,
                "name": str(_get(feat, "Name")),
                "type": str(_get(feat, "GetTypeName2")),
            })
        return {"count": len(items), "features": items}

    # -- geometry access ---------------------------------------------------
    def _ext(self):
        return _qi(_get(self._require_doc(), "Extension"), "IModelDocExtension")

    def _sm(self):
        return _qi(_get(self._require_doc(), "SketchManager"), "ISketchManager")

    def _fm(self):
        return _qi(_get(self._require_doc(), "FeatureManager"), "IFeatureManager")

    def _mass_prop(self):
        return _get(self._ext(), "CreateMassProperty2")

    def _volume(self):
        value = _get(self._mass_prop(), "Volume")
        return float(value) if value else 0.0

    def _body(self):
        """The solid body, reached through a feature or, failing that, a face.

        `IFeature.GetBody` answers NULL over early binding on this build, so the
        fallback picks the face nearest the top of the bounding box and asks
        that face for its body - which does work.
        """
        self._require_doc()
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) not in (
                    "Extrusion", "Cut", "Fillet", "CirPattern", "Revolution", "ICE", "Mirror"):
                continue
            member = getattr(feat, "GetBody", None)
            if not callable(member):
                continue
            try:
                body = member()
            except Exception:  # noqa: BLE001
                body = None
            if body is not None:
                typed = _qi(body, "IBody2")
                if typed is not None:
                    return typed
        return self._body_via_face()

    def _body_via_face(self):
        box = self.bbox()
        if not box:
            raise SwError("No solid body in this document", code="no_body")
        cx = (box[0] + box[3]) / 2.0
        cy = (box[1] + box[4]) / 2.0
        for z in (box[5], box[2]):
            self._deselect()
            ok = self._ext().SelectByID2("", "FACE", cx, cy, z, False, 0, None, 0)
            if not ok:
                continue
            manager = _get(self._doc, "SelectionManager")
            face = manager.GetSelectedObject6(1, -1) if manager is not None else None
            typed = _qi(face, "IFace2") if face is not None else None
            if typed is None:
                continue
            body = _qi(typed.GetBody(), "IBody2")
            if body is not None:
                return body
        raise SwError("No solid body in this document", code="no_body")

    def _faces(self):
        body = self._body()
        face = _call(lambda: body.GetFirstFace())
        alive = []
        seen = set()
        while face is not None and len(alive) < 5000:
            key = _addr(face)
            if key in seen:
                return
            seen.add(key)
            alive.append(face)
            yield _qi(face, "IFace2")
            face = _get(face, "GetNextFace")

    def _edges(self):
        """Every edge once, via loops and coedges (arrays are unmarshalable)."""
        alive = []
        seen = set()
        for face in self._faces():
            loop = _get(face, "GetFirstLoop")
            while loop is not None and len(alive) < 20000:
                alive.append(loop)
                coedge = _get(loop, "GetFirstCoEdge")
                start = _addr(coedge)
                while coedge is not None and len(alive) < 40000:
                    alive.append(coedge)
                    edge = _get(coedge, "GetEdge")
                    key = _addr(edge)
                    if key is not None and key not in seen:
                        alive.append(edge)
                        seen.add(key)
                        yield _qi(edge, "IEdge")
                    coedge = _get(coedge, "GetNext")
                    if coedge is None or _addr(coedge) == start:
                        break
                loop = _get(loop, "GetNext")
        return

    def bbox(self):
        self._require_doc()
        raw = _call(lambda: self._rawdoc.GetPartBox(True))
        if not raw:
            return None
        return [round(float(v), 6) for v in raw]

    # -- sketching ---------------------------------------------------------
    def _select_plane(self, alias):
        name = self._plane_name(alias)
        ok = self._ext().SelectByID2(name, "PLANE", 0, 0, 0, False, 0, None, 0)
        if not ok:
            raise SwError(f"Could not select plane {name!r}", code="plane_select_failed")
        return name

    def _sketch_begin(self):
        self._sketch_name = None
        self._sm().InsertSketch(True)

    def _sketch_end(self):
        self._sm().InsertSketch(True)
        self._sketch_name = self._last_sketch_name()

    def _last_sketch_name(self):
        """Name of the most recent ProfileFeature, which is the sketch just closed."""
        name = None
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) == "ProfileFeature":
                name = str(_get(feat, "Name"))
        return name

    def _line(self, x1, y1, x2, y2):
        _call(lambda: self._sm().CreateLine(x1, y1, 0.0, x2, y2, 0.0))

    def _extrude(self, depth_mm, flip=False):
        """Extrude the last sketch and confirm it actually added volume."""
        depth = _mm(depth_mm)
        name = self._sketch_name or self._last_sketch_name()
        self._sketch_name = name
        if name:
            # Walking the feature tree leaves nothing selected, and
            # FeatureExtrusion2 silently does nothing without a selected
            # profile -- so select the sketch explicitly every time.
            self._doc.ClearSelection2(True)
            self._ext().SelectByID2(name, "SKETCH", 0, 0, 0, False, 0, None, 0)
        before = self._volume()
        feat = _call(lambda: self._fm().FeatureExtrusion2(
            True, flip, False, END_COND_BLIND, END_COND_BLIND, depth, depth,
            False, False, False, False, 0, 0, False, False, False, False,
            True, True, True, 0, 0, False))
        after = self._volume()
        return feat, after - before

    def _extrude_feature(self, depth_mm):
        if abs(depth_mm) < 1e-9:
            raise SwError("Extrusion depth must not be zero", code="bad_argument")
        for flip in (False, True):
            feat, delta = self._extrude(depth_mm, flip=flip)
            if delta > 1e-9:
                return feat, delta
        raise SwError(
            "Extrusion produced no material",
            code="extrude_failed",
            hint="The sketch may be open or not on a planar face.",
        )

    # -- modelling ---------------------------------------------------------
    def add_box(self, width, height, depth, plane="front", flip=False):
        """A rectangular boss centred on the sketch plane."""
        self._select_plane(plane)
        self._sketch_begin()
        _call(lambda: self._sm().CreateCornerRectangle(
            -_mm(width) / 2, -_mm(height) / 2, 0, _mm(width) / 2, _mm(height) / 2, 0))
        self._sketch_end()
        feat, delta = self._extrude_feature(depth)
        return {
            "feature": str(_get(feat, "Name")),
            "volume_mm3": round(self._volume() * 1e9, 3),
            "added_mm3": round(delta * 1e9, 3),
            "bbox_m": self.bbox(),
        }

    def add_cylinder(self, diameter, height, plane="front", flip=False):
        """A cylindrical boss centred on the sketch plane."""
        self._select_plane(plane)
        self._sketch_begin()
        _call(lambda: self._sm().CreateCircleByRadius(0, 0, 0, _mm(diameter) / 2))
        self._sketch_end()
        feat, delta = self._extrude_feature(height)
        return {
            "feature": str(_get(feat, "Name")),
            "volume_mm3": round(self._volume() * 1e9, 3),
            "added_mm3": round(delta * 1e9, 3),
            "bbox_m": self.bbox(),
        }

    def add_profile(self, points, depth, plane="front", flip=False):
        """A closed polyline boss from (x, y) points in mm."""
        if len(points) < 3:
            raise SwError("A profile needs at least three points", code="bad_argument")
        self._select_plane(plane)
        self._sketch_begin()
        ring = list(points) + [points[0]]
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            self._line(_mm(x1), _mm(y1), _mm(x2), _mm(y2))
        self._sketch_end()
        feat, delta = self._extrude_feature(depth)
        return {
            "feature": str(_get(feat, "Name")),
            "points": len(points),
            "volume_mm3": round(self._volume() * 1e9, 3),
            "added_mm3": round(delta * 1e9, 3),
        }

    def _cut_live(self, before, depth_mm=None, select_segment=True, end=None):
        """Cut with the sketch that is currently open in the sketch editor.

        This mirrors the shipped VBA example (`Insert_Cut_Extrude_Example_VB`):
        while the sketch is still in edit mode, clear the selection and call
        `FeatureCut3` - the active sketch is the profile. Exiting the sketch
        first (what this code used to do) works for through-all cuts but makes
        blind cuts produce no feature at all on this build.
        """
        if end == "through_next":
            # swEndCondThroughNext = 2: stop after passing the next wall, which is
            # what a bolt hole through a single flange needs
            variants = [(2, END_COND_BLIND, 0.0, 0.0), (11, END_COND_BLIND, 0.0, 0.0)]
        elif end == "through_all_one_way":
            variants = [(END_COND_THROUGH_ALL, END_COND_BLIND, 0.0, 0.0)]
        elif depth_mm is None:
            variants = [(END_COND_THROUGH_ALL, END_COND_THROUGH_ALL, 0.0, 0.0)]
        else:
            depth = _mm(depth_mm)
            variants = [
                (END_COND_BLIND, END_COND_BLIND, depth, 0.0),
                (END_COND_BLIND, END_COND_BLIND, depth, depth),
                (END_COND_BLIND, END_COND_THROUGH_ALL, depth, 0.0),
            ]
        tried = []
        for t1, t2, d1, d2 in variants:
            for flip in (False, True):
                _call(lambda: self._doc.ClearSelection2(True))
                if select_segment:
                    _call(lambda: self._ext().SelectByID2(
                        "Arc1", "SKETCHSEGMENT", 0, 0, 0, False, 0, None, 0))
                    _call(lambda: self._doc.ClearSelection2(True))
                feat = _call(lambda f=flip, a=t1, b=t2, c=d1, e=d2: self._fm().FeatureCut3(
                    True, f, False, a, b, c, e, False, False, False, False,
                    math.radians(1.0), math.radians(1.0), False, False, False, False,
                    False, True, True, False, False, False, 0, 0.0, False))
                after = self._volume()
                tried.append(f"T1={t1},T2={t2},D1={d1:.4f},flip={flip}->{after:.1f}")
                if feat is not None and after < before - 1e-9:
                    return feat, before - after
        raise SwError(
            "Cut removed no material",
            code="cut_failed",
            hint="The profile may be open, or it does not overlap the body.",
            details={"tried": tried, "before": round(before * 1e9, 3)},
        )

    def _cut(self, depth_mm=None):
        """Cut with the sketch named in `self._sketch_name` (fallback path).

        The sketch must already be closed for this one; it is the path that has
        always worked for through-all cuts and is kept as a fallback for the
        live-sketch route.
        """
        before = self._volume()
        doc = self._require_doc()
        ext = self._ext()
        name = getattr(self, "_sketch_name", None)
        selected = False
        if name:
            _get(doc, "ClearSelection2", True)
            selected = bool(ext.SelectByID2(name, "SKETCH", 0, 0, 0, False, 0, None, 0))
        if not selected:
            for candidate in self._sketch_candidates():
                _get(doc, "ClearSelection2", True)
                if ext.SelectByID2(candidate, "SKETCH", 0, 0, 0, False, 0, None, 0):
                    selected = True
                    name = candidate
                    break
        if depth_mm is None:
            t1 = t2 = END_COND_THROUGH_ALL_BOTH
            d1 = d2 = 0.0
        else:
            t1 = t2 = END_COND_BLIND
            d1 = d2 = _mm(depth_mm)
        for flip in (False, True):
            feat = _call(lambda f=flip: self._fm().FeatureCut3(
                True, f, False, t1, t2, d1, d2, False, False, False, False, 0, 0,
                False, False, False, False, False, True, False, False, False, False,
                0, 0.0, False))
            after = self._volume()
            if after < before - 1e-12:
                return feat, before - after
        raise SwError(
            "Cut removed no material",
            code="cut_failed",
            hint="The sketch may not overlap the body, or it is not closed.",
            details={"sketch": name, "selected": selected},
        )

    def _face_sketch_offset(self, point):
        """How far the face's sketch origin sits from the part origin.

        A sketch placed on a face is positioned relative to that face's own
        origin, which on a revolved body is not the part origin - drawing a
        circle at (x, y) therefore lands somewhere else. The reference-curve
        trick gives the mapping: a sketch point drawn at (0, 0) is turned into a
        curve, and that curve's own defining point, read in model coordinates,
        *is* the sketch origin expressed in model space.
        """
        sketch_manager = self._sm()
        _call(lambda: sketch_manager.CreatePoint(_mm(0.0), _mm(0.0), 0.0))
        curve = _call(lambda: sketch_manager.CreateReferenceCurve(False, True, 0))
        if curve is None:
            return (0.0, 0.0, 0.0)
        try:
            _call(lambda: self._doc.ClearSelection2(True))
            curve.Select2(True, 0)
            data = _call(lambda: self._ext().GetReferenceCurveTypes())
            if data:
                offset = tuple(float(v) for v in data[:3])
                return offset
        except Exception:  # noqa: BLE001
            pass
        return (0.0, 0.0, 0.0)

    def _select_top_face(self, axis="z", high=True):
        """Select the outermost upward-facing planar face along `axis`.

        `axis` is "x", "y" or "z": the face whose normal points along +axis and
        which sits furthest out on that axis. A box uses "z" (its top), the
        upright shaft uses "y" (the flange face).
        """
        index = {"x": 0, "y": 1, "z": 2}[axis]
        box = self.mass_properties()["bbox_m"]
        out = box[index + 3] if high else box[index]
        centre = [(box[0] + box[3]) / 2.0, (box[1] + box[4]) / 2.0, (box[2] + box[5]) / 2.0]
        spot = list(centre)
        spot[index] = out
        spread = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}[axis]
        for nudge in (0.0, 0.008, 0.02, 0.04, -0.008):
            point = list(spot)
            point[spread[0]] += nudge
            if self._pick_point("FACE", *point):
                return tuple(point)
        raise SwError(f"Could not select the {'+' if high else '-'}{axis} face",
                      code="face_select_failed",
                      details={"bbox_m": box})

    def _sketch_candidates(self):
        """Likely names for the sketch a cut should consume."""
        names = []
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) == "ProfileFeature":
                names.append(str(_get(feat, "Name")))
        return list(reversed(names))

    def cut_hole(self, diameter, x=0.0, y=0.0, depth=None, plane="front",
                 from_face=None, axis="z", end=None, high=True):
        """A round hole at (x, y); omit `depth` for a through hole.

        `from_face=True` sketches on the topmost upward face instead of a datum
        plane, which is what a counterbore needs.
        """
        before = self._volume()
        if from_face is None:
            # the datum-plane form stopped producing features on this build, so
            # the face form is the default whenever there is material to cut
            from_face = before > 0.0
        if from_face:
            self._select_top_face(axis, high)
        else:
            self._select_plane(plane)
        _call(lambda: self._sm().InsertSketch(True))
        _call(lambda: self._doc.ClearSelection2(True))
        cx, cy = (x, y)
        _call(lambda: self._sm().CreateCircleByRadius(_mm(cx), _mm(cy), 0,
                                                     _mm(diameter) / 2))
        if from_face:
            # a face sketch keeps working when the cut is blind, and the cut has
            # to happen while the sketch is still open
            feat, removed = self._cut_live(before, depth, end=end)
        else:
            # a datum-plane sketch: closing it first is the only form that
            # produced features here, and it is what the through cuts use
            self._sketch_end()
            feat, removed = self._cut(depth)
        return {
            "feature": str(_get(feat, "Name")),
            "removed_mm3": round(removed * 1e9, 3),
            "volume_mm3": round(self._volume() * 1e9, 3),
        }

    def cut_pocket(self, points, depth=None):
        """Cut a polygonal pocket; omit `depth` for through-all."""
        if len(points) < 3:
            raise SwError("A pocket needs at least three points", code="bad_argument")
        before = self._volume()
        self._select_plane("front")
        _call(lambda: self._sm().InsertSketch(True))
        _call(lambda: self._doc.ClearSelection2(True))
        ring = list(points) + [points[0]]
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            self._line(_mm(x1), _mm(y1), _mm(x2), _mm(y2))
        if depth is not None:
            # a blind pocket only works from an open sketch on a face; from a
            # datum plane the closed-sketch form is what SolidWorks accepts
            try:
                feat, removed = self._cut_live(before, depth, select_segment=False)
            except SwError:
                self._sketch_end()
                feat, removed = self._cut(depth)
        else:
            self._sketch_end()
            feat, removed = self._cut(depth)
        return {
            "feature": str(_get(feat, "Name")),
            "removed_mm3": round(removed * 1e9, 3),
            "volume_mm3": round(self._volume() * 1e9, 3),
        }

    def fillet(self, radius, edges="all"):
        """Round every edge (or the ones named in `edges`) with one radius."""
        radius_m = _mm(radius)
        doc = self._require_doc()
        _get(doc, "ClearSelection2", True)
        selected = 0
        wanted = None if edges in (None, "all") else set(edges)
        for edge in self._edges():
            if wanted is not None and _addr(edge) not in wanted:
                continue
            if _call(lambda e=edge: e.Select4(True, None)):
                selected += 1
        if not selected:
            raise SwError("No edges available to fillet", code="no_edges")
        before = self._volume()
        feat = _call(lambda: self._fm().FeatureFillet3(
            FILLET_PROPAGATE | FILLET_UNIFORM_RADIUS, radius_m, radius_m, 0,
            FILLET_TYPE_SIMPLE, 0, 0, None, None, None, None, None, None, None))
        after = self._volume()
        if after >= before - 1e-12:
            raise SwError(
                "Fillet changed nothing",
                code="fillet_failed",
                hint="Try a smaller radius, or a part with sharper edges.",
            )
        return {
            "feature": str(_get(feat, "Name")) if feat is not None else None,
            "edges": selected,
            "radius_mm": radius,
            "volume_mm3": round(after * 1e9, 3),
            "removed_mm3": round((before - after) * 1e9, 3),
        }

    def gear(self, teeth=24, outer_diameter=100.0, thickness=10.0, bore=0.0,
             pressure_angle=20.0):
        """A spur gear with involute flanks, built the way the tutorials do it.

        草图1 (two circles: root circle + bore) -> 凸台-拉伸1, a 基准轴 taken from
        the disc's cylindrical face, 草图2 (one tooth) -> 凸台-拉伸2, then
        阵列(圆周)1 repeats that tooth ``teeth`` times about the axis.

        Returns the radii, the analytic volume and the measured volume so the
        caller can check the model instead of trusting it.
        """
        teeth = int(teeth)
        if teeth < 6:
            raise SwError("A gear needs at least 6 teeth", code="bad_argument")
        module = outer_diameter / (teeth + 2)          # mm
        r_out = outer_diameter / 2.0
        r_pitch = module * teeth / 2.0
        r_root = r_pitch - 1.25 * module
        r_bore = bore / 2.0 if bore else 0.0
        alpha_p = math.radians(pressure_angle)
        r_base = r_pitch * math.cos(alpha_p)
        half_tooth = math.pi / (2.0 * teeth)
        if r_root <= r_bore:
            raise SwError(
                f"{teeth} teeth at {outer_diameter} mm leave no material between "
                f"the root circle and the bore",
                code="bad_argument",
            )

        def involute(alpha):
            return math.tan(alpha) - alpha

        inv_p = involute(alpha_p)

        def half_angle(r):
            if r <= r_base:
                return half_tooth + inv_p
            return half_tooth + inv_p - involute(math.acos(min(1.0, r_base / r)))

        samples = 14
        polar = [(r_root, -half_angle(r_root)), (r_base, -half_angle(r_base))]
        for i in range(1, samples + 1):
            polar.append((r_base + (r_out - r_base) * i / samples,
                          -half_angle(r_base + (r_out - r_base) * i / samples)))
        for i in range(samples, 0, -1):
            polar.append((r_base + (r_out - r_base) * i / samples,
                          half_angle(r_base + (r_out - r_base) * i / samples)))
        polar.append((r_base, half_angle(r_base)))
        polar.append((r_root, half_angle(r_root)))
        tooth_xy = [(r * math.cos(a), r * math.sin(a)) for r, a in polar]
        tooth_area = _polygon_area(tooth_xy)
        expected = (math.pi * (r_root ** 2 - r_bore ** 2) + teeth * tooth_area) * thickness

        # 1. the disc, drawn as two circles so the bore needs no cut feature
        self._select_plane("front")
        self._sketch_begin()
        _call(lambda: self._sm().CreateCircleByRadius(0.0, 0.0, 0.0, _mm(r_root)))
        if r_bore:
            _call(lambda: self._sm().CreateCircleByRadius(0.0, 0.0, 0.0, _mm(r_bore)))
        self._sketch_end()
        disc_feat, _ = self._extrude_feature(thickness)

        # 2. a reference axis through the disc
        self._deselect()
        axis = None
        ok_face = _call(lambda: self._ext().SelectByID2(
            "", "FACE", _mm(r_root), 0.0, _mm(thickness / 2.0), False, 0, None, 0))
        if ok_face:
            axis = _call(lambda: self._doc.InsertAxis2(True))
        axis_name = None
        for feat in self._features():
            if str(_get(feat, "GetTypeName2") or "") == "RefAxis":
                axis_name = str(_get(feat, "Name") or "")

        # 3. one involute tooth
        self._deselect()
        self._select_plane("front")
        self._sketch_begin()
        for (x1, y1), (x2, y2) in zip(tooth_xy, tooth_xy[1:]):
            self._line(_mm(x1), _mm(y1), _mm(x2), _mm(y2))
        self._line(_mm(tooth_xy[-1][0]), _mm(tooth_xy[-1][1]),
                   _mm(tooth_xy[0][0]), _mm(tooth_xy[0][1]))
        self._sketch_end()
        tooth_feat, _ = self._extrude_feature(thickness)
        tooth_name = str(_get(tooth_feat, "Name") or "")

        # 4. circular pattern
        pattern = None
        if axis_name and tooth_name:
            self._deselect()
            ok_axis = _call(lambda: self._ext().SelectByID2(
                axis_name, "AXIS", 0.0, 0.0, 0.0, False, 1, None, 0))
            ok_tooth = _call(lambda: self._ext().SelectByID2(
                tooth_name, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0))
            if ok_axis and ok_tooth:
                pattern = _call(lambda: self._fm().FeatureCircularPattern4(
                    teeth, 2.0 * math.pi, False, "", True, True, False))
        # the axis is only scaffolding: hide it so screenshots show the part
        self._deselect()
        if axis_name and _call(lambda: self._ext().SelectByID2(
                axis_name, "AXIS", 0.0, 0.0, 0.0, False, 0, None, 0)):
            _call(lambda: self._doc.BlankRefGeom())
        self._deselect()

        volume = self._volume() * 1e9
        result = {
            "teeth": teeth,
            "module_mm": round(module, 4),
            "pressure_angle_deg": pressure_angle,
            "pitch_diameter_mm": round(2 * r_pitch, 3),
            "outer_diameter_mm": round(2 * r_out, 3),
            "root_diameter_mm": round(2 * r_root, 3),
            "base_diameter_mm": round(2 * r_base, 3),
            "thickness_mm": thickness,
            "bore_mm": bore,
            "profile_area_mm2": round(tooth_area, 3),
            "expected_volume_mm3": round(expected, 3),
            "volume_mm3": round(volume, 3),
            "deviation_percent": round(100.0 * (volume - expected) / expected, 3) if expected else None,
            "features": {
                "disc": str(_get(disc_feat, "Name") or ""),
                "axis": axis_name,
                "tooth": tooth_name,
                "pattern": str(_get(pattern, "Name") or "") if pattern is not None else None,
            },
            "bbox_m": self.bbox(),
        }
        return result

    # -- measurement -------------------------------------------------------
    def mass_properties(self):
        self._require_doc()
        mp = self._mass_prop()
        if mp is None:
            raise SwError("Could not create mass properties", code="no_mass_prop")
        volume = float(_get(mp, "Volume") or 0.0)
        mass = float(_get(mp, "Mass") or 0.0)
        area = float(_get(mp, "SurfaceArea") or 0.0)
        density = mass / volume if volume else None
        return {
            "volume_mm3": round(volume * 1e9, 3),
            "mass_g": round(mass * 1000, 3),
            "surface_area_mm2": round(area * 1e6, 3),
            "density_g_cm3": round(density / 1000, 4) if density else None,
            "bbox_m": self.bbox(),
        }

    # -- dimensions --------------------------------------------------------
    def set_dimension(self, name, value_mm):
        doc = self._require_doc()
        dim = None
        for feat in self._features():
            for kind in ("GetFirstDisplayDimension", "GetNextDisplayDimension"):
                pass
            dim = _get(feat, "Parameter", name)
            if dim is not None:
                break
        if dim is None:
            dim = _call(lambda: doc.Parameter(name))
        if dim is None:
            raise SwError(f"No dimension named {name!r}", code="no_dimension")
        try:
            dim.SystemValue = _mm(value_mm)
        except Exception as exc:  # noqa: BLE001
            raise SwError(f"Could not set {name!r}: {exc}", code="dimension_failed") from exc
        _call(lambda: doc.EditRebuild3())
        return {
            "dimension": name,
            "value_mm": value_mm,
            "volume_mm3": round(self._volume() * 1e9, 3),
        }

    def rebuild(self):
        doc = self._require_doc()
        ok = _call(lambda: doc.EditRebuild3())
        return {"rebuilt": True, "ok": ok, "volume_mm3": round(self._volume() * 1e9, 3)}

    # -- output ------------------------------------------------------------
    def save(self, path):
        doc = self._require_doc()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ok = _call(lambda: doc.SaveAs3(path, SAVE_AS_CURRENT_VERSION, SAVE_AS_SILENT))
        if not self._written(path):
            ok = _call(lambda: doc.SaveAs2(path, SAVE_AS_CURRENT_VERSION, False, True))
        if not self._written(path):
            ok = _call(lambda: doc.Save())
        if not self._written(path):
            raise SwError(
                f"Save did not produce {path}",
                code="save_failed",
                hint="SOLIDWORKS writes nothing while a modal dialog is open, for "
                     "example the Visual Basic warning it shows at startup.",
                details={"ok": ok,
                         "bytes": os.path.getsize(path) if os.path.exists(path) else 0},
            )
        return {"path": path, "bytes": os.path.getsize(path)}

    @staticmethod
    def _written(path):
        """A save counts only when the file has content.

        SOLIDWORKS creates the file, returns success, and leaves it at 0 bytes
        when a modal dialog is holding the UI thread.
        """
        return os.path.exists(path) and os.path.getsize(path) > 0

    def export(self, path):
        """Save a copy in another format (STEP, STL, IGES, Parasolid)."""
        self._require_doc()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ext = os.path.splitext(path)[1].lower()
        if ext not in EXPORT_FORMATS:
            raise SwError(
                f"Unsupported export format {ext!r}",
                code="bad_format",
                hint=f"Supported: {sorted(EXPORT_FORMATS)}",
            )
        ok = _call(lambda: self._ext().SaveAs(
            path, SAVE_AS_CURRENT_VERSION, SAVE_AS_SILENT, None, 0, 0))
        if not self._written(path):
            ok = _call(lambda: self._doc.SaveAs2(path, SAVE_AS_CURRENT_VERSION, False, True))
        if not self._written(path):
            raise SwError(f"Export did not produce {path}", code="export_failed",
                          hint="The translator for this format may not be installed, "
                               "or a modal dialog is blocking the document.",
                          details={"ok": ok, "format": EXPORT_FORMATS[ext]})
        return {"path": path, "format": EXPORT_FORMATS[ext], "bytes": os.path.getsize(path)}

    def screenshot(self, path, width=1600, height=1200):
        doc = self._require_doc()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        bmp = path + ".bmp" if not path.lower().endswith(".bmp") else path
        _call(lambda: doc.ViewZoomtofit2())
        ok = _call(lambda: doc.SaveBMP(bmp, width, height))
        if not os.path.exists(bmp):
            raise SwError("SolidWorks did not write a screenshot", code="screenshot_failed",
                          details={"ok": ok})
        if path.lower().endswith(".png"):
            bmp_to_png(bmp, path)
            try:
                os.remove(bmp)
            except OSError:
                pass
        return {"path": path, "width": width, "height": height}

    def screenshot_cropped(self, path, width=1800, height=1200, pad=0.05):
        """Screenshot cropped to the part, for a picture of the part alone."""
        doc = self._require_doc()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        bmp = path + ".raw.bmp"
        _call(lambda: doc.ViewZoomtofit2())
        ok = _call(lambda: doc.SaveBMP(bmp, width, height))
        if not os.path.exists(bmp):
            raise SwError("SolidWorks did not write a screenshot", code="screenshot_failed",
                          details={"ok": ok})
        crop_bmp(bmp, path, pad=pad)
        try:
            os.remove(bmp)
        except OSError:
            pass
        return {"path": path, "width": width, "height": height, "cropped": True}


# The optional tool families (patterns, shell, assembly, drawing, configurations)
# are a mixin so this file stays about core part modelling.
from .extra import ExtraTools  # noqa: E402  (circular import: extra imports helpers above)


class SessionWithExtras(Session, ExtraTools):
    pass
