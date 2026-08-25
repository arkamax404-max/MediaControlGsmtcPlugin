from __future__ import annotations

import base64
import re
import threading
import zlib
from dataclasses import dataclass


ARTWORK_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
PNG_DATA_URI_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 1_000_000
MAX_AGGREGATE_BYTES = 3_000_000
MAX_DIMENSION = 4096
MAX_PIXELS = 4_194_304


@dataclass(frozen=True)
class ArtworkBundle:
    artwork_id: str
    color: str
    grayscale: str
    tiles: tuple[str, str, str, str]


@dataclass(frozen=True)
class ArtworkFetchReservation:
    artwork_id: str
    epoch: int
    relevance: tuple[object, ...]


def _valid_png_uri(value: object) -> int | None:
    if not isinstance(value, str) or not value.startswith(PNG_DATA_URI_PREFIX):
        return None
    encoded = value[len(PNG_DATA_URI_PREFIX):]
    if not encoded or len(encoded) % 4 or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if not 1 <= len(data) <= MAX_IMAGE_BYTES:
        return None
    if base64.b64encode(data).decode("ascii") != encoded or not _valid_png(data):
        return None
    return len(data)


def _valid_png(data: bytes) -> bool:
    if not data.startswith(PNG_SIGNATURE):
        return False
    offset = len(PNG_SIGNATURE)
    chunk_index = 0
    idat_bytes = 0
    while offset < len(data):
        if len(data) - offset < 12:
            return False
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return False
        chunk_data = data[offset + 8:offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != crc:
            return False
        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(chunk_data[:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            if (not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION
                    or width * height > MAX_PIXELS
                    or chunk_data[8:] != bytes((8, 6, 0, 0, 0))):
                return False
        elif chunk_type == b"IDAT":
            idat_bytes += length
        elif chunk_type == b"IEND":
            return length == 0 and idat_bytes >= 1 and end == len(data)
        else:
            return False
        chunk_index += 1
        offset = end
    return False


def parse_artwork_bundle(payload: object, expected_id: str) -> ArtworkBundle | None:
    if (not isinstance(expected_id, str) or not ARTWORK_ID_PATTERN.fullmatch(expected_id)
            or not isinstance(payload, dict)
            or set(payload) != {"id", "color", "grayscale", "tiles"}
            or payload["id"] != expected_id
            or not isinstance(payload["tiles"], list)
            or len(payload["tiles"]) != 4):
        return None
    values = (payload["color"], payload["grayscale"], *payload["tiles"])
    total = 0
    for value in values:
        size = _valid_png_uri(value)
        if size is None or total + size > MAX_AGGREGATE_BYTES:
            return None
        total += size
    return ArtworkBundle(expected_id, values[0], values[1], tuple(values[2:]))


class ArtworkBundleCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expected_id: str | None = None
        self._bundle: ArtworkBundle | None = None
        self._epoch = 0
        self._closed = False

    def begin(self, artwork_id: str) -> ArtworkBundle | None:
        artwork_id = _canonical_artwork_id(artwork_id)
        if artwork_id is None:
            return None
        with self._lock:
            if self._closed:
                return None
            if artwork_id != self._expected_id:
                self._epoch += 1
                self._expected_id = artwork_id
                self._bundle = None
            return self._bundle

    def reserve(self, artwork_id: str,
                relevance: tuple[object, ...] = ()) -> ArtworkFetchReservation | None:
        artwork_id = _canonical_artwork_id(artwork_id)
        if artwork_id is None or not isinstance(relevance, tuple):
            return None
        with self._lock:
            if (self._closed or self._expected_id != artwork_id
                    or self._bundle is not None):
                return None
            self._epoch += 1
            return ArtworkFetchReservation(artwork_id, self._epoch, relevance)

    def install(self, reservation: ArtworkFetchReservation, bundle: ArtworkBundle) -> bool:
        if type(reservation) is not ArtworkFetchReservation or not isinstance(bundle, ArtworkBundle):
            return False
        artwork_id = reservation.artwork_id
        epoch = reservation.epoch
        if (type(artwork_id) is not str or not ARTWORK_ID_PATTERN.fullmatch(artwork_id)
                or type(epoch) is not int or isinstance(epoch, bool)
                or bundle.artwork_id != artwork_id):
            return False
        with self._lock:
            if (self._closed or self._expected_id != artwork_id
                    or self._epoch != epoch):
                return False
            self._bundle = bundle
            return True

    def fetch(self, artwork_id: str, client, cancelled=None) -> ArtworkBundle | None:
        artwork_id = _canonical_artwork_id(artwork_id)
        if artwork_id is None:
            return None
        cached = self.begin(artwork_id)
        if cached is not None:
            return cached
        reservation = self.reserve(artwork_id)
        if reservation is None:
            return None
        result = client.get_artwork(artwork_id, cancelled=cancelled)
        if not getattr(result, "ok", False) or not self.install(reservation, result.bundle):
            return None
        return result.bundle

    def get(self, artwork_id: str) -> ArtworkBundle | None:
        artwork_id = _canonical_artwork_id(artwork_id)
        if artwork_id is None:
            return None
        with self._lock:
            return self._bundle if self._expected_id == artwork_id else None

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1
            self._expected_id = None
            self._bundle = None

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1
            self._bundle = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._epoch += 1
            self._expected_id = None
            self._bundle = None


def _canonical_artwork_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        value = str.__str__(value)
    except Exception:
        return None
    return value if ARTWORK_ID_PATTERN.fullmatch(value) else None
