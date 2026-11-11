"""Host-side at-rest encryption for user-visible app access codes.

Windows machine-scope DPAPI protects secrets on the host. No cleartext is stored in SQLite.
No key is shipped with the installer. Requires machine-specific backup/migration.
"""
import ctypes
from ctypes import wintypes
import os


def dpapi(blob:bytes,decrypt=False)->bytes:
    if os.name!='nt':
        # Unit-test only. Do not persist production secrets on non-Windows Host.
        raise RuntimeError('VMO secret vault requires Windows DPAPI')
    class BLOB(ctypes.Structure):
        _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_byte))]
    source=ctypes.create_string_buffer(blob)
    inp=BLOB(len(blob),ctypes.cast(source,ctypes.POINTER(ctypes.c_byte)))
    out=BLOB()
    fun=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    # 8765 Web Console and 8766 Guest Gateway may run under different accounts.
    # Machine-scoped DPAPI enables both services to use encrypted values.
    # Protect the database with restrictive NTFS ACLs; DPAPI is not an ACL.
    flag=0x01 | (0 if decrypt else 0x04)  # UI_FORBIDDEN | LOCAL_MACHINE for encryption
    if not fun(ctypes.byref(inp),None,None,None,None,flag,ctypes.byref(out)):
        raise ctypes.WinError()
    try:return ctypes.string_at(out.pbData,out.cbData)
    finally:
        free=ctypes.windll.kernel32.LocalFree
        free.argtypes=[wintypes.HLOCAL];free.restype=wintypes.HLOCAL
        free(out.pbData)
