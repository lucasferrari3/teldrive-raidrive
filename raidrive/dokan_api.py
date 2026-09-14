"""ctypes bindings for Dokany 2.x (dokan2.dll).

Requires Dokany installed: https://github.com/dokan-dev/dokany
  winget install dokan-dev.Dokany
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import (
    POINTER,
    Structure,
    c_char,
    c_long,
    c_longlong,
    c_ubyte,
    c_ulong,
    c_ulonglong,
    c_ushort,
    c_void_p,
    c_wchar_p,
    sizeof,
)
from ctypes.wintypes import BOOL, DWORD, LPCWSTR, LPVOID, LPWSTR

if sys.platform != "win32":
    raise ImportError("Dokany bindings are only available on Windows")

# NTSTATUS is LONG (signed) on Windows.
NTSTATUS = c_long
STATUS_SUCCESS = 0x00000000
STATUS_NOT_IMPLEMENTED = 0xC0000002
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_END_OF_FILE = 0xC0000011
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_MEDIA_WRITE_PROTECTED = 0xC00000A2
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_NOT_A_DIRECTORY = 0xC0000103
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101

# Match installed dokan2.dll feature level (2.3.1 => 231).
DOKAN_VERSION = 231
DOKAN_OPTION_DEBUG = 1
DOKAN_OPTION_STDERR = 1 << 1
DOKAN_OPTION_WRITE_PROTECT = 1 << 3
DOKAN_OPTION_NETWORK = 1 << 4
DOKAN_OPTION_REMOVABLE = 1 << 5
DOKAN_OPTION_MOUNT_MANAGER = 1 << 6
DOKAN_OPTION_CURRENT_SESSION = 1 << 7
DOKAN_OPTION_CASE_SENSITIVE = 1 << 9
DOKAN_OPTION_ALLOW_IPC_BATCHING = 1 << 12

DOKAN_SUCCESS = 0
DOKAN_ERROR = -1
DOKAN_DRIVE_LETTER_ERROR = -2
DOKAN_DRIVER_INSTALL_ERROR = -3
DOKAN_START_ERROR = -4
DOKAN_MOUNT_ERROR = -5
DOKAN_MOUNT_POINT_ERROR = -6
DOKAN_VERSION_ERROR = -7

FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_ARCHIVE = 0x00000020

FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_DELETE_ON_CLOSE = 0x00001000

FILE_SUPERSEDE = 0
FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_OVERWRITE = 4
FILE_OVERWRITE_IF = 5

CREATE_NEW = 1
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
TRUNCATE_EXISTING = 5

FILE_CASE_PRESERVED_NAMES = 0x00000002
FILE_UNICODE_ON_DISK = 0x00000004
FILE_PERSISTENT_ACLS = 0x00000008
FILE_READ_ONLY_VOLUME = 0x00080000

VOLUME_SECURITY_DESCRIPTOR_MAX_SIZE = 1024 * 16
MAX_PATH = 260

DOKAN_CALLBACK = ctypes.WINFUNCTYPE


class FILETIME(Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class WIN32_FIND_DATAW(Structure):
    _fields_ = [
        ("dwFileAttributes", DWORD),
        ("ftCreationTime", FILETIME),
        ("ftLastAccessTime", FILETIME),
        ("ftLastWriteTime", FILETIME),
        ("nFileSizeHigh", DWORD),
        ("nFileSizeLow", DWORD),
        ("dwReserved0", DWORD),
        ("dwReserved1", DWORD),
        ("cFileName", ctypes.c_wchar * MAX_PATH),
        ("cAlternateFileName", ctypes.c_wchar * 14),
    ]


class BY_HANDLE_FILE_INFORMATION(Structure):
    _fields_ = [
        ("dwFileAttributes", DWORD),
        ("ftCreationTime", FILETIME),
        ("ftLastAccessTime", FILETIME),
        ("ftLastWriteTime", FILETIME),
        ("dwVolumeSerialNumber", DWORD),
        ("nFileSizeHigh", DWORD),
        ("nFileSizeLow", DWORD),
        ("nNumberOfLinks", DWORD),
        ("nFileIndexHigh", DWORD),
        ("nFileIndexLow", DWORD),
    ]


class DOKAN_OPTIONS(Structure):
    _pack_ = 8
    _fields_ = [
        ("Version", c_ushort),
        ("SingleThread", c_ubyte),
        ("Options", c_ulong),
        ("GlobalContext", c_ulonglong),
        ("MountPoint", c_wchar_p),
        ("UNCName", c_wchar_p),
        ("Timeout", c_ulong),
        ("AllocationUnitSize", c_ulong),
        ("SectorSize", c_ulong),
        ("VolumeSecurityDescriptorLength", c_ulong),
        ("VolumeSecurityDescriptor", c_char * VOLUME_SECURITY_DESCRIPTOR_MAX_SIZE),
    ]


class DOKAN_FILE_INFO(Structure):
    _pack_ = 8
    _fields_ = [
        ("Context", c_ulonglong),
        ("DokanContext", c_ulonglong),
        ("DokanOptions", c_void_p),
        ("ProcessingContext", c_void_p),
        ("ProcessId", c_ulong),
        ("IsDirectory", c_ubyte),
        ("DeletePending", c_ubyte),
        ("PagingIo", c_ubyte),
        ("SynchronousIo", c_ubyte),
        ("Nocache", c_ubyte),
        ("WriteToEndOfFile", c_ubyte),
    ]


PDOKAN_FILE_INFO = POINTER(DOKAN_FILE_INFO)
PWIN32_FIND_DATAW = POINTER(WIN32_FIND_DATAW)
PBY_HANDLE_FILE_INFORMATION = POINTER(BY_HANDLE_FILE_INFORMATION)
PFillFindData = ctypes.WINFUNCTYPE(ctypes.c_int, PWIN32_FIND_DATAW, PDOKAN_FILE_INFO)

ZwCreateFileProto = DOKAN_CALLBACK(
    NTSTATUS,
    LPCWSTR,
    c_void_p,
    c_ulong,
    c_ulong,
    c_ulong,
    c_ulong,
    c_ulong,
    PDOKAN_FILE_INFO,
)
CleanupProto = DOKAN_CALLBACK(None, LPCWSTR, PDOKAN_FILE_INFO)
CloseFileProto = DOKAN_CALLBACK(None, LPCWSTR, PDOKAN_FILE_INFO)
ReadFileProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, LPVOID, DWORD, POINTER(DWORD), c_longlong, PDOKAN_FILE_INFO
)
WriteFileProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_void_p, DWORD, POINTER(DWORD), c_longlong, PDOKAN_FILE_INFO
)
FlushFileBuffersProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, PDOKAN_FILE_INFO)
GetFileInformationProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, PBY_HANDLE_FILE_INFORMATION, PDOKAN_FILE_INFO
)
FindFilesProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, PFillFindData, PDOKAN_FILE_INFO)
FindFilesWithPatternProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, LPCWSTR, PFillFindData, PDOKAN_FILE_INFO
)
SetFileAttributesProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, DWORD, PDOKAN_FILE_INFO)
SetFileTimeProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_void_p, c_void_p, c_void_p, PDOKAN_FILE_INFO
)
DeleteFileProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, PDOKAN_FILE_INFO)
DeleteDirectoryProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, PDOKAN_FILE_INFO)
MoveFileProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, LPCWSTR, BOOL, PDOKAN_FILE_INFO)
SetEndOfFileProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, c_longlong, PDOKAN_FILE_INFO)
SetAllocationSizeProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, c_longlong, PDOKAN_FILE_INFO)
LockFileProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_longlong, c_longlong, PDOKAN_FILE_INFO
)
UnlockFileProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_longlong, c_longlong, PDOKAN_FILE_INFO
)
GetDiskFreeSpaceProto = DOKAN_CALLBACK(
    NTSTATUS,
    POINTER(c_ulonglong),
    POINTER(c_ulonglong),
    POINTER(c_ulonglong),
    PDOKAN_FILE_INFO,
)
GetVolumeInformationProto = DOKAN_CALLBACK(
    NTSTATUS,
    c_void_p,  # VolumeNameBuffer (must stay a writable pointer; LPWSTR becomes str)
    DWORD,
    POINTER(DWORD),
    POINTER(DWORD),
    POINTER(DWORD),
    c_void_p,  # FileSystemNameBuffer
    DWORD,
    PDOKAN_FILE_INFO,
)
MountedProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, PDOKAN_FILE_INFO)
UnmountedProto = DOKAN_CALLBACK(NTSTATUS, PDOKAN_FILE_INFO)
GetFileSecurityProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_void_p, LPVOID, c_ulong, POINTER(c_ulong), PDOKAN_FILE_INFO
)
SetFileSecurityProto = DOKAN_CALLBACK(
    NTSTATUS, LPCWSTR, c_void_p, LPVOID, c_ulong, PDOKAN_FILE_INFO
)
FindStreamsProto = DOKAN_CALLBACK(NTSTATUS, LPCWSTR, c_void_p, c_void_p, PDOKAN_FILE_INFO)


class DOKAN_OPERATIONS(Structure):
    _fields_ = [
        ("ZwCreateFile", ZwCreateFileProto),
        ("Cleanup", CleanupProto),
        ("CloseFile", CloseFileProto),
        ("ReadFile", ReadFileProto),
        ("WriteFile", WriteFileProto),
        ("FlushFileBuffers", FlushFileBuffersProto),
        ("GetFileInformation", GetFileInformationProto),
        ("FindFiles", FindFilesProto),
        ("FindFilesWithPattern", FindFilesWithPatternProto),
        ("SetFileAttributes", SetFileAttributesProto),
        ("SetFileTime", SetFileTimeProto),
        ("DeleteFile", DeleteFileProto),
        ("DeleteDirectory", DeleteDirectoryProto),
        ("MoveFile", MoveFileProto),
        ("SetEndOfFile", SetEndOfFileProto),
        ("SetAllocationSize", SetAllocationSizeProto),
        ("LockFile", LockFileProto),
        ("UnlockFile", UnlockFileProto),
        ("GetDiskFreeSpace", GetDiskFreeSpaceProto),
        ("GetVolumeInformation", GetVolumeInformationProto),
        ("Mounted", MountedProto),
        ("Unmounted", UnmountedProto),
        ("GetFileSecurity", GetFileSecurityProto),
        ("SetFileSecurity", SetFileSecurityProto),
        ("FindStreams", FindStreamsProto),
    ]


def _load_dokan() -> ctypes.WinDLL:
    candidates: list[str] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    base = os.path.join(pf, "Dokan")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            dll = os.path.join(base, name, "dokan2.dll")
            if os.path.isfile(dll):
                candidates.append(dll)
    candidates.extend(["dokan2.dll", "dokan1.dll"])

    last_err: OSError | None = None
    for name in candidates:
        try:
            return ctypes.WinDLL(name)
        except OSError as exc:
            last_err = exc
    raise OSError(
        "Dokany DLL not found. Install Dokany 2: winget install dokan-dev.Dokany\n"
        f"Tried: {candidates}\nDetail: {last_err}"
    )


_dokan = None


def get_dokan() -> ctypes.WinDLL:
    global _dokan
    if _dokan is None:
        dll = _load_dokan()
        dll.DokanInit.restype = None
        dll.DokanShutdown.restype = None
        dll.DokanMain.argtypes = [POINTER(DOKAN_OPTIONS), POINTER(DOKAN_OPERATIONS)]
        dll.DokanMain.restype = ctypes.c_int
        dll.DokanNtStatusFromWin32.argtypes = [DWORD]
        dll.DokanNtStatusFromWin32.restype = NTSTATUS
        dll.DokanVersion.restype = c_ulong
        dll.DokanDriverVersion.restype = c_ulong
        dll.DokanRemoveMountPoint.argtypes = [LPCWSTR]
        dll.DokanRemoveMountPoint.restype = BOOL
        dll.DokanMapKernelToUserCreateFileFlags.argtypes = [
            c_ulong,
            c_ulong,
            c_ulong,
            c_ulong,
            POINTER(c_ulong),
            POINTER(DWORD),
            POINTER(DWORD),
        ]
        dll.DokanMapKernelToUserCreateFileFlags.restype = None
        dll.DokanResetTimeout.argtypes = [c_ulong, PDOKAN_FILE_INFO]
        dll.DokanResetTimeout.restype = BOOL
        _dokan = dll
    return _dokan


def unix_to_filetime(ts: float) -> FILETIME:
    if ts <= 0:
        return FILETIME(0, 0)
    val = int(ts * 10_000_000) + 116444736000000000
    return FILETIME(val & 0xFFFFFFFF, (val >> 32) & 0xFFFFFFFF)


def dokan_error_name(code: int) -> str:
    return {
        DOKAN_SUCCESS: "DOKAN_SUCCESS",
        DOKAN_ERROR: "DOKAN_ERROR",
        DOKAN_DRIVE_LETTER_ERROR: "DOKAN_DRIVE_LETTER_ERROR",
        DOKAN_DRIVER_INSTALL_ERROR: "DOKAN_DRIVER_INSTALL_ERROR",
        DOKAN_START_ERROR: "DOKAN_START_ERROR",
        DOKAN_MOUNT_ERROR: "DOKAN_MOUNT_ERROR",
        DOKAN_MOUNT_POINT_ERROR: "DOKAN_MOUNT_POINT_ERROR",
        DOKAN_VERSION_ERROR: "DOKAN_VERSION_ERROR",
    }.get(code, f"unknown({code})")
