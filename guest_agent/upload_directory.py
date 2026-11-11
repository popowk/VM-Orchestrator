"""Guest-side validation for administrator-chosen local Windows upload folders.

This file is also bundled with the golden template installer. The Guest Agent
must not import the host app package, because its installation is independent.
"""
from __future__ import annotations
import ntpath
import os
import re


def guest_directory(raw: str) -> str:
    if not isinstance(raw, str) or not 3 <= len(raw) <= 900:
        raise ValueError('INVALID_UPLOAD_DIRECTORY')
    raw=raw.replace('/', '\\')
    if not re.match(r'^[A-Za-z]:\\',raw) or any(c in raw[3:] for c in '<>:"|?*\x00'):
        raise ValueError('UPLOAD_DIRECTORY_MUST_BE_LOCAL_DRIVE_PATH')
    parts=raw[3:].split('\\')
    for part in parts:
        if not part:continue
        if part in ('.','..') or part.rstrip(' .')!=part:
            raise ValueError('UPLOAD_DIRECTORY_UNSAFE_SEGMENT')
        if re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',part):
            raise ValueError('UPLOAD_DIRECTORY_RESERVED_NAME')
    result=ntpath.normpath(raw)
    if len(result)<=3 or re.match(r'(?i)^[a-z]:\\(?:windows|program files(?: \(x86\))?|programdata)(?:\\|$)',result):
        raise ValueError('UPLOAD_DIRECTORY_PROTECTED')
    # Windows junctions and symbolic links can be abused to circumvent textual
    # path checks. Reject any existing reparse component along the requested path.
    if os.name=='nt':
        drive,tail=ntpath.splitdrive(result)
        cursor=drive+'\\'
        for item in tail.lstrip('\\').split('\\'):
            cursor=ntpath.join(cursor,item)
            if os.path.lexists(cursor) and (os.path.islink(cursor) or getattr(os.path,'isjunction',lambda _p:False)(cursor)):
                raise ValueError('UPLOAD_DIRECTORY_REPARSE_POINT')
    return result


def guest_filename(filename: str) -> str:
    if not isinstance(filename,str) or not 1<=len(filename)<=180 or len(filename.encode('utf-8'))>600:
        raise ValueError('UPLOAD_INVALID_FILENAME')
    if filename in ('.','..') or filename.rstrip(' .')!=filename or any(ord(c)<32 or c in '<>:"/\\|?*' for c in filename):
        raise ValueError('UPLOAD_INVALID_FILENAME')
    if re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',filename):
        raise ValueError('UPLOAD_RESERVED_FILENAME')
    return filename
