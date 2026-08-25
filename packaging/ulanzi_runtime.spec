import sys
from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPEC).resolve().parents[1]
plugin = root / "com.arkamax404.mediacontrold200.ulanziPlugin"
entry = plugin / "runtime" / "python" / "mediacontrol_runtime.py"
hiddenimports = collect_submodules("ulanzi_api") + ["websocket"]


def distribution_file(name, suffix):
    package = distribution(name)
    matches = [item for item in package.files or []
               if str(item).replace("\\", "/").endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {name} license matching {suffix}")
    return Path(package.locate_file(matches[0]))


datas = [
    (str(root / "LICENSE"), "licenses/project"),
    (str(root / "THIRD_PARTY_NOTICES.md"), "licenses/project"),
    (str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/cpython"),
    (str(distribution_file("pyinstaller", "/licenses/COPYING.txt")), "licenses/pyinstaller"),
    (str(distribution_file("ulanzistudio-plugin-sdk-python", "/licenses/LICENSE")),
     "licenses/plugin-common-python"),
    (str(distribution_file("websocket-client", ".dist-info/LICENSE")),
     "licenses/websocket-client"),
]

a = Analysis(
    [str(entry)],
    pathex=[str(root), str(entry.parent)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "test", "websocket.tests", "PIL", "pycaw", "winrt"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaControlRuntime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="runtime",
)
