"""Enumerate display UUIDs without querying OBS's unsafe string property list."""

import ctypes
from functools import lru_cache
from uuid import UUID


class _UUIDBytes(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint8 * 16)]


@lru_cache(maxsize=1)
def _uuid_functions():
    colorsync = ctypes.CDLL("/System/Library/Frameworks/ColorSync.framework/ColorSync")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    create = colorsync.CGDisplayCreateUUIDFromDisplayID
    create.argtypes = [ctypes.c_uint32]
    create.restype = ctypes.c_void_p
    get_bytes = foundation.CFUUIDGetUUIDBytes
    get_bytes.argtypes = [ctypes.c_void_p]
    get_bytes.restype = _UUIDBytes
    release = foundation.CFRelease
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    return create, get_bytes, release


def display_uuid(display_id: int) -> str | None:
    create, get_bytes, release = _uuid_functions()
    value = create(display_id)
    if not value:
        return None
    try:
        return str(UUID(bytes=bytes(get_bytes(value).value))).upper()
    finally:
        release(value)


def active_displays() -> list[tuple[str, str]]:
    import Quartz

    error, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if error != 0:
        raise RuntimeError(f"macOS display enumeration failed: {error}")
    main = Quartz.CGMainDisplayID()
    displays = []
    for index, display_id in enumerate(ids[:count], start=1):
        value = display_uuid(display_id)
        if value:
            width = Quartz.CGDisplayPixelsWide(display_id)
            height = Quartz.CGDisplayPixelsHigh(display_id)
            primary = " (main)" if display_id == main else ""
            displays.append((value, f"Display {index} — {width}×{height}{primary}"))
    return displays
