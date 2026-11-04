from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import zlib

from .rpyc_loader import RestrictedRenPyUnpickler, RpycError


class RpaError(Exception):
    pass

MAX_RPA_INDEX_BYTES = 64 * 1024 * 1024
MAX_RPA_SCRIPT_BYTES = 256 * 1024 * 1024


def _limited_zlib_decompress(data: bytes, limit: int) -> bytes:
    dec = zlib.decompressobj()
    out = dec.decompress(data, limit + 1)
    if len(out) > limit or dec.unconsumed_tail or not dec.eof:
        raise RpaError(f"Compressed RPA data exceeds safety limit ({limit} bytes)")
    return out


@dataclass(frozen=True)
class RpaEntry:
    name: str
    parts: tuple[tuple[int, int, bytes], ...]


@dataclass(frozen=True)
class RpaArchive:
    path: Path
    version: str
    entries: dict[str, RpaEntry]

    def read(self, name: str) -> bytes:
        entry = self.entries[name]
        out = bytearray()
        file_size = self.path.stat().st_size
        with self.path.open("rb") as f:
            for offset, length, prefix in entry.parts:
                if offset < 0 or length < 0 or offset + length > file_size:
                    raise RpaError(f"RPA entry points outside archive: {name}")
                if len(out) + len(prefix) + length > MAX_RPA_SCRIPT_BYTES:
                    raise RpaError(f"RPA entry exceeds safety limit: {name}")
                out.extend(prefix)
                f.seek(offset)
                chunk = f.read(length)
                if len(chunk) != length:
                    raise RpaError(f"Truncated RPA entry: {name}")
                out.extend(chunk)
        return bytes(out)


def _safe_index_loads(data: bytes):
    for encoding in ("ASCII", "latin1"):
        try:
            return RestrictedRenPyUnpickler(BytesIO(data), fix_imports=False, encoding=encoding, errors="strict").load()
        except UnicodeDecodeError:
            continue
    raise RpaError("Unable to decode RPA index pickle")


def _name(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.decode("latin1")
    return str(v)


def open_rpa(path: str | Path) -> RpaArchive:
    path = Path(path)
    with path.open("rb") as f:
        header = f.read(64)
        if header.startswith(b"RPA-3.0 "):
            line = header.split(b"\n", 1)[0]
            parts = line.split()
            if len(parts) < 3:
                raise RpaError("Invalid RPA-3.0 header")
            index_offset = int(parts[1], 16)
            key = int(parts[2], 16)
            version = "RPA-3.0"
        elif header.startswith(b"RPA-2.0 "):
            line = header[:24]
            parts = line.split()
            if len(parts) < 2:
                raise RpaError("Invalid RPA-2.0 header")
            index_offset = int(parts[1], 16)
            key = 0
            version = "RPA-2.0"
        else:
            magic = header[:16].decode("ascii", errors="replace")
            raise RpaError(f"Unsupported RPA format: {magic!r}")

        f.seek(index_offset)
        packed_index = f.read()

    try:
        index = _safe_index_loads(_limited_zlib_decompress(packed_index, MAX_RPA_INDEX_BYTES))
    except Exception as e:
        if isinstance(e, RpaError):
            raise
        raise RpaError(f"Unable to read RPA index: {e}") from e
    if not isinstance(index, dict):
        raise RpaError("RPA index is not a dictionary")

    entries = {}
    for raw_name, raw_parts in index.items():
        name = _name(raw_name).replace("\\", "/")
        decoded = []
        if not isinstance(raw_parts, (list, tuple)):
            continue
        for part in raw_parts:
            if not isinstance(part, (list, tuple)) or len(part) < 2:
                continue
            offset = int(part[0]) ^ key
            length = int(part[1]) ^ key
            prefix = part[2] if len(part) >= 3 and isinstance(part[2], bytes) else b""
            decoded.append((offset, length, prefix))
        if decoded:
            entries[name] = RpaEntry(name, tuple(decoded))
    return RpaArchive(path=path, version=version, entries=entries)


def script_entries(archive: RpaArchive):
    for name in sorted(archive.entries):
        lower = name.lower()
        if lower.endswith((".rpy", ".rpym", ".rpyc", ".rpymc")):
            yield archive.entries[name]
