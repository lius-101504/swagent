"""Tool families beyond basic solids: patterns, shell, chamfer, revolve,
reference geometry, assemblies, drawings and configurations.

Everything here is a mixin for `Session`: it uses the same COM plumbing
(`self._doc`, `self._ext()`, `self._fm()`, `self._sm()`) and the same rule as
the rest of the package - do the call, then measure, and only report success
when the measurement agrees.

The argument lists below come from `sldworks.tlb`, not from documentation: the
out parameters are passed explicitly as ctypes pointers, because comtypes only
returns them automatically when they are declared `[out, retval]`.
"""
import ctypes
import glob
import math
import os

from comtypes import POINTER

from .errors import SwError
from .session import _addr, _call, _get, _mm, _qi, lib

MATE_COINCIDENT = 0
MATE_CONCENTRIC = 1
MATE_PARALLEL = 2
MATE_DISTANCE = 5
MATE_ANGLE = 6
MATE_ALIGN_CLOSEST = 2

CHAMFER_ANGLE_DISTANCE = 1
CHAMFER_EQUAL_DISTANCE = 16

REVOLVE_ONE_DIRECTION = 0
REVOLVE_ONE_DIRECTION_360 = 3

VIEW_CODES = {
    "*Front": 1, "*Back": 2, "*Left": 3, "*Right": 4,
    "*Top": 5, "*Bottom": 6, "*Isometric": 7, "*Trimetric": 8,
    "*Dimetric": 9, "*Normal To": 0,
}

DOC_PART = 1
DOC_ASSEMBLY = 2
DOC_DRAWING = 3


def _template(kind):
    """A template shipped with this installation: part, assembly or drawing."""
    suffix, env = {
        "part": (".prtdot", "SWAGENT_PART_TEMPLATE"),
        "assembly": (".asmdot", "SWAGENT_ASSEMBLY_TEMPLATE"),
        "drawing": (".drwdot", "SWAGENT_DRAWING_TEMPLATE"),
    }[kind]
    override = os.environ.get(env)
    if override and os.path.exists(override):
        return override
    candidates = []
    for pattern in (
        r"C:\ProgramData\SOLIDWORKS\*\templates\*" + suffix,
        os.path.join(os.environ.get("APPDATA", ""), "SOLIDWORKS", "*", "templates", "*" + suffix),
    ):
        candidates.extend(glob.glob(pattern))
    if not candidates:
        raise SwError(f"No {kind} template (*{suffix}) found", code="no_template",
                      hint=f"Set {env} to a {suffix} path.")
    return sorted(candidates, key=lambda path: (len(path), path))[0]


class ExtraTools:
    # -- small helpers -----------------------------------------------------
    def _choose(self, name, kind, x=0.0, y=0.0, z=0.0, mark=0, append=False):
        return _call(lambda: self._ext().SelectByID2(
            name, kind, _mm(x), _mm(y), _mm(z), append, mark, None, 0))

    def _pick_point(self, kind, x_m, y_m, z_m, mark=0, append=False):
        """Select the entity nearest a point given in METRES (GetBox units)."""
        return _call(lambda: self._ext().SelectByID2(
            "", kind, float(x_m), float(y_m), float(z_m), append, mark, None, 0))

    def _out(self, iface_name):
        """A pointer for one [out] parameter of the given interface type."""
        iface = getattr(lib(), iface_name, None)
        return POINTER(iface)() if iface is not None else None

    @staticmethod
    def _invoke(fn, *args):
        """Call a COM method, tolerating comtypes' retval bookkeeping.

        comtypes drops the trailing [out, retval] pointer from the argument
        list and returns it instead - but only sometimes.  Rather than encode
        that per method, try the full list, then retry without the last item
        when comtypes says the count is wrong.
        """
        try:
            return _call(lambda: fn(*args))
        except TypeError as exc:
            if "arguments" in str(exc) and len(args) > 1:
                return _call(lambda: fn(*args[:-1]))
            raise

    def _last_feature_named(self, type_name):
        name = None
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) == type_name:
                name = str(_get(feat, "Name"))
        return name

    def _name_of(self, obj):
        """Best-effort name of a feature/object; never raises."""
        if obj is None:
            return None
        try:
            value = _get(obj, "Name")
        except Exception:  # noqa: BLE001
            return None
        return str(value) if value is not None else None

    def _feature_by_name(self, name):
        for feat in self._features():
            if str(_get(feat, "Name")) == name:
                return feat
        raise SwError(f"No feature named {name!r}", code="no_feature",
                      hint=str([str(_get(f, "Name")) for f in self._features()][-12:]))

    def _last_boss_name(self):
        name = None
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) in ("Extrusion", "ICE", "Revolution", "CirPattern"):
                name = str(_get(feat, "Name"))
        return name

    def _pick_edges(self, points):
        self._deselect()
        picked = 0
        for x, y, z in points:
            if self._choose("", "EDGE", x, y, z, append=True):
                picked += 1
        return picked

    def _select_all_edges(self):
        self._deselect()
        picked = 0
        for edge in self._edges():
            entity = _qi(edge, "IEntity")
            if entity is not None and _call(lambda e=entity: e.Select4(True, None)):
                picked += 1
        return picked

    def _reference_axis(self):
        name = None
        for feat in self._features():
            if str(_get(feat, "GetTypeName2")) == "RefAxis":
                name = str(_get(feat, "Name"))
        return name

    # -- detail features ---------------------------------------------------
    def chamfer(self, distance, edges="all", angle_deg=45.0):
        """Break the edges with an equal-distance chamfer.

        `IModelDoc2::FeatureChamfer(Radius, Angle, Flip)` is the three-argument
        form and the one that works here; `InsertFeatureChamfer` wants eight
        arguments and a type constant and is only a fallback.
        """
        doc = self._require_doc()
        if edges in (None, "all"):
            picked = self._select_all_edges()
        else:
            picked = self._pick_edges(edges)
        if not picked:
            raise SwError("No edges to chamfer", code="no_edges")
        before = self._volume()
        feature = None
        for attempt in (
            lambda: doc.FeatureChamfer(_mm(distance), math.radians(angle_deg), False),
            lambda: self._fm().InsertFeatureChamfer(CHAMFER_EQUAL_DISTANCE, 0, _mm(distance),
                                                    0.0, 0.0, 0.0, 0.0, 0.0),
        ):
            try:
                feature = attempt()
            except Exception:  # noqa: BLE001
                feature = None
            if self._volume() < before - 1e-9:
                break
        after = self._volume()
        self._deselect()
        if after >= before - 1e-9:
            raise SwError("Chamfer changed nothing", code="chamfer_failed",
                          hint="Try a smaller distance, or a body with edges to break.",
                          details={"edges": picked})
        return {"feature": self._last_feature_named("Chamfer") or self._name_of(feature),
                "edges": picked, "distance_mm": distance,
                "volume_mm3": round(after * 1e9, 3),
                "removed_mm3": round((before - after) * 1e9, 3)}

    def shell(self, thickness, faces=None, outward=False):
        """Hollow the body; `faces` are [x, y, z] points on the faces to remove."""
        doc = self._require_doc()
        before = self._volume()
        self._deselect()
        removed = 0
        for x, y, z in (faces or []):
            if self._choose("", "FACE", x, y, z, append=True):
                removed += 1
        result = _call(lambda: doc.InsertFeatureShell(_mm(thickness), bool(outward)))
        named = _get(result, "Name") if result is not None and not isinstance(result, (int, bool)) else None
        if named is None:
            named = self._last_feature_named("Shell")
        after = self._volume()
        self._deselect()
        if after >= before - 1e-9:
            raise SwError("Shell changed nothing", code="shell_failed",
                          hint="A closed shell needs a closed solid; an open shell needs faces removed.",
                          details={"result": bool(result), "faces_removed": removed})
        return {"feature": str(named), "faces_removed": removed, "thickness_mm": thickness,
                "volume_mm3": round(after * 1e9, 3),
                "removed_mm3": round((before - after) * 1e9, 3)}

    def revolve(self, points, angle_deg=360.0, plane="front"):
        """Revolve a closed profile about a reference axis built from a sketch line.

        This follows the API help's own 360-degree example: an `Axis1` feature
        created from a line, the sketch selected with mark 0, the axis selected
        as type ``AXIS`` with mark **16**, then ``FeatureRevolve2`` with the end
        condition ``swEndCondBlind`` and a 2*pi angle.
        """
        self._require_doc()
        self._deselect()
        top = max(y for _, y in points)
        self._select_plane(plane)
        self._sketch_begin()
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
            self._line(_mm(x1), _mm(y1), _mm(x2), _mm(y2))
        # a line on the Y axis; the reference axis is built from it below
        _call(lambda: self._sm().CreateCenterLine(0.0, _mm(-1.0), 0.0, 0.0, _mm(top + 1.0), 0.0))
        self._sketch_end()
        name = self._sketch_name or self._last_sketch_name()

        self._deselect()
        picked = (self._choose(self._plane_name("front"), "PLANE", append=True)
                  and self._choose(self._plane_name("right"), "PLANE", append=True))
        axis = None
        if picked:
            _call(lambda: self._doc.InsertAxis2(True))
            self._deselect()
            axis = self._last_feature_named("RefAxis")
        if axis is None:
            raise SwError("Could not create the revolve axis", code="no_axis",
                          details={"line_selected": picked})

        before = self._volume()
        self._deselect()
        ok_sketch = self._choose(name, "SKETCH", mark=0)
        ok_axis = self._choose(axis, "AXIS", mark=16, append=True)
        feat = _call(lambda: self._fm().FeatureRevolve2(
            True, True, False, False, False, False, 0, 0,
            math.radians(angle_deg), 0.0,
            False, False, 0.01, 0.01, 0, 0.0, 0.0,
            True, True, True))
        after = self._volume()
        self._deselect()
        if axis and self._choose(axis, "AXIS"):
            _call(lambda: self._doc.BlankRefGeom())
        self._deselect()
        if after <= before + 1e-12:
            raise SwError(
                "Revolve produced no material", code="revolve_failed",
                hint="Keep the whole profile on one side of the axis line.",
                details={"profile": points, "angle_deg": angle_deg,
                         "sketch_selected": ok_sketch, "axis_selected": ok_axis,
                         "axis": axis})
        return {"feature": self._last_feature_named("Revolution") or self._name_of(feat),
                "axis": axis, "angle_deg": angle_deg,
                "volume_mm3": round(after * 1e9, 3),
                "added_mm3": round((after - before) * 1e9, 3)}

    # -- patterns ----------------------------------------------------------
    def circular_pattern(self, count, feature=None, axis=None, angle_deg=360.0):
        """Repeat a feature about an axis (created from a body face when not given)."""
        doc = self._require_doc()
        feature = feature or self._last_boss_name()
        if feature is None:
            raise SwError("No feature to pattern", code="no_feature")
        if axis is None:
            axis = self._make_axis_from_face()
        if axis is None:
            raise SwError("Could not create a reference axis", code="no_axis")
        before = self._volume()
        tries = []
        for axis_mark, feature_mark in ((1, 4), (0, 4), (1, 0), (0, 0)):
            self._deselect()
            ok_axis = self._choose(axis, "AXIS", mark=axis_mark)
            ok_feat = self._choose(feature, "BODYFEATURE", mark=feature_mark, append=True)
            if not (ok_axis and ok_feat):
                tries.append(f"marks {axis_mark}/{feature_mark}: selection {ok_axis}/{ok_feat}")
                continue
            result = None
            for call_fn in (
                # proven on this build: the legacy four-argument form creates the
                # feature where FeatureCircularPattern4 silently returns None
                lambda: self._fm().FeatureCircularPattern(
                    int(count), float(angle_deg), False, ""),
                lambda: self._fm().FeatureCircularPattern4(
                    int(count), float(angle_deg), False, "", True, True, False),
            ):
                try:
                    result = call_fn()
                except Exception as exc:  # noqa: BLE001
                    tries.append(f"marks {axis_mark}/{feature_mark}: {type(exc).__name__}")
                    result = None
                if result is not None:
                    break
            if result is not None and self._volume() > before + 1e-9:
                after = self._volume()
                self._deselect()
                return {"feature": self._name_of(result), "count": int(count),
                        "axis": axis, "source": feature, "angle_deg": angle_deg,
                        "volume_mm3": round(after * 1e9, 3),
                        "added_mm3": round((after - before) * 1e9, 3)}
            tries.append(f"marks {axis_mark}/{feature_mark}: feature={result is not None}")
        self._deselect()
        raise SwError("Circular pattern was not created", code="pattern_failed",
                      details={"axis": axis, "feature": feature, "tries": tries})

    def _make_axis_from_face(self):
        """A reference axis through the centre of the first cylindrical face."""
        body = self._body()
        face = _call(lambda: body.GetFirstFace()) if body is not None else None
        while face is not None:
            box = _get(face, "GetBox")
            normal = _get(face, "Normal")
            if box and normal and abs(abs(normal[2]) - 1.0) > 0.5:
                self._deselect()
                if self._pick_point("FACE", (box[0] + box[3]) / 2.0,
                                    (box[1] + box[4]) / 2.0, (box[2] + box[5]) / 2.0):
                    _call(lambda: self._doc.InsertAxis2(True))
                    self._deselect()
                    name = self._last_feature_named("RefAxis")
                    if name:
                        return name
            face = _get(face, "GetNextFace")
        # no cylinder: fall back to the axis where two default planes meet
        self._deselect()
        self._choose(self._plane_name("front"), "PLANE", append=True)
        self._choose(self._plane_name("right"), "PLANE", append=True)
        _call(lambda: self._doc.InsertAxis2(True))
        self._deselect()
        return self._last_feature_named("RefAxis")

    def linear_pattern(self, count, spacing, feature=None, direction=None):
        """Repeat a feature along a straight edge."""
        doc = self._require_doc()
        feature = feature or self._last_boss_name()
        if feature is None:
            raise SwError("No feature to pattern", code="no_feature")
        self._deselect()
        picked_dir = False
        if direction is not None:
            picked_dir = self._pick_point("EDGE", float(direction[0]), float(direction[1]),
                                          float(direction[2]))
        if not picked_dir:
            picked_dir = self._pick_first_edge()
        if picked_dir:
            ok_feat = self._choose(feature, "BODYFEATURE", mark=4, append=True)
        else:
            ok_feat = self._choose(feature, "BODYFEATURE", mark=4)
        before = self._volume()
        result = None
        for call_fn in (
            lambda: self._fm().FeatureLinearPattern(
                int(count), _mm(spacing), 1, 0.0, False, False, "", ""),
            lambda: self._fm().FeatureLinearPattern4(
                int(count), _mm(spacing), 1, 0.0, False, False, "", "",
                True, False, False, False, False, False, False, False, False, False,
                False, False, 0.0, 0.0),
        ):
            try:
                result = call_fn()
            except Exception:  # noqa: BLE001
                result = None
            if result is not None and self._volume() > before + 1e-9:
                break
        after = self._volume()
        self._deselect()
        if result is None or after <= before + 1e-9:
            raise SwError("Linear pattern was not created", code="pattern_failed",
                          details={"feature": feature, "direction_selected": picked_dir,
                                   "feature_selected": ok_feat})
        return {"feature": str(_get(result, "Name")), "count": int(count),
                "spacing_mm": spacing, "volume_mm3": round(after * 1e9, 3),
                "added_mm3": round((after - before) * 1e9, 3)}

    def _pick_first_edge(self):
        """Select the first edge of the first face - a usable pattern direction."""
        body = self._body()
        if body is None:
            return False
        face = _call(lambda: body.GetFirstFace())
        while face is not None:
            loop = _get(face, "GetFirstLoop")
            coedge = _get(loop, "GetFirstCoEdge") if loop is not None else None
            while coedge is not None:
                edge = _get(coedge, "GetEdge")
                if edge is not None:
                    entity = _qi(edge, "IEntity")
                    if entity is not None and _call(lambda e=entity: e.Select4(False, None)):
                        return True
                coedge = _get(coedge, "GetNext")
            face = _get(face, "GetNextFace")
        return False

    def mirror(self, feature=None, plane="right"):
        """Mirror a feature about a default plane."""
        self._require_doc()
        feature = feature or self._last_boss_name()
        plane_name = self._plane_name(plane)
        before = self._volume()
        tries = []
        # mirroring the body itself is the case that matters when the seed feature
        # has already merged into the plate
        for body_mark, plane_mark in ((256, 2), (128, 2)):
            self._deselect()
            ok_body = self._choose("", "SOLIDBODY", mark=body_mark)
            ok_plane = self._choose(plane_name, "PLANE", mark=plane_mark, append=True)
            if not (ok_body and ok_plane):
                tries.append(f"body marks {body_mark}/{plane_mark}: selection {ok_body}/{ok_plane}")
                continue
            for flags in ((True, False, False, False), (True, True, True, True)):
                try:
                    result = self._fm().InsertMirrorFeature(*flags)
                except Exception:  # noqa: BLE001
                    result = None
                tries.append(f"body marks {body_mark}/{plane_mark} {flags}: {result is not None}")
                if result is not None and self._volume() > before + 1e-9:
                    after = self._volume()
                    self._deselect()
                    return {"feature": str(_get(result, "Name")), "source": "body",
                            "plane": plane_name, "volume_mm3": round(after * 1e9, 3),
                            "added_mm3": round((after - before) * 1e9, 3)}
        for order in ("plane-first", "feature-first"):
            for plane_mark, feature_mark in ((2, 1), (2, 4), (0, 1)):
                self._deselect()
                if order == "plane-first":
                    ok_plane = self._choose(plane_name, "PLANE", mark=plane_mark)
                    ok_feat = self._choose(feature, "BODYFEATURE", mark=feature_mark, append=True)
                else:
                    ok_feat = self._choose(feature, "BODYFEATURE", mark=feature_mark)
                    ok_plane = self._choose(plane_name, "PLANE", mark=plane_mark, append=True)
                if not (ok_plane and ok_feat):
                    continue
                result = None
                for flags in ((True, True, True, True, 0), (True, False, False, False, 0)):
                    try:
                        result = self._fm().InsertMirrorFeature2(*flags)
                    except Exception:  # noqa: BLE001
                        result = None
                    if result is not None:
                        break
                if result is None:
                    for flags in ((True, False, False, False), (True, True, True, True)):
                        try:
                            result = self._fm().InsertMirrorFeature(*flags)
                        except Exception:  # noqa: BLE001
                            result = None
                        if result is not None:
                            break
                tries.append(f"{order} marks {plane_mark}/{feature_mark}: {result is not None}")
                if result is not None and self._volume() > before + 1e-9:
                    after = self._volume()
                    self._deselect()
                    return {"feature": str(_get(result, "Name")), "source": feature,
                            "plane": plane_name, "volume_mm3": round(after * 1e9, 3),
                            "added_mm3": round((after - before) * 1e9, 3)}
        self._deselect()
        raise SwError("Mirror was not created", code="mirror_failed",
                      details={"plane": plane_name, "feature": feature, "tries": tries})

    # -- reference geometry ------------------------------------------------
    def offset_plane(self, distance, base="front"):
        """A reference plane parallel to a default plane, offset by `distance` mm."""
        doc = self._require_doc()
        name = self._plane_name(base)
        self._deselect()
        if not self._choose(name, "PLANE"):
            raise SwError(f"Could not select plane {name!r}", code="plane_select_failed")
        before = {str(_get(f, "Name")) for f in self._features()}
        created = None
        out = ctypes.POINTER(ctypes.c_void_p)()
        for attempt in (
            lambda: doc.ICreatePlaneAtOffset2(_mm(distance), False),
            lambda: self._fm().InsertRefPlane(8, _mm(distance), 0, 0.0, 0.0, 0.0),
        ):
            try:
                created = attempt()
            except Exception:  # noqa: BLE001
                created = None
            if created:
                break
        self._deselect()
        # the new plane is whatever appeared in the tree that was not there before
        plane_name = None
        for feat in self._features():
            candidate = str(_get(feat, "Name"))
            if candidate not in before and str(_get(feat, "GetTypeName2")) in ("RefPlane", "Plane"):
                plane_name = candidate
        if not plane_name:
            for feat in self._features():
                candidate = str(_get(feat, "Name"))
                if candidate not in before:
                    plane_name = candidate
        if not plane_name:
            raise SwError("Could not create the offset plane", code="plane_create_failed",
                          details={"base": name, "distance_mm": distance,
                                   "features": [str(_get(f, "Name")) for f in self._features()][-6:]})
        return {"plane": plane_name or ("offset " + name), "base": name,
                "distance_mm": distance, "name_to_select": plane_name}

    def delete_feature(self, name):
        """Delete a feature by name."""
        self._require_doc()
        self._deselect()
        if not self._choose(name, "BODYFEATURE"):
            raise SwError(f"Could not select feature {name!r}", code="no_feature")
        ok = _call(lambda: self._ext().DeleteSelection2(0))
        self._deselect()
        _call(lambda: self._doc.EditRebuild3())
        if name in [str(_get(f, "Name")) for f in self._features()]:
            raise SwError(f"Feature {name!r} is still in the tree", code="delete_failed")
        return {"deleted": name, "ok": bool(ok),
                "volume_mm3": round(self._volume() * 1e9, 3)}

    def rebuild(self):
        ok = _call(lambda: self._doc.EditRebuild3())
        return {"rebuilt": True, "ok": ok, "volume_mm3": round(self._volume() * 1e9, 3)}

    def view(self, name="*Isometric", zoom_to_fit=True):
        doc = self._require_doc()
        code = VIEW_CODES.get(name, 7)
        ok = _call(lambda: doc.ShowNamedView2(name, code))
        if zoom_to_fit:
            _call(lambda: doc.ViewZoomtofit2())
        return {"view": name, "ok": bool(ok)}

    def _top_face_point(self):
        """A point on the highest upward-facing planar face, or None."""
        body = self._body()
        if body is None:
            return None
        best = None
        face = _get(body, "GetFirstFace")
        while face is not None:
            surface = _get(face, "GetSurface")
            if surface is not None and _get(surface, "IsPlane"):
                normal = _get(face, "Normal")
                if normal and normal[2] > 0.9:
                    box = _get(face, "GetBox")
                    if box and (best is None or box[2] > best[2]):
                        best = ((box[0] + box[3]) / 2.0, (box[1] + box[4]) / 2.0, box[2])
            face = _get(face, "GetNextFace")
        return best

    def _cut_circle_on_top(self, x, y, radius, depth):
        """Sketch a circle on the top face and cut it: material is under the sketch."""
        point = self._top_face_point()
        if point is None:
            raise SwError("No upward-facing planar face to start the hole from",
                          code="no_face")
        self._deselect()
        if not self._pick_point("FACE", point[0], point[1], point[2]):
            raise SwError("Could not select the top face", code="face_select_failed")
        sm = self._sm()
        _call(lambda: sm.InsertSketch(True))
        _call(lambda: sm.CreateCircleByRadius(_mm(x), _mm(y), 0.0, _mm(radius)))
        _call(lambda: sm.InsertSketch(True))
        return self._cut(depth)

    def counterbore(self, diameter, cbore_diameter, cbore_depth, depth=None, x=0.0, y=0.0):
        """A counterbored hole: wide blind hole from the top face, then the through hole."""
        self._require_doc()
        before = self._volume()
        # the small hole goes first: once the wide blind hole is cut, the face
        # above the centre is gone, so a through cut from the datum plane no
        # longer finds material at (x, y)
        # both holes from the same top face, which keeps them concentric; the
        # wide one is cut second because the first one leaves a hole at the very
        # point the face pick needs
        small = self.cut_hole(diameter, x, y, depth=depth, from_face=True)
        big = self.cut_hole(cbore_diameter, x, y, depth=cbore_depth, from_face=True)
        after = self._volume()
        if after >= before - 1e-12:
            raise SwError("Counterbore removed nothing", code="cut_failed")
        return {"diameter_mm": diameter, "cbore_diameter_mm": cbore_diameter,
                "cbore_depth_mm": cbore_depth, "depth_mm": depth,
                "features": [small.get("feature"), big.get("feature")],
                "volume_mm3": round(after * 1e9, 3),
                "removed_mm3": round((before - after) * 1e9, 3)}

    # -- assembly ----------------------------------------------------------
    def _adopt(self, raw, doc_type):
        """Take the document returned by NewDocument as current."""
        self._rawdoc = raw
        self._doc = _qi(raw, "IModelDoc2")
        self._planes = None
        self._sketch_name = None
        return self._doc

    def new_assembly(self, template=None):
        app = self._require_app()
        path = template or _template("assembly")
        result = _call(lambda: app.NewDocument(path, 0, 0, 0))
        if result is None:
            raise SwError("Could not create an assembly", code="new_document_failed",
                          details={"template": path})
        self._adopt(result, DOC_ASSEMBLY)
        return {"document": _get(self._doc, "GetTitle"), "template": path, "type": "assembly"}

    def add_component(self, path, x=0.0, y=0.0, z=0.0):
        """Drop an existing part into the current assembly."""
        self._require_doc()
        assembly = _qi(self._doc, "IAssemblyDoc")
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise SwError(f"No such part: {path}", code="file_missing")
        component = None
        try:
            component = self._invoke(assembly.AddComponent4, path, "", _mm(x), _mm(y), _mm(z))
        except Exception:  # noqa: BLE001
            component = None
        if not component:
            try:
                component = self._invoke(assembly.AddComponent2, path, _mm(x), _mm(y), _mm(z))
            except Exception:  # noqa: BLE001
                component = None
        if not component:
            raise SwError("Could not add the component", code="component_failed",
                          details={"path": path})
        _call(lambda: self._doc.EditRebuild3())
        return {"component": str(_get(component, "Name2") or _get(component, "Name") or ""),
                "path": path, "position_mm": [x, y, z]}

    def list_components(self):
        self._require_doc()
        items = []
        for feat in self._features():
            name = str(_get(feat, "Name"))
            kind = str(_get(feat, "GetTypeName2"))
            if kind in ("Reference", "ReferenceCurve") or "-" in name[-3:]:
                items.append({"name": name, "type": kind})
        return {"count": len(items), "components": items}

    def add_mate(self, kind="coincident", distance=0.0, angle_deg=0.0, align="closest"):
        """Mate whatever is selected right now (select the entities first)."""
        self._require_doc()
        assembly = _qi(self._doc, "IAssemblyDoc")
        mate_type = {"coincident": MATE_COINCIDENT, "concentric": MATE_CONCENTRIC,
                     "parallel": MATE_PARALLEL, "distance": MATE_DISTANCE,
                     "angle": MATE_ANGLE}.get(kind, MATE_COINCIDENT)
        align_value = MATE_ALIGN_CLOSEST if align == "closest" else 0
        errors = ctypes.c_long()
        mate = self._invoke(
            assembly.AddMate3,
            mate_type, align_value, False, _mm(distance), 0.0, 0.0,
            1.0, 1.0, math.radians(angle_deg), 0.0, 0.0, False, ctypes.byref(errors))
        if not mate:
            raise SwError("Mate was not created", code="mate_failed",
                          hint="Select the two entities (faces, edges or planes) first, "
                               "for example with sw_measure or by opening the assembly.",
                          details={"errors": errors.value, "kind": kind})
        return {"mate": str(_get(mate, "Name") or ""), "kind": kind,
                "errors": errors.value}

    # -- drawing -----------------------------------------------------------
    def new_drawing(self, template=None):
        app = self._require_app()
        path = template or _template("drawing")
        result = _call(lambda: app.NewDocument(path, 0, 0, 0))
        if result is None:
            raise SwError("Could not create a drawing", code="new_document_failed",
                          details={"template": path})
        self._adopt(result, DOC_DRAWING)
        return {"document": _get(self._doc, "GetTitle"), "template": path, "type": "drawing"}

    def drawing_view(self, model_path, view="*Front", x=0.15, y=0.15, scale=None):
        """Put the model on a drawing sheet.

        On this build `IDrawingDoc::CreateDrawViewFromModelView3` returns None no
        matter what (model open or not, `*Front` or `Front`), while
        `Create3rdAngleViews2(ModelName)` works and lays out front/top/right in
        one call - so that is the route used here, and the single-view call is
        only a fallback.
        """
        self._require_doc()
        drawing = _qi(self._doc, "IDrawingDoc")
        model_path = os.path.abspath(model_path)
        if not os.path.exists(model_path):
            raise SwError(f"No such model: {model_path}", code="file_missing")
        before = _call(lambda: drawing.GetViewCount()) or 0

        created = None
        problem = None
        try:
            made = _call(lambda: drawing.Create3rdAngleViews2(model_path))
            if made:
                created = "third-angle views"
            else:
                problem = "Create3rdAngleViews2 returned false"
        except Exception as exc:  # noqa: BLE001
            problem = f"{type(exc).__name__}: {exc}"
        if created is None:
            try:
                view_ptr = _call(lambda: drawing.CreateDrawViewFromModelView3(
                    model_path, view, float(x), float(y), 0.0))
                if view_ptr is not None:
                    created = str(_get(view_ptr, "Name") or view)
            except Exception as exc:  # noqa: BLE001
                problem = (problem or "") + f" | single view: {type(exc).__name__}: {exc}"

        _call(lambda: self._doc.EditRebuild3())
        after = _call(lambda: drawing.GetViewCount()) or 0
        if after <= before:
            raise SwError("No drawing view was created", code="view_failed",
                          details={"model": model_path, "views_before": before,
                                   "views_after": after, "problem": problem})
        return {"model": model_path, "views_before": before, "views_after": after,
                "route": created or "unknown"}

    def third_angle_views(self, model_path):
        """Front/top/right/isometric in one call (`Create3rdAngleViews2`)."""
        self._require_doc()
        drawing = _qi(self._doc, "IDrawingDoc")
        model_path = os.path.abspath(model_path)
        made = _call(lambda: drawing.Create3rdAngleViews2(model_path))
        _call(lambda: self._doc.EditRebuild3())
        if not made:
            raise SwError("Could not create the standard views", code="view_failed",
                          details={"model": model_path})
        return {"model": model_path, "views": "third angle (front/top/right/iso)",
                "views_on_sheet": _call(lambda: drawing.GetViewCount())}

    def drawing_annotations(self, all_views=True):
        """Bring the model's dimensions onto the drawing."""
        self._require_doc()
        drawing = _qi(self._doc, "IDrawingDoc")
        ok = ctypes.c_short()
        inserted = self._invoke(drawing.InsertModelAnnotations3,
                                0, 0, bool(all_views), True, False, False, ctypes.byref(ok))
        return {"inserted": bool(inserted) if inserted is not None else bool(ok.value),
                "all_views": bool(all_views)}

    # -- configurations ----------------------------------------------------
    def add_configuration(self, name, comment=""):
        """Add a configuration, using whichever AddConfigurationN this build has."""
        self._require_doc()
        manager = _get(self._doc, "ConfigurationManager")
        created = None
        attempts = (
            lambda: self._invoke(manager.AddConfiguration2,
                                 name, comment, "", 0, "", "", True),
            lambda: self._invoke(manager.AddConfiguration,
                                 name, comment, "", False, False, False, True, 0),
        )
        for attempt in attempts:
            try:
                created = attempt()
            except Exception:  # noqa: BLE001
                created = None
            if created is not None:
                break
        if created is None:
            existing = {str(c.Name) for c in (() if manager is None else ())}
            try:
                existing = {str(manager.GetConfigurationNames()[i]) for i in range(0)}
            except Exception:  # noqa: BLE001
                existing = set()
            if name in existing:
                return {"configuration": name, "comment": comment, "existing": True}
        if created is None:
            raise SwError(f"Could not add configuration {name!r}", code="config_failed")
        return {"configuration": name, "comment": comment}

    def activate_configuration(self, name):
        doc = self._require_doc()
        ok = _call(lambda: doc.ShowConfiguration2(name))
        _call(lambda: doc.EditRebuild3())
        return {"configuration": name, "activated": bool(ok),
                "volume_mm3": round(self._volume() * 1e9, 3)}

    def family_table(self, rows):
        """Build one configuration per row: {name, dimensions: {dim: value_mm}}."""
        self._require_doc()
        built = []
        for row in rows:
            name = str(row["name"])
            self.add_configuration(name, row.get("comment", ""))
            self.activate_configuration(name)
            values = {}
            for dim, value in (row.get("dimensions") or {}).items():
                self.set_dimension(dim, float(value))
                values[dim] = float(value)
            built.append({"configuration": name, "dimensions": values,
                          "volume_mm3": round(self._volume() * 1e9, 3)})
        return {"configurations": built, "count": len(built)}
