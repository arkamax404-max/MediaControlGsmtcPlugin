import base64
import hashlib
import io
import json
import re
import threading
import warnings
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace

try:
    from PIL import Image, ImageOps
except ImportError:  # Allows non-artwork bridge tests to run before dependencies are installed.
    Image = None
    ImageOps = None


MAX_SOURCE_BYTES = 1_000_000
MAX_DECODED_PIXELS = 4_194_304
MAX_IMAGE_DIMENSION = 4096
MAX_ENCODED_BYTES = 1_000_000
MAX_AGGREGATE_ENCODED_BYTES = 3_000_000
MAX_BUNDLE_BYTES = 4_001_000
ARTWORK_CACHE_SIZE = 8
MOSAIC_SIZE = 392
MOSAIC_TILE_SIZE = 196
SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "GIF", "WEBP"})
ARTWORK_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_DATA_URI_PREFIX = "data:image/png;base64,"


@dataclass(frozen=True)
class ArtworkVariants:
    color: str
    grayscale: str
    mosaic_tiles: tuple[str, ...] = ()
    artwork_id: str = ""

    def public(self):
        return {
            "id": self.artwork_id,
            "color": self.color,
            "grayscale": self.grayscale,
            "tiles": list(self.mosaic_tiles),
        }


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
    return f"{PNG_DATA_URI_PREFIX}{base64.b64encode(data).decode('ascii')}"


def validate_png_data_uri(value, max_encoded_bytes=MAX_ENCODED_BYTES):
    if not isinstance(value, str) or not value.startswith(PNG_DATA_URI_PREFIX):
        return None
    encoded = value[len(PNG_DATA_URI_PREFIX):]
    if not encoded or len(encoded) % 4 or len(encoded) > 4 * ((max_encoded_bytes + 2) // 3):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if not data or len(data) > max_encoded_bytes:
        return None
    if base64.b64encode(data).decode("ascii") != encoded or not _valid_bridge_png(data):
        return None
    return value


def _valid_bridge_png(data):
    chunks = _png_chunks(data)
    if chunks is None:
        return False
    if chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        return False
    header = chunks[0][1]
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    if width <= 0 or height <= 0:
        return False
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        return False
    if width * height > MAX_DECODED_PIXELS or header[8:] != bytes((8, 6, 0, 0, 0)):
        return False
    idat_bytes = 0
    saw_iend = False
    for chunk_type, chunk_data in chunks[1:]:
        if chunk_type == b"IDAT" and not saw_iend:
            idat_bytes += len(chunk_data)
        elif chunk_type == b"IEND" and not chunk_data and idat_bytes > 0 and not saw_iend:
            saw_iend = True
        else:
            return False
    return idat_bytes > 0 and saw_iend


def _png_chunks(data):
    if not data.startswith(PNG_SIGNATURE):
        return None
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        if len(data) - offset < 12:
            return None
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return None
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length:chunk_end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            return None
        chunks.append((chunk_type, chunk_data))
        if chunk_type == b"IEND":
            if chunk_end != len(data):
                return None
            return chunks
        offset = chunk_end
    return None


def _valid_png_source(data):
    chunks = _png_chunks(data)
    if chunks is None or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        return False
    if chunks[-1] != (b"IEND", b""):
        return False
    return sum(len(chunk_data) for chunk_type, chunk_data in chunks if chunk_type == b"IDAT") > 0


def _valid_variants(variants):
    values = (variants.color, variants.grayscale, *variants.mosaic_tiles)
    if not ARTWORK_ID_PATTERN.fullmatch(variants.artwork_id):
        return False
    if len(variants.mosaic_tiles) != 4 or not all(validate_png_data_uri(value) for value in values):
        return False
    body = json.dumps(variants.public(), separators=(",", ":")).encode("utf-8")
    return len(body) <= MAX_BUNDLE_BYTES


class ArtworkProcessor:
    def __init__(
        self,
        cache_size=ARTWORK_CACHE_SIZE,
        max_source_bytes=MAX_SOURCE_BYTES,
        max_decoded_pixels=MAX_DECODED_PIXELS,
        max_encoded_bytes=MAX_ENCODED_BYTES,
        max_aggregate_encoded_bytes=MAX_AGGREGATE_ENCODED_BYTES,
    ):
        self._cache_size = max(1, int(cache_size))
        self._max_source_bytes = max_source_bytes
        self._max_decoded_pixels = max_decoded_pixels
        self._max_encoded_bytes = max_encoded_bytes
        self._max_aggregate_encoded_bytes = max_aggregate_encoded_bytes
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def process(self, data):
        if not isinstance(data, bytes) or not data or len(data) > self._max_source_bytes:
            return None
        digest = hashlib.sha256(data).digest()
        artwork_id = digest.hex()
        with self._lock:
            if digest in self._cache:
                result = self._cache.pop(digest)
                self._cache[digest] = result
                return result
            result = self._decode_encode(data)
            if not isinstance(result, ArtworkVariants):
                return None
            result = replace(result, artwork_id=artwork_id)
            if not _valid_variants(result):
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
        if magic_format == "PNG" and not _valid_png_source(data):
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as opened:
                    if opened.format != magic_format or opened.format not in SUPPORTED_FORMATS:
                        return None
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > self._max_decoded_pixels:
                        return None
                    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                        return None
                    opened.seek(0)
                    source = ImageOps.exif_transpose(opened)
                    source.load()
                    if source.width <= 0 or source.height <= 0:
                        return None
                    if source.width * source.height > self._max_decoded_pixels:
                        return None
                    color = source.convert("RGBA")
                    luminance = color.convert("RGB").convert("L")
                    grayscale = Image.merge("RGBA", (luminance, luminance, luminance, color.getchannel("A")))
                    contained = ImageOps.contain(
                        color,
                        (MOSAIC_SIZE, MOSAIC_SIZE),
                        method=Image.Resampling.LANCZOS,
                    )
                    mosaic = Image.new("RGBA", (MOSAIC_SIZE, MOSAIC_SIZE), (0, 0, 0, 0))
                    mosaic.paste(
                        contained,
                        (
                            (MOSAIC_SIZE - contained.width) // 2,
                            (MOSAIC_SIZE - contained.height) // 2,
                        ),
                    )
                    tiles = tuple(
                        mosaic.crop((left, top, left + MOSAIC_TILE_SIZE, top + MOSAIC_TILE_SIZE))
                        for left, top in (
                            (0, 0),
                            (MOSAIC_TILE_SIZE, 0),
                            (0, MOSAIC_TILE_SIZE),
                            (MOSAIC_TILE_SIZE, MOSAIC_TILE_SIZE),
                        )
                    )
                    encoded = [
                        self._encode_png(color),
                        self._encode_png(grayscale),
                        *(self._encode_png(tile) for tile in tiles),
                    ]
                    if sum(len(data) for data in encoded) > self._max_aggregate_encoded_bytes:
                        raise ValueError("Encoded artwork variants exceed aggregate limit")
                    return ArtworkVariants(
                        color=_png_data_uri(encoded[0]),
                        grayscale=_png_data_uri(encoded[1]),
                        mosaic_tiles=tuple(_png_data_uri(data) for data in encoded[2:]),
                    )
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError):
            return None

    def _encode_png(self, image):
        output = io.BytesIO()
        image.save(output, format="PNG")
        data = output.getvalue()
        if not data or len(data) > self._max_encoded_bytes:
            raise ValueError("Encoded artwork exceeds limit")
        return data

    @property
    def cached_entries(self):
        with self._lock:
            return len(self._cache)

    def get_cached(self, artwork_id):
        if not isinstance(artwork_id, str) or not ARTWORK_ID_PATTERN.fullmatch(artwork_id):
            return None
        with self._lock:
            return self._cache.get(bytes.fromhex(artwork_id))


artwork_processor = ArtworkProcessor()
