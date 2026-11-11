"""Run a locally approved updater as the ACTIVE Windows user, never as SYSTEM.

Restricted to a locally configured SID. Uses CreateProcessAsUserW and waits for an
exit code. No password storage, remote shell, or user-provided command strings.
"""
from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess

SID_RE = re.compile(r'^S-1-5-21-(?:\d+-){3}\d+$')


def _ps_string(value: str) -> str:
    if '\x00' in value or '\r' in value or '\n' in value:
        raise ValueError('Unsafe command value')
    return "'"+value.replace("'","''")+"'"


def render_command(executable: str, args: list[str]) -> str:
    """Native executable or .cmd/.bat invoked under real interactive user token."""
    if not (Path(executable).is_absolute() or PureWindowsPath(executable).is_absolute()) or Path(executable).suffix.lower() not in ('.exe','.cmd','.bat'):
        raise ValueError('Configured updater executable must be an absolute native or command path')
    if len(args)>15 or any(not isinstance(x,str) or len(x)>256 for x in args):
        raise ValueError('Invalid locally approved arguments')
    arg_list=' '.join(_ps_string(x) for x in args)
    return "$ErrorActionPreference='Stop'; & "+_ps_string(executable)+(' '+arg_list if arg_list else '')+"; if ($null -ne $LASTEXITCODE) { exit [int]$LASTEXITCODE } else { exit 0 }"


def _token_sid(token):
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    adv.GetTokenInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype=wintypes.BOOL
    required=wintypes.DWORD()
    adv.GetTokenInformation(token,1,None,0,ctypes.byref(required))  # TokenUser=1
    if not required.value:raise RuntimeError('CANNOT_READ_INTERACTIVE_SID')
    buffer=ctypes.create_string_buffer(required.value)
    if not adv.GetTokenInformation(token,1,buffer,required.value,ctypes.byref(required)):
        raise RuntimeError('CANNOT_READ_INTERACTIVE_SID')
    sid_pointer=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0]
    ptr=wintypes.LPWSTR()
    adv.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertSidToStringSidW.restype=wintypes.BOOL
    if not adv.ConvertSidToStringSidW(sid_pointer,ctypes.byref(ptr)):
        raise RuntimeError('CANNOT_STRINGIFY_INTERACTIVE_SID')
    try:return ptr.value
    finally:kernel.LocalFree(ctypes.cast(ptr,ctypes.c_void_p))


def active_user_sid() -> str:
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    try:from .interactive_session import _get_active_session
    except ImportError:from interactive_session import _get_active_session
    wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
    wts.WTSQueryUserToken.argtypes=[wintypes.ULONG,ctypes.POINTER(wintypes.HANDLE)]
    wts.WTSQueryUserToken.restype=wintypes.BOOL
    token=wintypes.HANDLE()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    if not wts.WTSQueryUserToken(_get_active_session(),ctypes.byref(token)):
        raise RuntimeError('NO_ACTIVE_USER_TOKEN')
    try:return _token_sid(token)
    finally:kernel.CloseHandle(token)


def run_as_user(executable: str,args: list[str],expected_sid: str,*,timeout:int=900) -> dict:
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    if not SID_RE.fullmatch(expected_sid or ''):raise ValueError('LOCAL_INTERACTIVE_USER_SID_REQUIRED')
    exe=Path(executable)
    if not exe.is_file():raise FileNotFoundError('LOCAL_UPDATER_EXECUTABLE_NOT_FOUND')
    command=render_command(str(exe),args)
    try:from .interactive_session import _get_active_session
    except ImportError:from interactive_session import _get_active_session
    session=_get_active_session()
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
    userenv=ctypes.WinDLL('userenv',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    wts.WTSQueryUserToken.argtypes=[wintypes.ULONG,ctypes.POINTER(wintypes.HANDLE)]
    wts.WTSQueryUserToken.restype=wintypes.BOOL
    adv.DuplicateTokenEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.POINTER(wintypes.HANDLE)]
    adv.DuplicateTokenEx.restype=wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes=[ctypes.POINTER(ctypes.c_void_p),wintypes.HANDLE,wintypes.BOOL]
    userenv.CreateEnvironmentBlock.restype=wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes=[ctypes.c_void_p]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype=wintypes.BOOL
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    class STARTUPINFO(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('lpReserved',wintypes.LPWSTR),('lpDesktop',wintypes.LPWSTR),
        ('lpTitle',wintypes.LPWSTR),('dwX',wintypes.DWORD),('dwY',wintypes.DWORD),
        ('dwXSize',wintypes.DWORD),('dwYSize',wintypes.DWORD),('dwXCountChars',wintypes.DWORD),
        ('dwYCountChars',wintypes.DWORD),('dwFillAttribute',wintypes.DWORD),('dwFlags',wintypes.DWORD),
        ('wShowWindow',wintypes.WORD),('cbReserved2',wintypes.WORD),('lpReserved2',ctypes.c_void_p),
        ('hStdInput',wintypes.HANDLE),('hStdOutput',wintypes.HANDLE),('hStdError',wintypes.HANDLE)]
    class PROCINFO(ctypes.Structure):
        _fields_=[('hProcess',wintypes.HANDLE),('hThread',wintypes.HANDLE),('dwProcessId',wintypes.DWORD),('dwThreadId',wintypes.DWORD)]
    adv.CreateProcessAsUserW.argtypes=[wintypes.HANDLE,wintypes.LPCWSTR,wintypes.LPWSTR,
        ctypes.c_void_p,ctypes.c_void_p,wintypes.BOOL,wintypes.DWORD,ctypes.c_void_p,
        wintypes.LPCWSTR,ctypes.POINTER(STARTUPINFO),ctypes.POINTER(PROCINFO)]
    adv.CreateProcessAsUserW.restype=wintypes.BOOL
    token=wintypes.HANDLE();dup=wintypes.HANDLE();env=ctypes.c_void_p();created=False;proc=PROCINFO()
    try:
        if not wts.WTSQueryUserToken(session,ctypes.byref(token)):
            raise RuntimeError('NO_ACTIVE_USER_TOKEN')
        if _token_sid(token).lower()!=expected_sid.lower():
            raise PermissionError('ACTIVE_USER_SID_DOES_NOT_MATCH_LOCAL_ADAPTER')
        if not adv.DuplicateTokenEx(token,0x02000000,None,2,1,ctypes.byref(dup)):
            raise RuntimeError('CANNOT_DUPLICATE_INTERACTIVE_TOKEN')
        if not userenv.CreateEnvironmentBlock(ctypes.byref(env),dup,False):
            raise RuntimeError('CANNOT_BUILD_INTERACTIVE_USER_ENV')
        created=True
        pwsh=str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')
        encoded=base64.b64encode(command.encode('utf-16le')).decode('ascii')
        command_line=subprocess.list2cmdline([pwsh,'-NoProfile','-NonInteractive','-EncodedCommand',encoded])
        startup=STARTUPINFO();startup.cb=ctypes.sizeof(startup)
        startup.lpDesktop=r'winsta0\default'
        raw=ctypes.create_unicode_buffer(command_line)
        if not adv.CreateProcessAsUserW(dup,pwsh,raw,None,None,False,
            0x00000400|0x08000000,env,str(exe.parent),ctypes.byref(startup),ctypes.byref(proc)):
            raise RuntimeError('USER_UPDATER_CREATE_PROCESS_FAILED_'+str(ctypes.get_last_error()))
        status=kernel.WaitForSingleObject(proc.hProcess,int(timeout)*1000)
        if status==0x102:
            # Unattended install must not continue after the task returns unknown.
            kernel.TerminateProcess(proc.hProcess,1)
            raise TimeoutError('USER_UPDATE_TIMEOUT_OUTCOME_REQUIRES_REVIEW')
        if status!=0:raise RuntimeError('USER_UPDATER_WAIT_FAILED')
        exitcode=wintypes.DWORD()
        if not kernel.GetExitCodeProcess(proc.hProcess,ctypes.byref(exitcode)):
            raise RuntimeError('USER_UPDATER_EXIT_UNKNOWN')
        return {'exit_code':int(exitcode.value),'session_id':session}
    finally:
        if proc.hThread:kernel.CloseHandle(proc.hThread)
        if proc.hProcess:kernel.CloseHandle(proc.hProcess)
        if created:userenv.DestroyEnvironmentBlock(env)
        if dup:kernel.CloseHandle(dup)
        if token:kernel.CloseHandle(token)
