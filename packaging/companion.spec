import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs


root = Path(SPEC).resolve().parents[1]
metadata_root = Path(os.environ["GSMTC_BUILD_METADATA"])
entry = root / "packaging" / "companion_entry.py"
optional_excludes = ["ssl", "_ssl", "_hashlib", "pyexpat", "_elementtree",
    "xml.parsers.expat", "lzma", "_lzma", "compression.zstd",
    "compression.zstd._zstdfile", "_zstd"]

hiddenimports = [
    "PIL.Image", "PIL.ImageOps", "PIL.PngImagePlugin", "PIL.JpegImagePlugin",
    "PIL.GifImagePlugin", "PIL.WebPImagePlugin", "PIL._imaging", "PIL._webp",
    "comtypes", "comtypes.client", "comtypes.gen",
    "psutil._psutil_windows", "pycaw.pycaw", "winrt.runtime",
    "winrt.windows.foundation", "winrt.windows.foundation.collections",
    "winrt.windows.media.control", "winrt.windows.storage.streams", "winrt.windows.system",
]
binaries = collect_dynamic_libs("psutil") + collect_dynamic_libs("winrt")
datas = [
    (str(root / "LICENSE"), "."),
    (str(root / "THIRD_PARTY_NOTICES.md"), "."),
    (str(metadata_root / "build-dependencies.json"), "."),
    (str(metadata_root / "third-party-notices.json"), "."),
    (str(metadata_root / "licenses"), "licenses"),
]

a = Analysis(
    [str(entry)], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tests", "test", "comtypes.test", "plugin", "node_modules", "openspec"] + optional_excludes,
    noarchive=False, optimize=0,
)
unused_pillow = ("PIL/_avif", "PIL/_imagingcms", "PIL/_imagingtk")
a.pure = [item for item in a.pure if item[0] not in optional_excludes]
a.binaries = [item for item in a.binaries
              if not any(marker in item[0].replace("\\", "/") for marker in unused_pillow)
              and Path(item[0]).name.lower() not in {"_ssl.pyd", "_hashlib.pyd", "pyexpat.pyd",
                  "_elementtree.pyd", "_lzma.pyd", "_zstd.pyd", "libssl-3.dll", "libcrypto-3.dll"}]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="GSMTCD200Companion",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False,
    name="GSMTCD200Companion",
)
