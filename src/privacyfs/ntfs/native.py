"""Windows metadata APIs only: no raw MFT sectors, file bodies, or journal writes."""
from dataclasses import dataclass, field
import os
import struct

from ..scanner import winlong

DIRECTORY = 0x10
REPARSE = 0x400
# Names, links, reparse boundaries and access rights. A timestamp-only/basic
# attribute change does not alter this filename inventory.
STRUCTURAL_REASONS = 0x100 | 0x200 | 0x1000 | 0x2000 | 0x10000 | 0x100000 | 0x800


class NtfsUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NtfsCancelled(Exception):
    pass


@dataclass(frozen=True)
class Record:
    file_id: int
    parent_id: int
    attributes: int
    usn: int
    name: str = field(repr=False)
    reason: int = 0


@dataclass(frozen=True)
class Journal:
    identifier: int
    first_usn: int
    next_usn: int


def valid_name(name):
    if not name or name == ".." or any(c in name for c in ("\0", "/", "\\", ":")):
        raise NtfsUnavailable("invalid_metadata")
    return name


def parse_usn_records(data):
    cursor = 0
    while cursor < len(data):
        if len(data)-cursor < 60:
            raise NtfsUnavailable("invalid_metadata")
        size, major, minor, fid, parent, usn, timestamp, reason, source, security, attrs, length, offset = struct.unpack_from(
            "<IHHQQqqIIIIHH", data, cursor)
        if major != 2:
            raise NtfsUnavailable("record_version_unsupported")
        if size < 60 or size % 8 or cursor+size > len(data) or offset < 60 or length % 2 or offset+length > size:
            raise NtfsUnavailable("invalid_metadata")
        name = data[cursor+offset:cursor+offset+length].decode("utf-16-le", "surrogatepass")
        yield Record(fid, parent, attrs, usn, valid_name(name), reason)
        cursor += size


def parse_links(data):
    if len(data) < 8:
        raise NtfsUnavailable("invalid_links")
    needed, count = struct.unpack_from("<II", data)
    if not 0 < needed <= len(data) or not 1 <= count <= 65536:
        raise NtfsUnavailable("invalid_links")
    result, cursor = [], 8
    for i in range(count):
        if cursor + 20 > len(data):
            raise NtfsUnavailable("invalid_links")
        next_offset = struct.unpack_from("<I", data, cursor)[0]
        parent, length = struct.unpack_from("<QI", data, cursor+8)
        end = cursor+20+length*2
        if not length or end > len(data):
            raise NtfsUnavailable("invalid_links")
        name = valid_name(data[cursor+20:end].decode("utf-16-le", "surrogatepass"))
        if name == ".":
            raise NtfsUnavailable("invalid_links")
        result.append((parent, name))
        if i+1 < count:
            if not next_offset or next_offset % 8 or cursor+next_offset < end:
                raise NtfsUnavailable("invalid_links")
            cursor += next_offset
        elif next_offset:
            raise NtfsUnavailable("invalid_links")
    return result


class WindowsAPI:
    def __init__(self):
        if os.name != "nt":
            raise NtfsUnavailable("not_windows")
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.ntdll = c.WinDLL("ntdll")
        class Info(c.Structure):
            _fields_ = [("attributes",c.c_uint32),("times",c.c_uint32*6),("serial",c.c_uint32),
                        ("size_hi",c.c_uint32),("size_lo",c.c_uint32),("links",c.c_uint32),
                        ("id_hi",c.c_uint32),("id_lo",c.c_uint32)]
        class IdUnion(c.Union):
            _fields_ = [("file_id",c.c_uint64),("extended",c.c_ubyte*16)]
        class IdDescriptor(c.Structure):
            _fields_ = [("size",c.c_uint32),("type",c.c_uint32),("identifier",IdUnion)]
        class IoStatus(c.Structure):
            _fields_ = [("status",c.c_void_p),("information",c.c_size_t)]
        self.Info, self.IdDescriptor, self.IoStatus = Info, IdDescriptor, IoStatus
        self.invalid = c.c_void_p(-1).value
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.GetFileInformationByHandle.argtypes = [w.HANDLE,c.POINTER(Info)]
        self.kernel.GetFileInformationByHandle.restype = w.BOOL
        self.kernel.OpenFileById.argtypes = [w.HANDLE,c.POINTER(IdDescriptor),w.DWORD,w.DWORD,c.c_void_p,w.DWORD]
        self.kernel.OpenFileById.restype = w.HANDLE
        self.kernel.DeviceIoControl.argtypes = [w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
        self.kernel.DeviceIoControl.restype = w.BOOL
        self.kernel.GetVolumeInformationW.argtypes = [w.LPCWSTR,w.LPWSTR,w.DWORD,c.POINTER(w.DWORD),c.POINTER(w.DWORD),c.POINTER(w.DWORD),w.LPWSTR,w.DWORD]
        self.kernel.GetVolumeInformationW.restype = w.BOOL
        self.kernel.GetVolumeNameForVolumeMountPointW.argtypes = [w.LPCWSTR,w.LPWSTR,w.DWORD]
        self.kernel.GetVolumeNameForVolumeMountPointW.restype = w.BOOL
        self.ntdll.NtQueryInformationFile.argtypes = [w.HANDLE,c.POINTER(IoStatus),c.c_void_p,w.ULONG,c.c_int]
        self.ntdll.NtQueryInformationFile.restype = c.c_int32

    def error(self, code=None):
        code = self.c.get_last_error() if code is None else code
        raise NtfsUnavailable({5:"access_denied",1:"unsupported_volume",50:"unsupported_volume",
                               1178:"journal_unavailable",1179:"journal_unavailable",1181:"journal_gap"}.get(code,"metadata_io_failed"))

    def open_path(self, path, access=0x80, volume=False):
        handle = self.kernel.CreateFileW(str(path) if volume else winlong(path), access, 7, None, 3,
                                         0 if volume else 0x02000000 | 0x00200000, None)
        if handle == self.invalid:
            self.error()
        return handle

    def close(self, handle):
        if handle is not None and handle != self.invalid:
            self.kernel.CloseHandle(handle)

    def info(self, handle):
        value = self.Info()
        if not self.kernel.GetFileInformationByHandle(handle, self.c.byref(value)):
            self.error()
        return value, (value.id_hi << 32) | value.id_lo

    def open_id(self, hint, identifier):
        desc = self.IdDescriptor()
        desc.size, desc.type, desc.identifier.file_id = self.c.sizeof(desc), 0, identifier
        handle = self.kernel.OpenFileById(hint, self.c.byref(desc), 0x80, 7, None, 0x02000000 | 0x00200000)
        if handle == self.invalid:
            error = self.c.get_last_error()
            if error in (2,3,1168):
                return None
            self.error(error)
        return handle

    def ioctl(self, handle, code, request=b"", size=1024*1024, eof=False):
        incoming = self.c.create_string_buffer(request) if request else None
        output, used = self.c.create_string_buffer(size), self.w.DWORD()
        if not self.kernel.DeviceIoControl(handle, code, incoming, len(request), output, size, self.c.byref(used), None):
            error = self.c.get_last_error()
            if eof and error == 38:
                return None
            self.error(error)
        if used.value > size:
            raise NtfsUnavailable("invalid_metadata")
        return output.raw[:used.value]

    def links(self, handle):
        size = 65536
        while size <= 2*1024*1024:
            data, io = self.c.create_string_buffer(size), self.IoStatus()
            status = self.ntdll.NtQueryInformationFile(handle, self.c.byref(io), data, size, 46) & 0xffffffff
            if status == 0:
                used = int(io.information)
                if not 8 <= used <= size:
                    raise NtfsUnavailable("invalid_links")
                return parse_links(data.raw[:used])
            if status not in (0x80000005, 0xc0000004, 0xc0000023):
                raise NtfsUnavailable("hardlinks_unavailable")
            needed = struct.unpack_from("<I",data.raw)[0]
            size = max(size*2, needed)
        raise NtfsUnavailable("hardlinks_limit")


class NativeVolume:
    def __init__(self, root):
        self.api = WindowsAPI()
        self.root, self.volume, self.root_handle = root, None, None
        try:
            fs = self.api.c.create_unicode_buffer(32)
            serial, limit, flags = self.api.w.DWORD(), self.api.w.DWORD(), self.api.w.DWORD()
            if not self.api.kernel.GetVolumeInformationW(str(root),None,0,self.api.c.byref(serial),self.api.c.byref(limit),self.api.c.byref(flags),fs,32):
                self.api.error()
            if fs.value.upper() != "NTFS":
                raise NtfsUnavailable("not_ntfs")
            self.serial = serial.value
            guid = self.api.c.create_unicode_buffer(64)
            if not self.api.kernel.GetVolumeNameForVolumeMountPointW(str(root),guid,64):
                raise NtfsUnavailable("scope_not_volume")
            self.volume = self.api.open_path(guid.value.rstrip("\\"), 0x80000000, volume=True)
            self.root_handle = self.api.open_path(root)
            info, self.root_id = self.api.info(self.root_handle)
            if info.serial != self.serial or info.attributes & REPARSE:
                raise NtfsUnavailable("volume_changed")
            canonical = self.api.open_path(guid.value)
            try:
                canonical_info, canonical_id = self.api.info(canonical)
                if canonical_id != self.root_id or canonical_info.serial != self.serial:
                    raise NtfsUnavailable("scope_not_volume")
            finally:
                self.api.close(canonical)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.api.close(self.root_handle)
        self.api.close(self.volume)
        self.root_handle = self.volume = None

    def journal(self):
        data = self.api.ioctl(self.volume,0x000900f4,size=256)
        if len(data) < 56:
            raise NtfsUnavailable("invalid_journal")
        identifier, first, next_usn, lowest = struct.unpack_from("<Qqqq",data)
        return Journal(identifier,max(first,lowest),next_usn)

    def records(self, cancel):
        cursor = 0
        while not cancel.is_set():
            data = self.api.ioctl(self.volume,0x000900b3,struct.pack("<Qqq",cursor,0,0x7fffffffffffffff),eof=True)
            if data is None:
                return
            if len(data) < 8:
                raise NtfsUnavailable("invalid_metadata")
            next_cursor = struct.unpack_from("<Q",data)[0]
            if next_cursor <= cursor:
                raise NtfsUnavailable("invalid_cursor")
            yield from parse_usn_records(data[8:])
            cursor = next_cursor
        raise NtfsCancelled()

    def changes(self, start, end, cancel):
        if start.identifier != end.identifier or start.next_usn < end.first_usn:
            raise NtfsUnavailable("journal_gap")
        cursor = start.next_usn
        while cursor < end.next_usn:
            if cancel.is_set():
                raise NtfsCancelled()
            request = struct.pack("<qIIQQQ",cursor,0xffffffff,0,0,0,start.identifier)
            data = self.api.ioctl(self.volume,0x000900bb,request)
            if len(data) < 8:
                raise NtfsUnavailable("invalid_journal")
            next_cursor = struct.unpack_from("<q",data)[0]
            if next_cursor <= cursor:
                raise NtfsUnavailable("journal_gap")
            for record in parse_usn_records(data[8:]):
                if cursor <= record.usn < end.next_usn:
                    yield record
            cursor = next_cursor

    def current_record(self, identifier):
        handle = self.api.open_id(self.root_handle, identifier)
        if handle is None:
            return None
        try:
            records = list(parse_usn_records(self.api.ioctl(handle,0x000900eb,struct.pack("<HH",2,2),size=65536)))
            if len(records) != 1 or records[0].file_id != identifier:
                raise NtfsUnavailable("stale_identity")
            return records[0]
        finally:
            self.api.close(handle)

    def directory_identity(self, path):
        """Resolve only the user's selected directory, not every directory."""
        handle = self.api.open_path(path)
        try:
            info, identifier = self.api.info(handle)
            if info.serial != self.serial or not info.attributes & DIRECTORY or info.attributes & REPARSE:
                raise NtfsUnavailable("scope_changed")
            return identifier
        finally:
            self.api.close(handle)

    def gate_directory(self, path, identifier):
        handle = None
        try:
            handle = self.api.open_path(path)
            info, actual = self.api.info(handle)
            if info.serial != self.serial or actual != identifier:
                raise NtfsUnavailable("stale_identity")
            if info.attributes & REPARSE:
                return "reparse"
            # A volume handle is not proof of ordinary directory-list access.
            with os.scandir(winlong(path)) as probe:
                next(probe, None)
            return "ok"
        except PermissionError:
            return "denied"
        except NtfsUnavailable as error:
            if error.code == "access_denied":
                return "denied"
            raise
        except OSError:
            raise NtfsUnavailable("directory_changed") from None
        finally:
            self.api.close(handle)

    def file_links(self, identifier):
        handle = self.api.open_id(self.root_handle, identifier)
        if handle is None:
            return None
        try:
            info, actual = self.api.info(handle)
            if info.serial != self.serial or actual != identifier:
                raise NtfsUnavailable("stale_identity")
            if info.attributes & REPARSE:
                return info.attributes, []
            links = self.api.links(handle)
            if len(links) != info.links or len(set(links)) != len(links):
                raise NtfsUnavailable("hardlink_count_changed")
            return info.attributes, links
        finally:
            self.api.close(handle)
