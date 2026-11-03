"""MCP tool surface for SolidWorks.

Design rule: no tool runs arbitrary code or arbitrary API calls. The agent can
only do what is listed here, every modelling tool returns the measured volume
and bounding box of the result, and files land under an allowed root. That
keeps a wrong prompt from becoming a wrong part nobody checked.
"""
import os

from .errors import SwError

# Everything the server writes must stay inside these roots. Both can be
# widened with SWAGENT_OUTPUT_ROOTS (os.pathsep separated).
from .locate import output_roots as _locate_roots

DEFAULT_ROOTS = None          # filled from swagent.locate on first use


def _roots():
    return _locate_roots()


def resolve_output(path):
    """Resolve an output path and refuse anything outside the allowed roots."""
    target = os.path.abspath(path)
    for root in _roots():
        if target == root or target.startswith(root + os.sep):
            return target
    raise SwError(
        f"Refusing to write outside the allowed folders: {target}",
        code="path_not_allowed",
        hint=f"Allowed roots: {', '.join(_roots())} (extend with SWAGENT_OUTPUT_ROOTS).",
    )


TOOLS = [
    {
        "name": "sw_status",
        "description": (
            "Check the connection to SolidWorks. Reports the attached instance, its revision and "
            "UI language, the active document, and whether the configured licence server answers. "
            "Call this first; if it reports an unreachable licence server, no modelling call can work."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_new_part",
        "description": (
            "Create a new empty part from the installed part template and make it current. "
            "Returns the localised names of the three default planes, which every later tool uses."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_open_part",
        "description": "Open an existing .sldprt (or an imported STEP/IGES/Parasolid part) and make it current.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to the file to open."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_close_part",
        "description": "Close the current document. Set save=true to write it back to its existing path first.",
        "inputSchema": {
            "type": "object",
            "properties": {"save": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_add_box",
        "description": (
            "Add a centred box (rectangle sketch + blind extrude) to the current part. "
            "Dimensions in millimetres. Returns the feature name plus the measured volume and "
            "bounding box, so the result can be checked against a hand calculation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "number", "description": "Size along X, mm."},
                "height": {"type": "number", "description": "Size along Y, mm."},
                "depth": {"type": "number", "description": "Extrude depth along Z, mm."},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "front"},
                "flip": {"type": "boolean", "default": False, "description": "Extrude in -normal instead of +normal."},
            },
            "required": ["width", "height", "depth"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_add_cylinder",
        "description": "Add a centred cylinder (circle sketch + blind extrude). Diameter and height in millimetres.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "diameter": {"type": "number", "description": "Diameter, mm."},
                "height": {"type": "number", "description": "Height, mm."},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "front"},
                "flip": {"type": "boolean", "default": False},
            },
            "required": ["diameter", "height"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_add_profile",
        "description": (
            "Extrude a closed polygon given as [x, y] millimetre points on a default plane. "
            "Use this for brackets and plates that are not plain rectangles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "description": "Closed polygon in mm, e.g. [[-40,-30],[40,-30],[40,30],[-40,30]]. Do not repeat the first point.",
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "minItems": 3,
                },
                "depth": {"type": "number", "description": "Extrude depth, mm."},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "front"},
                "flip": {"type": "boolean", "default": False},
            },
            "required": ["points", "depth"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_cut_hole",
        "description": (
            "Drill a round hole into the part's topmost upward-facing planar face. "
            "x and y are millimetres from the part origin; omit depth for a through hole. "
            "Verifies the cut actually removed material before reporting success."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "diameter": {"type": "number", "description": "Hole diameter, mm."},
                "x": {"type": "number", "default": 0.0, "description": "X position from the part origin, mm."},
                "y": {"type": "number", "default": 0.0, "description": "Y position from the part origin, mm."},
                "depth": {"type": "number", "description": "Blind depth in mm; omit for through-all."},
                "end": {"type": "string", "enum": ["through_all", "through_next"],
                        "default": "through_all",
                        "description": "through_all drills every wall on the line; through_next "
                                       "stops at the far side of the first wall (bolt holes)."},
                "axis": {"type": "string", "enum": ["x", "y", "z"], "default": "z",
                         "description": "Which face the hole starts from: the outermost +axis face."},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "front",
                          "description": "Datum plane for the sketch when the part has no solid body yet."},
            },
            "required": ["diameter"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_cut_pocket",
        "description": (
            "Cut a closed polygon pocket into the topmost upward-facing planar face. "
            "Points are [x, y] millimetres from the part origin; omit depth for through-all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "minItems": 3,
                },
                "depth": {"type": "number", "description": "Blind depth in mm; omit for through-all."},
            },
            "required": ["points"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_fillet",
        "description": (
            "Round edges of the part with a constant radius. edges='all' rounds every edge, "
            "'first' rounds a single edge (useful as a minimal probe)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "radius": {"type": "number", "description": "Fillet radius, mm."},
                "edges": {"type": "string", "enum": ["all", "first"], "default": "all"},
            },
            "required": ["radius"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_mass_properties",
        "description": "Measured volume, mass, surface area, density and tight bounding box of the current part (mm-based).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_list_features",
        "description": (
            "List the feature tree in order (name and type). Feature names are localised on a "
            "Chinese install, so read them from here instead of assuming English names."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_set_dimension",
        "description": (
            "Set a named driving dimension (for example D1@Extrude1) in millimetres and rebuild, "
            "returning the new measured volume so the change can be checked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Dimension name, e.g. D1@Extrude1."},
                "value": {"type": "number", "description": "New value in millimetres (angles in degrees)."},
            },
            "required": ["name", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_save",
        "description": "Save the current document as a native .sldprt inside the allowed output folders.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Target .sldprt path."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_export",
        "description": (
            "Export the current part. Format is taken from the extension: .step/.stp, .stl, .igs, "
            ".x_t, .3mf or .pdf. Verifies the file was written and reports its size."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Target path with the desired extension."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_screenshot",
        "description": (
            "Capture the current model view to a PNG (SolidWorks writes BMP, the server converts it). "
            "Use this to actually look at the part instead of trusting the numbers alone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target .png path."},
                "width": {"type": "integer", "default": 1600},
                "height": {"type": "integer", "default": 1200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_gear",
        "description": (
            "Build a spur gear with true involute flanks: a disc (root circle plus bore) "
            "extruded, a reference axis from that disc, one tooth extruded, and a circular "
            "pattern of that tooth. Returns the analytic volume next to the measured one. "
            "Diameter is the OUTSIDE (tip) diameter in millimetres."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "teeth": {"type": "integer", "minimum": 6, "maximum": 200, "default": 24},
                "outer_diameter": {"type": "number", "exclusiveMinimum": 0,
                                   "description": "Tip diameter, mm."},
                "thickness": {"type": "number", "exclusiveMinimum": 0, "default": 10},
                "bore": {"type": "number", "minimum": 0, "default": 0,
                         "description": "Centre bore diameter, mm; 0 for none."},
                "pressure_angle": {"type": "number", "default": 20},
            },
            "required": ["outer_diameter"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_chamfer",
        "description": "Break edges with an equal-distance chamfer. edges='all' or [x, y, z] points on the edges.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "distance": {"type": "number", "exclusiveMinimum": 0, "description": "Chamfer distance, mm."},
                "edges": {"description": "'all' or [[x, y, z], ...] points on the edges."},
                "angle_deg": {"type": "number", "default": 45},
            },
            "required": ["distance"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_shell",
        "description": "Hollow the solid. Face points open the shell; with none it stays closed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thickness": {"type": "number", "exclusiveMinimum": 0},
                "faces": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}},
                "outward": {"type": "boolean", "default": False},
            },
            "required": ["thickness"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_revolve",
        "description": "Revolve a closed [x, y] mm profile about the sketch Y axis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "minItems": 3},
                "angle_deg": {"type": "number", "default": 360},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "front"},
            },
            "required": ["points"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_circular_pattern",
        "description": "Repeat a feature about an axis; an axis is created from the body when none is named.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 2, "maximum": 500},
                "feature": {"type": "string"},
                "axis": {"type": "string"},
                "angle_deg": {"type": "number", "default": 360},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_linear_pattern",
        "description": "Repeat a feature along a straight edge: count copies, spacing mm apart.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 2, "maximum": 500},
                "spacing": {"type": "number", "exclusiveMinimum": 0},
                "feature": {"type": "string"},
                "direction": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "[x, y, z] point on the edge to use as direction."},
            },
            "required": ["count", "spacing"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_mirror",
        "description": "Mirror a feature about a default plane.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string"},
                "plane": {"type": "string", "enum": ["front", "top", "right"], "default": "right"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_offset_plane",
        "description": "Create a reference plane parallel to a default plane.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "distance": {"type": "number", "description": "Offset in mm."},
                "base": {"type": "string", "enum": ["front", "top", "right"], "default": "front"},
            },
            "required": ["distance"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_counterbore",
        "description": "Counterbored hole on the topmost upward face: wide shallow hole plus through hole.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "diameter": {"type": "number"},
                "cbore_diameter": {"type": "number"},
                "cbore_depth": {"type": "number"},
                "depth": {"type": "number"},
                "x": {"type": "number", "default": 0},
                "y": {"type": "number", "default": 0},
            },
            "required": ["diameter", "cbore_diameter", "cbore_depth"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_delete_feature",
        "description": "Delete a feature by name and rebuild.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_rebuild",
        "description": "Rebuild and report the resulting volume.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_view",
        "description": "Switch to a standard view and zoom to fit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "*Isometric"},
                "zoom_to_fit": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_new_assembly",
        "description": "Create a new assembly from the installed assembly template.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_add_component",
        "description": "Insert a part into the current assembly at a position in mm.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "x": {"type": "number", "default": 0},
                "y": {"type": "number", "default": 0},
                "z": {"type": "number", "default": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_list_components",
        "description": "List the components of the current assembly.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_add_mate",
        "description": "Mate whatever is currently selected: coincident, concentric, parallel, distance, angle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["coincident", "concentric", "parallel", "distance", "angle"], "default": "coincident"},
                "distance": {"type": "number", "default": 0},
                "angle_deg": {"type": "number", "default": 0},
                "align": {"type": "string", "enum": ["closest", "aligned", "anti-aligned"], "default": "closest"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_new_drawing",
        "description": "Create a new drawing from the installed drawing template.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sw_add_drawing_view",
        "description": "Place a view of a model on the current sheet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_path": {"type": "string"},
                "view": {"type": "string", "default": "*Front"},
                "x": {"type": "number", "default": 0.15},
                "y": {"type": "number", "default": 0.15},
                "scale": {"type": "number"},
            },
            "required": ["model_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_standard_views",
        "description": "Create third-angle standard views (front, top, right, isometric) of a model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_path": {"type": "string"},
            },
            "required": ["model_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_drawing_annotations",
        "description": "Insert the model's dimensions into the drawing views.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "all_views": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_add_configuration",
        "description": "Add a configuration to the current part.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "comment": {"type": "string", "default": ""},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_activate_configuration",
        "description": "Switch to a configuration and report its volume.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sw_family_table",
        "description": "Build one configuration per row from a table of dimension values: rows = [{name, dimensions: {'D1@Extrude1': 12}}].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"name": {"type": "string"}, "comment": {"type": "string"}, "dimensions": {"type": "object", "additionalProperties": {"type": "number"}}}, "required": ["name"], "additionalProperties": False}},
            },
            "required": ["rows"],
            "additionalProperties": False,
        },
    },
]


def _out_path(args, key="path"):
    return resolve_output(args[key])


def call(session, name, args):
    """Dispatch one tool call and return a JSON-serialisable result."""
    args = args or {}
    if name == "sw_status":
        return session.status()
    if name == "sw_new_part":
        return session.new_part()
    if name == "sw_open_part":
        return session.open_part(os.path.abspath(args["path"]))
    if name == "sw_close_part":
        return session.close(save=bool(args.get("save", False)))
    if name == "sw_add_box":
        return session.add_box(
            float(args["width"]), float(args["height"]), float(args["depth"]),
            plane=args.get("plane", "front"), flip=bool(args.get("flip", False)),
        )
    if name == "sw_add_cylinder":
        return session.add_cylinder(
            float(args["diameter"]), float(args["height"]),
            plane=args.get("plane", "front"), flip=bool(args.get("flip", False)),
        )
    if name == "sw_add_profile":
        return session.add_profile(
            [tuple(map(float, p)) for p in args["points"]], float(args["depth"]),
            plane=args.get("plane", "front"), flip=bool(args.get("flip", False)),
        )
    if name == "sw_cut_hole":
        depth = args.get("depth")
        end = args.get("end") or None
        return session.cut_hole(
            float(args["diameter"]), float(args.get("x", 0.0)), float(args.get("y", 0.0)),
            depth=None if depth in (None, "") else float(depth),
            plane=args.get("plane") or "front",
            axis=args.get("axis") or "z",
            end=None if end in (None, "", "through_all") else end,
        )
    if name == "sw_cut_pocket":
        depth = args.get("depth")
        return session.cut_pocket(
            [tuple(map(float, p)) for p in args["points"]],
            depth=None if depth in (None, "") else float(depth),
        )
    if name == "sw_fillet":
        return session.fillet(float(args["radius"]), edges=args.get("edges", "all"))
    if name == "sw_gear":
        return session.gear(
            int(args.get("teeth", 24)), float(args["outer_diameter"]),
            float(args.get("thickness", 10.0)), float(args.get("bore", 0.0)),
            float(args.get("pressure_angle", 20.0)),
        )
    if name == "sw_chamfer":
        return session.chamfer(float(args["distance"]), args.get("edges", "all"),
                               float(args.get("angle_deg", 45.0)))
    if name == "sw_shell":
        return session.shell(float(args["thickness"]), args.get("faces") or [],
                             bool(args.get("outward", False)))
    if name == "sw_revolve":
        return session.revolve([tuple(map(float, p)) for p in args["points"]],
                               float(args.get("angle_deg", 360.0)), args.get("plane", "front"))
    if name == "sw_circular_pattern":
        return session.circular_pattern(int(args["count"]), args.get("feature"),
                                        args.get("axis"), float(args.get("angle_deg", 360.0)))
    if name == "sw_linear_pattern":
        direction = args.get("direction")
        return session.linear_pattern(int(args["count"]), float(args["spacing"]),
                                      args.get("feature"), tuple(direction) if direction else None)
    if name == "sw_mirror":
        return session.mirror(args.get("feature"), args.get("plane", "right"))
    if name == "sw_offset_plane":
        return session.offset_plane(float(args["distance"]), args.get("base", "front"))
    if name == "sw_counterbore":
        depth = args.get("depth")
        return session.counterbore(
            float(args["diameter"]), float(args["cbore_diameter"]), float(args["cbore_depth"]),
            None if depth in (None, "") else float(depth),
            float(args.get("x", 0.0)), float(args.get("y", 0.0)))
    if name == "sw_delete_feature":
        return session.delete_feature(args["name"])
    if name == "sw_rebuild":
        return session.rebuild()
    if name == "sw_view":
        return session.view(args.get("name", "*Isometric"), bool(args.get("zoom_to_fit", True)))
    if name == "sw_new_assembly":
        return session.new_assembly()
    if name == "sw_add_component":
        return session.add_component(args["path"], float(args.get("x", 0.0)),
                                     float(args.get("y", 0.0)), float(args.get("z", 0.0)))
    if name == "sw_list_components":
        return session.list_components()
    if name == "sw_add_mate":
        return session.add_mate(args.get("kind", "coincident"), float(args.get("distance", 0.0)),
                                float(args.get("angle_deg", 0.0)), args.get("align", "closest"))
    if name == "sw_new_drawing":
        return session.new_drawing()
    if name == "sw_add_drawing_view":
        return session.drawing_view(args["model_path"], args.get("view", "*Front"),
                                    float(args.get("x", 0.15)), float(args.get("y", 0.15)),
                                    args.get("scale"))
    if name == "sw_standard_views":
        return session.third_angle_views(args["model_path"])
    if name == "sw_drawing_annotations":
        return session.drawing_annotations(bool(args.get("all_views", True)))
    if name == "sw_add_configuration":
        return session.add_configuration(args["name"], args.get("comment", ""))
    if name == "sw_activate_configuration":
        return session.activate_configuration(args["name"])
    if name == "sw_family_table":
        return session.family_table(args["rows"])
    if name == "sw_mass_properties":
        return session.mass_properties()
    if name == "sw_list_features":
        return session.list_features()
    if name == "sw_set_dimension":
        return session.set_dimension(args["name"], float(args["value"]))
    if name == "sw_save":
        return session.save(_out_path(args))
    if name == "sw_export":
        return session.export(_out_path(args))
    if name == "sw_screenshot":
        return session.screenshot(
            _out_path(args), int(args.get("width", 1600)), int(args.get("height", 1200))
        )
    raise SwError(f"Unknown tool: {name}", code="unknown_tool")
