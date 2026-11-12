"""Launch a visible console in the existing Windows user's interactive session.

Called only by the authenticated, SYSTEM-owned VMO Guest Agent after an admin
COMMAND. Never opens a listener, never stores user passwords, and never gives
SYSTEM a desktop on Session 0. The command runs with the logged-on user's token.
Windows-only; offline unit tests use injected stubs for this boundary.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
import subprocess
import base64
import re

class InteractiveLaunchError(RuntimeError):
    pass


def _get_active_session():
    wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
    class WTS_SESSION_INFO(ctypes.Structure):
        _fields_=[('SessionId',wintypes.DWORD),('pWinStationName',wintypes.LPWSTR),('State',ctypes.c_int)]
    enum=wts.WTSEnumerateSessionsW
    enum.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFO)),ctypes.POINTER(wintypes.DWORD)]
    enum.restype=wintypes.BOOL
    free=wts.WTSFreeMemory
    free.argtypes=[wintypes.LPVOID]
    free.restype=None
    pointer=ctypes.POINTER(WTS_SESSION_INFO)()
    count=wintypes.DWORD(0)
    if not enum(None,0,1,ctypes.byref(pointer),ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        active=sorted({int(pointer[i].SessionId) for i in range(count.value)
                       if pointer[i].State==0 and pointer[i].SessionId>0})
    finally:
        free(pointer)
    if not active:raise InteractiveLaunchError('NO_INTERACTIVE_USER_LOGGED_ON')
    if len(active)>1:raise InteractiveLaunchError('MULTIPLE_INTERACTIVE_SESSIONS')
    return active[0]


def _windows_process_command(cmd, system_root):
    """Return (application_name, raw Win32 command_line) without CMD quote corruption."""
    if cmd[0].lower()=='exe':
        # Portable apps are launched directly from the approved C:\\dsh
        # installation path, never via a command shell or extra arguments.
        exe=os.path.abspath(cmd[1])
        if not os.path.isabs(exe) or not os.path.isfile(exe):
            raise InteractiveLaunchError('PORTABLE_EXECUTABLE_NOT_FOUND')
        return exe,subprocess.list2cmdline([exe])
    if cmd[0].lower()=='cmd.exe':
        exe=os.path.join(system_root,'System32','cmd.exe')
        operation='/C' if len(cmd)>2 and cmd[2].upper()=='/C' else '/K'
        # CMD needs its quoted command text verbatim, not list2cmdline.
        return exe,f'"{exe}" /D {operation} {cmd[3]}'
    exe=os.path.join(system_root,'System32','WindowsPowerShell','v1.0','powershell.exe')
    return exe,subprocess.list2cmdline([exe]+cmd[1:])


def _start_process(session_id,cmd,cwd):
    if os.name!='nt':raise InteractiveLaunchError('WINDOWS_ONLY')
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
    userenv=ctypes.WinDLL('userenv',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)

    # Explicit WinAPI signatures avoid HANDLE truncation on 64-bit Windows.
    wts.WTSQueryUserToken.argtypes=[wintypes.ULONG,ctypes.POINTER(wintypes.HANDLE)]
    wts.WTSQueryUserToken.restype=wintypes.BOOL
    adv.DuplicateTokenEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,
                                     ctypes.c_int,ctypes.c_int,ctypes.POINTER(wintypes.HANDLE)]
    adv.DuplicateTokenEx.restype=wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes=[ctypes.POINTER(ctypes.c_void_p),wintypes.HANDLE,wintypes.BOOL]
    userenv.CreateEnvironmentBlock.restype=wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes=[ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype=wintypes.BOOL

    class STARTUPINFO(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('lpReserved',wintypes.LPWSTR),
          ('lpDesktop',wintypes.LPWSTR),('lpTitle',wintypes.LPWSTR),
          ('dwX',wintypes.DWORD),('dwY',wintypes.DWORD),('dwXSize',wintypes.DWORD),
          ('dwYSize',wintypes.DWORD),('dwXCountChars',wintypes.DWORD),
          ('dwYCountChars',wintypes.DWORD),('dwFillAttribute',wintypes.DWORD),
          ('dwFlags',wintypes.DWORD),('wShowWindow',wintypes.WORD),
          ('cbReserved2',wintypes.WORD),('lpReserved2',ctypes.c_void_p),
          ('hStdInput',wintypes.HANDLE),('hStdOutput',wintypes.HANDLE),
          ('hStdError',wintypes.HANDLE)]
    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_=[('hProcess',wintypes.HANDLE),('hThread',wintypes.HANDLE),
                  ('dwProcessId',wintypes.DWORD),('dwThreadId',wintypes.DWORD)]
    adv.CreateProcessAsUserW.argtypes=[wintypes.HANDLE,wintypes.LPCWSTR,wintypes.LPWSTR,
       ctypes.c_void_p,ctypes.c_void_p,wintypes.BOOL,wintypes.DWORD,ctypes.c_void_p,
       wintypes.LPCWSTR,ctypes.POINTER(STARTUPINFO),ctypes.POINTER(PROCESS_INFORMATION)]
    adv.CreateProcessAsUserW.restype=wintypes.BOOL

    user=wintypes.HANDLE()
    if not wts.WTSQueryUserToken(session_id,ctypes.byref(user)):
        raise InteractiveLaunchError('CANNOT_GET_INTERACTIVE_USER_TOKEN')
    token=wintypes.HANDLE(); env=ctypes.c_void_p(); created=False
    proc=PROCESS_INFORMATION()
    try:
        if not adv.DuplicateTokenEx(user,0x02000000,None,2,1,ctypes.byref(token)):
            raise InteractiveLaunchError('CANNOT_DUPLICATE_USER_TOKEN')
        if not userenv.CreateEnvironmentBlock(ctypes.byref(env),token,False):
            raise InteractiveLaunchError('CANNOT_CREATE_INTERACTIVE_ENVIRONMENT')
        created=True
        startup=STARTUPINFO();startup.cb=ctypes.sizeof(startup)
        startup.lpDesktop=r'winsta0\default'
        exe,command_text=_windows_process_command(cmd,os.environ.get('SystemRoot',r'C:\Windows'))
        if len(command_text.encode('utf-16le')) // 2 >= 32767:
            raise InteractiveLaunchError('COMMAND_TOO_LONG_FOR_WINDOWS_PROCESS')
        if not os.path.isdir(cwd):
            raise InteractiveLaunchError('WORKING_DIRECTORY_NOT_FOUND: '+str(cwd))
        command=ctypes.create_unicode_buffer(command_text)
        flags=0x00000010 | 0x00000400  # CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT
        if not adv.CreateProcessAsUserW(token,exe,command,None,None,False,flags,
                                        env,str(cwd),ctypes.byref(startup),ctypes.byref(proc)):
            err=ctypes.get_last_error()
            raise InteractiveLaunchError('CREATE_PROCESS_AS_USER_FAILED_'+str(err))
        # We can confirm that *the console process* remains alive, not the app.
        status=int(kernel.WaitForSingleObject(proc.hProcess,1000))
        still_running=(status==0x102)  # WAIT_TIMEOUT
        code=wintypes.DWORD(0)
        if not still_running and status==0 and kernel.GetExitCodeProcess(proc.hProcess,ctypes.byref(code)):
            exit_code=int(code.value)
        else: exit_code=None
        return {'launched':True,'pid':int(proc.dwProcessId),'session_id':session_id,
                'run_mode':'interactive','process_alive':still_running,
                'exit_code':exit_code,
                'note':'Visible CMD/PowerShell in signed-in user session; Windows user permissions apply. PID is console, not proof the target service is healthy.'}
    finally:
        if proc.hThread:kernel.CloseHandle(proc.hThread)
        if proc.hProcess:kernel.CloseHandle(proc.hProcess)
        if created:userenv.DestroyEnvironmentBlock(env)
        if token:kernel.CloseHandle(token)
        if user:kernel.CloseHandle(user)


# We deliberately only normalize the two documented "launch a batch" spellings.
# Other administrator commands are passed to cmd.exe without rewriting syntax.
_BATCH_NAME=re.compile(r'[^<>:"/\\|?*\x00-\x1f]+\.(?:bat|cmd)\Z',re.I)
_START_BATCH=re.compile(
    r'\s*start\s+(?:""\s+)?/d\s+"(?P<folder>[^"\r\n]+)"\s+'
    r'cmd(?:\.exe)?\s+/c\s+"call\s+(?P<bat>[^"\r\n]+)"\s*',re.I)
_CD_BATCH=re.compile(
    r'\s*cd\s+/d\s+"(?P<folder>[^"\r\n]+)"\s*&&\s+'
    r'call\s+(?:"(?P<quoted>[^"\r\n]+)"|(?P<plain>[^&|<>\r\n]+?))\s*',re.I)


def foreground_command(text,shell,cwd):
    """Return (argv, working_directory) for a visible interactive console.

    A batch startup command must use lpCurrentDirectory, not nested `cd /d`
    inside a `cmd /K` string. The previous version let list2cmdline() escape
    nested CMD quotes as backslash+quote, causing Windows error 123.
    """
    if shell=='cmd':
        match=_START_BATCH.fullmatch(text) or _CD_BATCH.fullmatch(text)
        if match:
            folder=match.group('folder')
            bat=(match.groupdict().get('bat') or match.groupdict().get('quoted')
                 or match.groupdict().get('plain') or '').strip()
            if _BATCH_NAME.fullmatch(bat):
                return ['cmd.exe','/D','/K',f'call "{bat}"'],folder
        return ['cmd.exe','/D','/K',text],cwd
    # PowerShell's -EncodedCommand avoids any ambiguity in Win32 command-line
    # quoting, including Chinese text and paths containing spaces.
    encoded=base64.b64encode(text.encode('utf-16le')).decode('ascii')
    return ['powershell.exe','-NoProfile','-NoExit','-EncodedCommand',encoded],cwd


def launch_interactive(text,shell,cwd):
    if os.name!='nt':raise InteractiveLaunchError('WINDOWS_ONLY')
    session=_get_active_session()
    command,workdir=foreground_command(text,shell,cwd)
    return _start_process(session,command,workdir)
