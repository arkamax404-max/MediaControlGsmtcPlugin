import os

if os.environ.get("GSMTC_PACKAGING_IMPORT_PROBE") == "1":
    import hashlib, hmac, importlib.util, io, zipfile
    import comtypes, psutil
    from PIL import Image, ImageOps, GifImagePlugin, JpegImagePlugin, PngImagePlugin, WebPImagePlugin
    from pycaw import pycaw
    from winrt.windows.foundation import IAsyncOperation
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    from winrt.windows.storage.streams import DataReader
    excluded = ("ssl", "_ssl", "_hashlib", "pyexpat", "_elementtree", "xml.parsers.expat",
        "lzma", "_lzma", "compression.zstd", "compression.zstd._zstdfile", "_zstd")
    def available(name):
        try: return importlib.util.find_spec(name) is not None
        except ModuleNotFoundError: return False
    if any(available(name) for name in excluded): raise RuntimeError("optional module present")
    if hashlib.sha256(b"abc").hexdigest() != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad": raise RuntimeError("sha256 fallback failed")
    if not hmac.compare_digest(b"same", b"same"): raise RuntimeError("compare_digest failed")
    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", zipfile.ZIP_DEFLATED) as archive: archive.writestr("value", b"deflate")
    with zipfile.ZipFile(io.BytesIO(compressed.getvalue())) as archive:
        if archive.read("value") != b"deflate": raise RuntimeError("DEFLATE failed")
    for image_format in ("PNG", "JPEG", "GIF", "WEBP"):
        encoded = io.BytesIO(); Image.new("RGB", (2, 2), "red").save(encoded, image_format)
        with Image.open(io.BytesIO(encoded.getvalue())) as image: image.load()
    raise SystemExit(0)

if os.environ.get("GSMTC_DIAGNOSTICS_FORCE_OFFLINE") == "1":
    from d200_bridge import diagnostics
    diagnostics.bounded_http_get = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError())

from d200_bridge.__main__ import main

raise SystemExit(main())
