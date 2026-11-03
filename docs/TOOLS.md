# Tool reference

39 tools. `*` marks a required argument. The **Status** column is what was actually measured
on SOLIDWORKS 2022 SP05 — ✅ verified with the number, ⚠️ wired but never exercised, ❌ known
broken on that build.

Every modelling tool returns the measured `volume_mm3` (and usually `removed_mm3` /
`added_mm3`), so a no-op is visible in the response instead of silently "succeeding".

## Session and document

| Tool | Arguments | Status | Notes |
| --- | --- | --- | --- |
| `sw_status` | — | ✅ | revision, UI language, active document, licence probe |
| `sw_new_part` | — | ✅ | partial template from disk, returns the localised plane names |
| `sw_open_part` | `path*` | ✅ | also opens STEP/IGES via the STEP translator |
| `sw_close_part` | `save` | ✅ | `save=true` writes back first |
| `sw_list_features` | — | ✅ | ordered tree; names are localised (`凸台-拉伸1`) |
| `sw_mass_properties` | — | ✅ | `volume_mm3`, `mass_g`, `surface_area_mm2`, `bbox_m` |
| `sw_rebuild` | — | ✅ | `EditRebuild3` + new volume |
| `sw_view` | `name`, `zoom_to_fit` | ✅ | `*Isometric`, `*Front`, `*Top`, … |
| `sw_screenshot` | `path*`, `width`, `height` | ✅ | `SaveBMP` then pure-Python BMP→PNG |
| `sw_save` | `path*` | ✅ | native `.sldprt`, refuses writes outside the roots |
| `sw_export` | `path*` | ✅ | `.step .stp .stl .igs .x_t .3mf .pdf` from the extension |
| `sw_set_dimension` | `name*`, `value*` | ✅ | `D1@Extrude1`, millimetres, rebuilds |

## Solids

| Tool | Arguments | Status | Notes |
| --- | --- | --- | --- |
| `sw_add_box` | `width* height* depth* plane flip` | ✅ | 100×60×20 → 120000 mm³ |
| `sw_add_cylinder` | `diameter* height* plane flip` | ✅ | ⌀40×50 → 62831.85 mm³ |
| `sw_add_profile` | `points* depth* plane flip` | ✅ | closed `[[x, y], …]` polygon |
| `sw_cut_hole` | `diameter* x y depth end axis plane` | ✅ | `end="through_next"` cuts one wall; `axis` picks the face |
| `sw_cut_pocket` | `points* depth` | ✅ | blind pocket or through-all |
| `sw_counterbore` | `diameter* cbore_diameter* cbore_depth* depth x y` | ✅ | ⌀16×6 + ⌀8 → 1910.088 mm³ exactly |
| `sw_gear` | `teeth outer_diameter* thickness bore pressure_angle` | ✅ | true involute flanks, 24T ⌀100 → 62484.6 mm³ |
| `sw_fillet` | `radius* edges` | ✅ | `edges="all"` or `[[x,y,z], …]` |
| `sw_chamfer` | `distance* edges angle_deg` | ✅ | 48000 → 46812 mm³ |
| `sw_shell` | `thickness* faces outward` | ✅ | 48000 → 16788 mm³ with one face open |
| `sw_revolve` | `points* angle_deg plane` | ✅ | shaft 1010273.366 mm³, exact |
| `sw_circular_pattern` | `count* feature axis angle_deg` | ✅ | 41152 → 48418 mm³ |
| `sw_linear_pattern` | `count* spacing* feature direction` | ⚠️ | wired, never measured |
| `sw_mirror` | `feature plane` | ✅ | 41152 → 82304 mm³ |
| `sw_offset_plane` | `distance* base` | ✅ | `基准面1`, then usable as `plane=` |
| `sw_delete_feature` | `name*` | ✅ | verifies the name left the tree |

## Assembly

| Tool | Arguments | Status | Notes |
| --- | --- | --- | --- |
| `sw_new_assembly` | — | ✅ | `.asmdot` template |
| `sw_add_component` | `path* x y z` | ✅ | 2 components in one assembly |
| `sw_list_components` | — | ✅ | |
| `sw_add_mate` | `kind distance angle_deg align` | ⚠️ | needs the two entities selected first; never measured |

## Drawing

| Tool | Arguments | Status | Notes |
| --- | --- | --- | --- |
| `sw_new_drawing` | — | ✅ | A0 `.drwdot` |
| `sw_add_drawing_view` | `model_path* view x y scale` | ✅ | falls back to the 4-view route |
| `sw_standard_views` | `model_path*` | ✅ | `Create3rdAngleViews2` → 4 views, **model must be open** |
| `sw_drawing_annotations` | `all_views` | ⚠️ | `InsertModelAnnotations3`, never measured |

## Configurations

| Tool | Arguments | Status | Notes |
| --- | --- | --- | --- |
| `sw_add_configuration` | `name* comment` | ✅ | `AddConfiguration2`, falls back to `AddConfiguration` |
| `sw_activate_configuration` | `name*` | ✅ | |
| `sw_family_table` | `rows*` | ✅ | `[{name, dimensions: {"D1@Extrude1": 30}}]` → 15000 / 30000 mm³ |

## Failure codes

`not_connected`, `no_document`, `no_typelib`, `no_template`, `path_not_allowed`,
`extrude_failed`, `cut_failed`, `chamfer_failed`, `shell_failed`, `revolve_failed`,
`pattern_failed`, `mirror_failed`, `plane_create_failed`, `face_select_failed`,
`no_feature`, `file_missing`, `view_failed`, `mate_failed`, `config_failed`,
`save_failed`, `export_failed`, `bad_argument`, `internal`.

Each carries a `hint` meant to be read by the model, e.g.

```json
{"code": "cut_failed",
 "message": "Cut removed no material",
 "hint": "The sketch may not overlap the body, or it is not closed."}
```
