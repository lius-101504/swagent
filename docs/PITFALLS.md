# SolidWorks COM pitfalls

Everything here was measured on **SOLIDWORKS 2022 SP05, Windows 11, Python 3.14, comtypes 1.4.17**.
If you are writing your own driver, these are the traps that cost the most time.

## 1. pywin32 cannot bind this type library

```python
win32com.client.gencache.EnsureDispatch("SldWorks.Application")
# TypeError: This COM object can not automate the makepy process - please run makepy manually
win32com.client.GetActiveObject("SldWorks.Application")
# com_error (-2147221021, '操作无法使用')      # MK_E_UNAVAILABLE, even while SLDWORKS runs
```

Dynamic dispatch (`Dispatch`) returns an object whose `_username_` is `<unknown>`; members that
live on the interfaces raise `AttributeError: <unknown>.FirstFeature`. The feature tree — and
therefore planes, bodies, measurements — is invisible.

**comtypes works.** Point it at the type library and use the early-bound interfaces:

```python
import comtypes.client
comtypes.client.GetModule(r"...\sldworks.tlb")     # generates comtypes.gen.SldWorks once
from comtypes.gen import SldWorks as sw
app = comtypes.client.CreateObject("SldWorks.Application")
```

## 2. The document you get back is not the document you need

```python
raw = app.NewDocument(template, 0, 0, 0)      # POINTER(IPartDoc)
doc = raw.QueryInterface(sw.IModelDoc2)        # ← this is the real document
doc.GetTitle(); doc.FirstFeature()
```

Mass properties, `Extension`, `SketchManager`, `FeatureManager` all come from `IModelDoc2`.
Calling a `IModelDoc2` method on the `IPartDoc` pointer gives `AttributeError`.

## 3. Interface SAFEARRAYs raise `KeyError: 9`

`IPartDoc.GetBodies2`, `IBody2.GetFaces`, `IFace2.GetEdges` return `SAFEARRAY` of interface
pointers, which comtypes refuses to unmarshal (`9` = `VT_DISPATCH`). The COM call has already
run, so treat `KeyError: 9` as "value discarded, call succeeded" and walk instead:

```
IPartDoc → feature.GetBody() → body.GetFirstFace() → face.GetNextFace()
face.GetFirstLoop() → loop.GetFirstCoEdge() → coedge.GetNext() / coedge.GetEdge()
```

Note `IFeature.GetBody` is often missing on dispatch objects from a tree walk —
fall back to selecting a face by coordinate and reading `IFace2.GetBody`.

## 4. Properties that look like methods

On `IFace2` this typelib exposes `GetBox()`, `Normal()` and `GetFirstLoop()` as **properties**.
Calling them raises `TypeError: 'tuple' object is not callable`. Read them with `getattr`.
`IModelDoc2.ViewDisplayShadedWithEdges` does not exist at all — only `ViewDisplayShaded()`.

## 5. Argument counts: the last `[out, retval]` parameter must be omitted

comtypes strips a trailing retval parameter from the call, so the Python signature is
`len(argtypes) - 1`. Measured on 2022 SP05:

| Method | Pass |
| --- | --- |
| `IFeatureManager.FeatureExtrusion2` | 22 |
| `IFeatureManager.FeatureRevolve2` | 20 |
| `IFeatureManager.FeatureCut3` / `FeatureCut4` | 25 / 26 |
| `IFeatureManager.FeatureCircularPattern4` | 7 |
| `IFeatureManager.FeatureLinearPattern4` | 20 |
| `IFeatureManager.InsertMirrorFeature2` | 5 |
| `IFeatureManager.InsertRefPlane` | 6 |
| `IFeatureManager.FeatureFillet3` | 14 |
| `IModelDoc2.FeatureCut3` | 15 (last = `KeepPieceIndex`) |
| `IModelDoc2.ICreatePlaneAtOffset2` | 2 |
| `IAssemblyDoc.AddComponent4` | 5 |
| `IDrawingDoc.Create3rdAngleViews2` | 1 |

The error text counts `self`, so "takes exactly N arguments (M given)" means the method wanted
**N − 1**. Re-measure for another release with:

```python
fn = sw.IFeatureManager.FeatureRevolve2
print(len(fn.argtypes), fn.restype)     # pass len(argtypes) - 1
```

## 6. A cut only materialises from a face sketch that is still open

The shipped example `Insert_Cut_Extrude_Example_VB.htm` is the recipe:

```
SelectByID2("", "FACE", x, y, z, False, 0, Nothing, 0)
SketchManager.InsertSketch(True)
ClearSelection2(True)
CreateCircleByRadius(...)
FeatureCut3(...)          # ← while the sketch is still being edited
```

Closing the sketch and selecting it by name works for `through_all` and silently builds nothing
for a blind depth. Consequences:

- `_volume()` returns **0** while a sketch is open, so capture the "before" volume first.
- After the first hole, the pick point at the face centre is inside the hole; nudge it
  (+8 mm, +20, +40, −8) until `SelectByID2` returns True.

## 7. Blind holes: `through_next` beats a depth

`FeatureCut3` with `swEndCondBlind` behaves erratically through early binding; `swEndCondThroughNext`
(2) and `swEndCondUpToNext` (11) both cut exactly one wall and measure correctly.
`swEndCondThroughAllBoth` = 9 drills the whole body, which is how a bolt hole through a flange
became a hole through a 600 mm shaft.

## 8. Revolved bodies have a shifted sketch frame

On an **extruded** face the sketch coordinates equal the part coordinates (verified: a ⌀12 hole
at (28,18) removed the clipped overlap, 1116.443). On a **revolved** face they do not
(3304.046 instead of 3078.761). So model flanges as extrusions, drill them, and revolve the
body afterwards; do not drill a revolved flange.

## 9. Elevation and VBA

On the machine this was written for, VBA 7.1 only initialises for an elevated process. An
unelevated SOLIDWORKS then reports **success** for every save and writes **0 bytes**. If
`SaveAs3` returns True and the file is empty, run the host and SOLIDWORKS in the same elevation
state. `ISldWorks::GetErrorMessages` tells you whether VBA actually started
(`(None, None, None, 0)` is healthy).

## 10. Single-instance behaviour

- `GetActiveObject` never works, so a launcher must call `CreateObject` and be prepared to have
  SolidWorks start its own process.
- A COM-launched instance may show a modal VBA warning; a `Start-Process` instance may not.
  Either way, a modal dialog blocks saves.
- Killing SLDWORKS while documents are modified leaves a "save changed documents?" prompt and
  can leave kernel-stuck processes that only a reboot clears.

## 11. Screenshots

`SaveBMP(path, width, height)` is the only raster API; it captures the whole window, so with the
FeatureManager visible the part sits off-centre. Either size the bitmap to the window or crop
the background afterwards (this project ships `crop_bmp()`: bounding box of pixels darker than
238, 5 % padding). Blank reference axes and planes before capturing, or they show up as orange
squares in the render.

## 12. Localisation

Plane and feature names are localised (`前视基准面`, `凸台-拉伸1`, `零件1`). Never hardcode them:
read the `RefPlane` features from the tree in order, and read `GetTypeName2()` rather than
matching display names.
