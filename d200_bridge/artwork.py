import base64
import hashlib
import io
import threading
import warnings
from collections import OrderedDict
from dataclasses import dataclass

try:
    from PIL import Image, ImageOps
except ImportError:  # Allows non-artwork bridge tests to run before dependencies are installed.
    Image = None
    ImageOps = None


MAX_SOURCE_BYTES = 1_000_000
MAX_DECODED_PIXELS = 16_000_000
MAX_ENCODED_BYTES = 1_000_000
ARTWORK_CACHE_SIZE = 8
SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "GIF", "WEBP"})


@dataclass(frozen=True)
class ArtworkVariants:
    color: str
    grayscale: str


def _magic_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def _png_data_uri(data):
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


class ArtworkProcessor:
    def __init__(
        self,
        cache_size=ARTWORK_CACHE_SIZE,
        max_source_bytes=MAX_SOURCE_BYTES,
        max_decoded_pixels=MAX_DECODED_PIXELS,
        max_encoded_bytes=MAX_ENCODED_BYTES,
    ):
        self._cache_size = max(1, int(cache_size))
        self._max_source_bytes = max_source_bytes
        self._max_decoded_pixels = max_decoded_pixels
        self._max_encoded_bytes = max_encoded_bytes
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def process(self, data):
        if not isinstance(data, bytes) or not data or len(data) > self._max_source_bytes:
            return None
        digest = hashlib.sha256(data).digest()
        with self._lock:
            if digest in self._cache:
                result = self._cache.pop(digest)
                self._cache[digest] = result
                return result
            result = self._decode_encode(data)
            if not isinstance(result, ArtworkVariants):
                return None
            self._cache[digest] = result
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return result

    def _decode_encode(self, data):
        if Image is None:
            return None
        magic_format = _magic_format(data)
        if magic_format not in SUPPORTED_FORMATS:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as probe:
                    if probe.format != magic_format or probe.format not in SUPPORTED_FORMATS:
                        return None
                    width, height = probe.size
                    if width <= 0 or height <= 0 or width * height > self._max_decoded_pixels:
                        return None
                    probe.verify()

                with Image.open(io.BytesIO(data)) as source:
                    if source.format != magic_format:
                        return None
                    source.seek(0)
                    source = ImageOps.exif_transpose(source)
                    source.load()
                    if source.width <= 0 or source.height <= 0:
                        return None
                    if source.width * source.height > self._max_decoded_pixels:
                        return None
                    color = source.convert("RGBA")
                    luminance = color.convert("RGB").convert("L")
                    grayscale = Image.merge("RGBA", (luminance, luminance, luminance, color.getchannel("A")))
                    return ArtworkVariants(
                        color=self._encode_png(color),
                        grayscale=self._encode_png(grayscale),
                    )
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError):
            return None

    def _encode_png(self, image):
        output = io.BytesIO()
        image.save(output, format="PNG")
        data = output.getvalue()
        if not data or len(data) > self._max_encoded_bytes:
            raise ValueError("Encoded artwork exceeds limit")
        return _png_data_uri(data)

    @property
    def cached_entries(self):
        with self._lock:
            return len(self._cache)


artwork_processor = ArtworkProcessor()
