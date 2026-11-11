"""Native Windows service dispatcher via stdlib ctypes; no pywin32 dependency.

Test this service under Windows before production. Windows SCM's service
entrypoint must be invoked when --service is passed to python.exe.
"""
import ctypes
from ctypes import wintypes
import threading

SERVICE_WIN32_OWN_PROCESS=0x10
SERVICE_START_PENDING=2
SERVICE_RUNNING=4
SERVICE_STOP_PENDING=3
SERVICE_STOPPED=1
SERVICE_ACCEPT_STOP=1


def start_service(worker):
    if not hasattr(ctypes,'WINFUNCTYPE'):
        raise RuntimeError('Windows native service dispatcher required')
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    SERVICE_MAIN=ctypes.WINFUNCTYPE(None,wintypes.DWORD,ctypes.POINTER(wintypes.LPWSTR))
    SERVICE_CONTROL=ctypes.WINFUNCTYPE(wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,ctypes.c_void_p)
    class STATUS(ctypes.Structure):
        _fields_=[('dwServiceType',wintypes.DWORD),('dwCurrentState',wintypes.DWORD),
                 ('dwControlsAccepted',wintypes.DWORD),('dwWin32ExitCode',wintypes.DWORD),
                 ('dwServiceSpecificExitCode',wintypes.DWORD),('dwCheckPoint',wintypes.DWORD),('dwWaitHint',wintypes.DWORD)]
    class ENTRY(ctypes.Structure):
        _fields_=[('lpServiceName',wintypes.LPWSTR),('lpServiceProc',SERVICE_MAIN)]
    register=advapi.RegisterServiceCtrlHandlerExW
    register.argtypes=[wintypes.LPCWSTR,SERVICE_CONTROL,ctypes.c_void_p]
    register.restype=wintypes.HANDLE
    set_status=advapi.SetServiceStatus
    set_status.argtypes=[wintypes.HANDLE,ctypes.POINTER(STATUS)]
    set_status.restype=wintypes.BOOL
    dispatch=advapi.StartServiceCtrlDispatcherW
    dispatch.argtypes=[ctypes.POINTER(ENTRY)]
    dispatch.restype=wintypes.BOOL
    stop=threading.Event()
    completely_stopped=threading.Event()
    handle=[None]
    def state(value,checkpoint=0):
        if not handle[0]:return
        status=STATUS(SERVICE_WIN32_OWN_PROCESS,value,SERVICE_ACCEPT_STOP if value==SERVICE_RUNNING else 0,
                      0,0,checkpoint,12000 if value in (SERVICE_START_PENDING,SERVICE_STOP_PENDING) else 0)
        if not set_status(handle[0],ctypes.byref(status)):raise ctypes.WinError(ctypes.get_last_error())
    @SERVICE_CONTROL
    def control(code,etype,data,context):
        if code in (1,5):
            stop.set()
            state(SERVICE_STOP_PENDING,1)
            # The WSS/network worker may take time to unwind. Advance the SCM
            # checkpoint during a graceful stop, instead of appearing hung.
            def progress():
                checkpoint=2
                while not completely_stopped.wait(3):
                    try: state(SERVICE_STOP_PENDING,checkpoint)
                    except OSError: return
                    checkpoint+=1
            threading.Thread(target=progress,daemon=True).start()
        return 0
    @SERVICE_MAIN
    def main(argc,argv):
        handle[0]=register('VMO-GuestAgentV2',control,None)
        if not handle[0]:return
        state(SERVICE_START_PENDING,1)
        state(SERVICE_RUNNING)
        try:worker(stop)
        finally:
            completely_stopped.set()
            state(SERVICE_STOPPED)
    entries=(ENTRY*2)()
    entries[0].lpServiceName='VMO-GuestAgentV2'
    entries[0].lpServiceProc=main
    entries[1].lpServiceName=None
    if not dispatch(entries):raise ctypes.WinError(ctypes.get_last_error())
