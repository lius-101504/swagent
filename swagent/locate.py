"""Find the pieces of a SOLIDWORKS installation this server needs.

Everything here is discovered at run time rather than hard-coded, so the same
package runs on another machine: the type library path comes from the registry
the installer writes, and the folders we are allowed to write to come from an
environment variable (or default to the folder that contains the package).
"""
import glob
import os
import winreg

from .errors import SwError

# {83A33D31-27C5-11CE-BFD4-00400513BB57} — the SOLIDWORKS type library, the same
# GUID in every release since 1995; only the version subkey (1.0, 1e.0, …) moves.
TLB_GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
TLB_ENV = "SWAGENT_SLDWORKS_TLB"
ROOTS_ENV = "SWAGENT_OUTPUT_ROOTS"

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_PACKAGE_DIR)


def _registry_typelines():
    """Every (version, path) pair the registry knows for the type library."""
    bases = (
        r"SOFTWARE\Classes\TypeLib",
        r"SOFTWARE\Classes\WOW6432Node\TypeLib",
    )
    found = []
    for base in bases:
        path = base + "\\" + TLB_GUID
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                versions = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        except OSError:
            continue
        for version in versions:
            for plat in ("win64", "win32"):
                sub = path + "\\" + version + "\\0\\" + plat
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                if value and os.path.exists(value):
                    found.append((version, value))
    return found


def _glob_typelines():
    """Fallback for installs whose registry entry is missing."""
    patterns = [
        r"C:\Program Files\SOLIDWORKS Corp\*\sldworks.tlb",
        r"C:\Program Files\SOLIDWORKS Corp\*\*.tlb",
        r"D:\solidworks*\*\sldworks.tlb",
        r"D:\solidworks*\*\*\sldworks.tlb",
    ]
    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return [path for path in found if os.path.basename(path).lower() == "sldworks.tlb"]


def solidworks_typelib():
    """Absolute path of `sldworks.tlb`, or SwError explaining what to do."""
    override = os.environ.get(TLB_ENV)
    if override:
        if os.path.exists(override):
            return override
        raise SwError(
            f"{TLB_ENV} points at {override}, which does not exist",
            code="no_typelib",
            hint=f"Point {TLB_ENV} at the sldworks.tlb of your installation.",
        )
    candidates = _registry_typelines()
    if candidates:
        # prefer the newest registered version, then a 64-bit entry
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]
    fallback = _glob_typelines()
    if fallback:
        return sorted(fallback)[0]
    raise SwError(
        "SOLIDWORKS type library (sldworks.tlb) was not found on this machine",
        code="no_typelib",
        hint=f"Is SOLIDWORKS installed? If it is, set {TLB_ENV} to its sldworks.tlb.",
    )


def default_roots():
    """Folders the server may write into when SWAGENT_OUTPUT_ROOTS is unset."""
    parent = os.path.dirname(_PROJECT_DIR)
    roots = [_PROJECT_DIR]
    if parent and parent != _PROJECT_DIR:
        roots.append(parent)
    return tuple(dict.fromkeys(roots))


def output_roots():
    """The write whitelist: the environment variable, or the defaults."""
    extra = os.environ.get(ROOTS_ENV)
    roots = list(default_roots())
    if extra:
        roots.extend(part for part in extra.split(os.pathsep) if part)
    return [os.path.abspath(root) for root in roots]
