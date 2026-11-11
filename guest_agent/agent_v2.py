"""VMO Guest Agent V2, outbound-only WSS persistent connection.

Commands are admin-only on authenticated Host Agent; enrollment trusts the host administrator.
No remote arbitrary shell is exposed to a Sib customer. Files land in VMO/incoming.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import random
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
try:
    from .ws_client import Websocket, WebsocketError
    from . import agent as legacy
    from .app_registry import read_registry, reported_apps, load_app_secret
    from .local_app_report import LocalReportServer
    from .machine_identity import verify_machine_binding
    from .interactive_session import launch_interactive, InteractiveLaunchError
    from .app_tunnel import local_http_reply, ws_open_worker, local_http_stream
except ImportError:
    from ws_client import Websocket, WebsocketError
    import agent as legacy
    from app_registry import read_registry, reported_apps, load_app_secret
    from local_app_report import LocalReportServer
    from machine_identity import verify_machine_binding
    from interactive_session import launch_interactive, InteractiveLaunchError
    from app_tunnel import local_http_reply, ws_open_worker, local_http_stream

AGENT_VERSION = json.loads(Path(__file__).with_name('agent-release.json').read_text(encoding='utf-8'))['version']
APP_IDS={'snowluma','dsh-fish-soul'}
DIAG_CMDS={
    'hostname':['hostname'],
    'network':['ipconfig','/all'],
    'processes':['tasklist','/FO','CSV'],
    'agent':['sc','query','VMO-GuestAgentV2'],
    'software':['powershell.exe','-NoProfile','-NonInteractive','-Command',
        'Get-Item "C:\\dsh\\DeepSeek Harness\\DeepSeek Harness.exe" -ErrorAction SilentlyContinue | Select-Object Name,LastWriteTime | Out-String'],
}


def safe_path_fragment(path):
    from urllib.parse import urlsplit
    import posixpath
    if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or '\\' in path or '\x00' in path:
        raise ValueError('Invalid local app path')
    p=urlsplit(path)
    if p.scheme or p.netloc or p.fragment or any(piece=='..' for piece in p.path.split('/')):
        raise ValueError('Invalid local app path')
    return p.path + ('?'+p.query if p.query else '')


def execute_op(op,cfg,root,ctx):
    action=op.get('action');params=op.get('args') or {}
    if action=='DIAGNOSTIC':
        name=params.get('name')
        if name not in DIAG_CMDS:raise ValueError('Unapproved diagnostic')
        cmd=DIAG_CMDS[name];timeout=12
    elif action=='COMMAND':
        if params.get('shell') not in ('cmd','powershell') or not params.get('confirmed') or params.get('privilege')!='system':
            raise PermissionError('COMMAND_REQUEST_NOT_AUTHORIZED')
        text=params.get('command','')
        max_chars=8000 if params['shell']=='cmd' else 12000
        if not isinstance(text,str) or not 1<=len(text)<=max_chars:raise ValueError('Invalid command length')
        if params['shell']=='powershell':cmd=['powershell.exe','-NoProfile','-NonInteractive','-Command',text]
        else:cmd=['cmd.exe','/D','/S','/C',text]
        timeout=int(params.get('timeout_seconds',60))
        if not 5 <= timeout <= 300:raise ValueError('Invalid command timeout')
        mode=params.get('run_mode','wait')
        if mode not in ('wait','detached','interactive'):raise ValueError('Invalid execution mode')
        if mode != 'interactive':
            # A long script can expand after Windows argument quoting.
            # CMD itself has a smaller limit than CreateProcessW.
            command_line=subprocess.list2cmdline(cmd)
            if params['shell']=='cmd' and len(command_line)>8191:
                raise ValueError('CMD_COMMAND_LINE_TOO_LONG_USE_SCRIPT_FILE')
            if len(command_line.encode('utf-16le'))//2>=32767:
                raise ValueError('WINDOWS_COMMAND_LINE_TOO_LONG_USE_SCRIPT_FILE')
        if mode=='interactive':
            return launch_interactive(text,params['shell'],params.get('cwd') or os.environ.get('SystemDrive','C:')+'\\')
        if mode=='detached':
            # Long-running launchers must not block on their child process. Never
            # attach stdout/stderr pipes to a detached child (avoids pipe stalls).
            flags=(getattr(subprocess,'CREATE_NO_WINDOW',0)
                   |getattr(subprocess,'DETACHED_PROCESS',0)
                   |getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
            child=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,cwd=str(params.get('cwd') or root),shell=False,close_fds=True,
                creationflags=flags)
            return {'launched':True,'pid':child.pid,'run_mode':'detached',
                'note':'SYSTEM Session 0 accepted the launcher; PID may belong to cmd.exe, not the final app. App health unconfirmed. For visible UI use a logged-in-user Scheduled Task.'}
    elif action in ('FILES_LIST','FILE_READ'):
        import re
        area=params.get('area')
        if area not in ('logs','incoming'):raise ValueError('Unsupported file area')
        base=(root/area).resolve()
        if not base.is_dir():return {'files':[],'area':area} if action=='FILES_LIST' else {'error':'LOG_DIRECTORY_MISSING'}
        if action=='FILES_LIST':
            entries=[]
            for candidate in sorted(base.iterdir(),key=lambda x:x.name.lower())[:80]:
                if candidate.is_symlink() or not candidate.is_file():continue
                try:
                    st=candidate.stat()
                    entries.append({'name':candidate.name[:120],'bytes':st.st_size})
                except OSError:continue
            return {'files':entries,'area':area,'truncated':len(entries)>=80}
        filename=params.get('filename','')
        if area!='logs' or not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',filename):
            raise ValueError('Unsupported log read')
        file=(base/filename)
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(base):
            raise ValueError('Log file unavailable')
        with file.open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-24000));body=f.read(24000)
        return {'name':filename,'area':'logs','total_bytes':size,'tail_text':body.decode('utf-8',errors='replace')}
    elif action=='UPLOAD':
        import re
        name=params.get('filename','')
        try:
            from .upload_directory import guest_filename
        except ImportError:
            from upload_directory import guest_filename
        guest_filename(name)
        from pathlib import PureWindowsPath
        from urllib.parse import quote
        # Import the same local-Windows directory validator on the Guest. No
        # dependency on the host package (the Agent is installed separately).
        try:
            from .upload_directory import guest_directory
        except ImportError:
            from upload_directory import guest_directory
        directory=guest_directory(params.get('directory','C:\\dsh\\'))
        expected_size=int(params.get('bytes',-1))
        if expected_size<0:raise ValueError('Invalid upload size')
        expected_sha=params.get('sha256','')
        if not isinstance(expected_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_sha):
            raise ValueError('Invalid upload checksum')
        dest=Path(directory)
        dest.mkdir(parents=True,exist_ok=True)
        # Atomic replace is intentionally not used for existing files: reject
        # duplicates instead of silently overwriting customer data.
        final=dest/name
        if final.exists():raise ValueError('UPLOAD_TARGET_EXISTS: '+str(final))
        temp=dest/('.vmo-upload-'+op['id']+'.part')
        h=hashlib.sha256();total=0
        req=urllib.request.Request(cfg['server_url'].rstrip('/')+'/agent/uploads/'+quote(op['id']),
            headers={'X-Agent-ID':cfg['agent_id'],'X-Agent-Key':cfg['agent_key']},method='GET')
        try:
            try:
                with urllib.request.urlopen(req,timeout=120,context=ctx) as res, temp.open('xb') as out:
                    while True:
                        block=res.read(1024*1024)
                        if not block:break
                        out.write(block);h.update(block);total+=len(block)
                        if total>expected_size:raise ValueError('UPLOAD_SIZE_MISMATCH')
            except urllib.error.HTTPError as err:
                raise ValueError('UPLOAD_GATEWAY_HTTP_'+str(err.code)) from err
            except urllib.error.URLError as err:
                raise ValueError('UPLOAD_GATEWAY_CONNECTION_FAILED') from err
            if total!=expected_size or h.hexdigest()!=expected_sha:
                raise ValueError('UPLOAD_CHECKSUM_MISMATCH')
            if final.exists():raise ValueError('UPLOAD_TARGET_EXISTS: '+str(final))
            temp.rename(final)
        finally:temp.unlink(missing_ok=True)
        return {'path':str(final),'bytes':total,'sha256':expected_sha,'executed':False}
    elif action=='APP_HTTP':
        app=params.get('app_id')
        if app not in APP_IDS:raise ValueError('Unsupported application')
        registry=read_registry(root)
        entry=registry.get(app)
        if not entry:raise ValueError('App not registered on this Guest')
        target=entry['url'].rstrip('/')+safe_path_fragment(params.get('path','/'))
        method=params.get('method','GET')
        if method not in ('GET','POST'):raise ValueError('Unsupported app method')
        payload=base64.b64decode(params.get('body_b64',''),validate=True)
        if len(payload)>128*1024:raise ValueError('App request too large')
        # NO redirects, no cookie forwarding, no arbitrary proxy headers.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kw):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect)
        req=urllib.request.Request(target,data=payload if method=='POST' else None,method=method,
            headers={'Content-Type':str(params.get('content_type','application/octet-stream'))[:100]})
        try:
            response=opener.open(req,timeout=8)
        except urllib.error.HTTPError as ex: response=ex
        with response as r:
            body=r.read(256*1024+1)
            if len(body)>256*1024:raise ValueError('Proxy response too large')
            typ=r.headers.get('Content-Type','application/octet-stream')[:80]
            if any(x in typ.lower() for x in ('text/html','javascript')):
                # No executable scripts or HTML in the initial safe proxy.
                raise PermissionError('Interactive app web proxy not yet enabled; metadata is available')
            return {'status':r.status,'content_type':typ,'body_b64':base64.b64encode(body).decode('ascii')}
    else:
        raise ValueError('Unknown operation')
    res=subprocess.run(cmd,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout,
        cwd=str(params.get('cwd') or root) if action=='COMMAND' else str(root),shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return {'exit_code':res.returncode,
            'stdout':res.stdout[:20*1024].decode('utf-8',errors='replace'),
            'stderr':res.stderr[:10*1024].decode('utf-8',errors='replace')}


def run_live(config_path:Path,stop:threading.Event=None):
    stop=stop or threading.Event()
    root=config_path.parent
    root.mkdir(parents=True,exist_ok=True)
    logs=root/'logs';logs.mkdir(exist_ok=True)
    logger=logging.getLogger('vmo_v2')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler=RotatingFileHandler(str(logs/'agent-v2.log'),maxBytes=1024*1024,backupCount=4,encoding='utf-8')
        formatter=logging.Formatter('%(asctime)s +08:00 %(levelname)s %(message)s')
        formatter.converter=lambda seconds:time.gmtime(seconds+8*3600)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    # The loopback receiver is owned by the SYSTEM service and remains available
    # even while outbound WSS is reconnecting. No state ACL weakening.
    try:
        local_report_server=LocalReportServer(root,logger).start()
        logger.info('Local app reporter listening on 127.0.0.1:18767')
        def stop_local_reporter():
            stop.wait()
            local_report_server.close()
        threading.Thread(target=stop_local_reporter,daemon=True).start()
    except OSError as exc:
        logger.error('Local app reporter unavailable (%s)', type(exc).__name__)
        # No hidden fallback that writes to protected state from an ordinary user.
    failure_count=0
    last_connect_error=None
    results_path=root/'pending-results.json'
    results_lock=threading.RLock()
    active_lock=threading.RLock()
    active={'id':None,'worker':None}
    # Bounded burst buffer for SPA static assets (each WSS chunk <= 24 KiB).
    app_outbox=queue.Queue(maxsize=512)
    app_streams={}
    http_streams={}
    try:MAX_HTTP_STREAMS=max(10,min(96,int(os.environ.get('VMO_GUEST_APP_HTTP_STREAMS','24'))))
    except ValueError:MAX_HTTP_STREAMS=24  # Host default 16 assets + 8 SSE
    try:MAX_APP_SOCKETS=max(1,min(64,int(os.environ.get('VMO_APP_WS_CONNECTIONS','32'))))
    except ValueError:MAX_APP_SOCKETS=32
    try: pending_results=json.loads(results_path.read_text(encoding='utf-8'))
    except (ValueError,OSError):pending_results={}
    if not isinstance(pending_results,dict):pending_results={}
    def remember(reply):
        with results_lock:
            pending_results[reply['id']]=reply
            legacy.atomic_json(results_path,pending_results)
    def acknowledge(op_id):
        with results_lock:
            if op_id in pending_results:
                pending_results.pop(op_id)
                legacy.atomic_json(results_path,pending_results)
    while not stop.is_set():
        ws=None
        try:
            if not config_path.is_file():
                legacy.bootstrap_from_iso(config_path)
            if not config_path.is_file():
                raise ValueError('Awaiting unique enrollment configuration')
            cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
            if not cfg.get('agent_key'):
                if not legacy.agent_loop(config_path,once=True):
                    raise ValueError('Initial enrollment pending')
                cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
            base=cfg['server_url']
            if not base.startswith('https://'):raise ValueError('HTTPS required')
            ws=Websocket(base.replace('https://','wss://',1).rstrip('/')+'/agent/ws',
                         {'X-Agent-ID':cfg['agent_id'],'X-Agent-Key':cfg['agent_key']},cfg.get('ca_bundle'))
            # Server proves the Agent's authorized vm_id before this installation
            # binds its unique VMware SMBIOS identity. Do not trust an IP for identity.
            welcome = None
            for _ in range(12):
                welcome = ws.recv()
                if welcome is not None: break
            if not isinstance(welcome, dict) or welcome.get('type') != 'connected' or welcome.get('vm_id') != cfg['vm_id']:
                raise WebsocketError('Guest gateway identity handshake failed')
            try:
                machine_uuid = verify_machine_binding(root, cfg['vm_id'])
            except PermissionError:
                # The server can persist the reason while WSS is still authenticated.
                # Never send the duplicated/mismatched identity or credentials.
                ws.send({'type':'client.identity_error','reason':'IDENTITY_MISMATCH'})
                raise
            logger.info('WSS connected and VM identity verified')
            if last_connect_error:
                ws.send({'type':'client.reconnect','reason':last_connect_error})
                last_connect_error=None
            failure_count=0
            last_hb=0.0;last_apps=0.0
            sent_results={}
            worker=None
            def handle(kind,task,cfg_local):
                op_id=task.get('id')
                try:
                    if kind=='operation':
                        result=execute_op(task,cfg_local,root,ssl.create_default_context(cafile=cfg_local.get('ca_bundle')))
                        good=((result.get('launched') is True and isinstance(result.get('pid'),int) and result['pid']>0 and not (result.get('process_alive') is False and isinstance(result.get('exit_code'),int) and result['exit_code']!=0)) if task.get('action')=='COMMAND' and task.get('args',{}).get('run_mode') in ('interactive','detached') else result.get('exit_code',0)==0)
                        reply={'type':'operation.result','id':op_id,'ok':good,'result':result}
                    else:
                        mode=task.get('kind')
                        if mode=='ROLLBACK':result=legacy.perform_rollback(task,cfg_local)
                        else:
                            def report_phase(phase):
                                try:app_outbox.put_nowait({'type':'software.progress','id':op_id,'phase':phase})
                                except queue.Full:pass
                            report_phase('starting')
                            result=legacy.perform_update(task,cfg_local,root,ssl.create_default_context(cafile=cfg_local.get('ca_bundle')),on_phase=report_phase)
                        legacy.atomic_json(config_path,cfg_local)
                        reply={'type':'software.result','id':op_id,'ok':True,'message':json.dumps(result,ensure_ascii=False)[:800]}
                except Exception as e:
                    # Don't forward raw exception strings that may contain secrets, paths or tokens.
                    if kind=='operation':
                        code = str(e) if isinstance(e,InteractiveLaunchError) else \
                               str(e).split(':',1)[0][:100] if task.get('action')=='UPLOAD' and isinstance(e,ValueError) and str(e).startswith('UPLOAD_') else \
                               'COMMAND_REQUEST_NOT_AUTHORIZED' if str(e)=='COMMAND_REQUEST_NOT_AUTHORIZED' else \
                               'WINDOWS_ACCESS_DENIED' if isinstance(e,PermissionError) else type(e).__name__
                        reply={'type':'operation.result','id':op_id,'ok':False,'result':{'error':code}}
                    else:reply={'type':'software.result','id':op_id,'ok':False,'message':legacy.safe_update_failure(e)}
                remember(reply)
                with active_lock:
                    if active['id']==op_id:
                        active['id']=None
                        active['worker']=None
            while not stop.is_set():
                tick=time.monotonic()
                if tick-last_hb>=30:
                    report=legacy.snapshot_versions(cfg)
                    report['guest_agent']=AGENT_VERSION
                    report['smbios_uuid']=machine_uuid
                    report['transport']='wss'
                    report['remote_commands_allowed']=True
                    ws.send({'type':'heartbeat','report':report})
                    last_hb=tick
                if tick-last_apps>=20:
                    apps=reported_apps(root)
                    ws.send({'type':'apps.report','apps':apps})
                    # Secrets travel only over validated TLS; host stores encrypted.
                    # Repeating a WSS sync must not alter the original report time.
                    for a in apps:
                        secret=load_app_secret(root,a['app_id'])
                        if secret:
                            ws.send({'type':'app.secret','app_id':a['app_id'],
                                     'secret':secret,
                                     'reported_at':a.get('secret_reported_at')})
                    last_apps=tick
                # Send already queued update progress BEFORE the final result;
                # otherwise SUCCEEDED may reach Host first and suppress the phases.
                # App traffic shares this authenticated socket, not the command worker.
                for _ in range(36):
                    try: app_event=app_outbox.get_nowait()
                    except queue.Empty:break
                    ws.send(app_event)
                # Durable result outbox: the worker can finish during a reconnect.
                with results_lock:
                    snapshot=list(pending_results.items())
                for op_id,reply in snapshot:
                    if tick-sent_results.get(op_id,-100)>5:
                        ws.send(reply)
                        sent_results[op_id]=tick
                event=ws.recv()
                if not event:continue
                if event.get('type') in ('operation.ack','software.ack'):
                    op_id=event.get('id')
                    acknowledge(op_id)
                    sent_results.pop(op_id,None)
                    continue
                if event.get('type')=='app.ws.open':
                    req=event.get('request',{})
                    if isinstance(req,dict) and len(app_streams)<MAX_APP_SOCKETS:
                        threading.Thread(target=ws_open_worker,
                            args=(root,req,app_streams,app_outbox),daemon=True).start()
                    continue
                if event.get('type') in ('app.ws.client','app.ws.close'):
                    entry=app_streams.get(event.get('id',''))
                    if entry:
                        try:entry['queue'].put_nowait(event if event['type']=='app.ws.client' else {'type':'close'})
                        except queue.Full:entry['stop'].set()
                    continue
                if event.get('type')=='app.http.cancel':
                    stopped=http_streams.get(event.get('id',''))
                    if stopped:stopped.set()
                    continue
                if event.get('type')=='app.http.request':
                    request=event.get('request',{})
                    if isinstance(request,dict) and request.get('stream') is True:
                        key=request.get('id')
                        if isinstance(key,str) and len(key)==36 and len(http_streams)<MAX_HTTP_STREAMS:
                            cancel=threading.Event()
                            http_streams[key]=cancel
                            def worker_stream(req=request,flag=cancel):
                                def emit(item):
                                    try:app_outbox.put(item,timeout=10)
                                    except queue.Full:flag.set()
                                try:local_http_stream(root,req,emit,flag)
                                finally:http_streams.pop(req.get('id'),None)
                            threading.Thread(target=worker_stream,daemon=True).start()
                        elif isinstance(key,str) and len(key)==36:
                            # Never silently drop a stream request: the Host
                            # should fail promptly rather than wait 24 seconds.
                            try:app_outbox.put_nowait({'type':'app.http.stream_start',
                                'id':key,'status':503,'content_type':'text/plain'})
                            except queue.Full:pass
                        continue
                    if isinstance(request,dict):
                        def local_request_worker(req=request):
                            answer=local_http_reply(root,req)
                            try:app_outbox.put(answer,timeout=1)
                            except queue.Full:logger.warning('Application relay response queue full')
                        threading.Thread(target=local_request_worker,daemon=True).start()
                    continue
                if event.get('type') in ('operation','software_update'):
                    task=event.get('operation') if event['type']=='operation' else event.get('task')
                    if not isinstance(task,dict) or not isinstance(task.get('id'),str):
                        raise ValueError('Malformed remote task')
                    op_id=task['id']
                    with results_lock:
                        cached=pending_results.get(op_id)
                    if cached:
                        ws.send(cached)
                        sent_results[op_id]=time.monotonic()
                        continue
                    with active_lock:
                        if active['id']==op_id:
                            if event['type']=='operation':ws.send({'type':'operation.started','id':op_id})
                            continue
                        if active['worker'] and active['worker'].is_alive():
                            # Do not execute two privileged jobs concurrently.
                            # The server will recover the undelivered job via lease.
                            raise RuntimeError('Guest busy with a previous operation')
                        active['id']=op_id
                        worker=threading.Thread(target=handle,args=(event['type'],task,cfg.copy()),daemon=True)
                        active['worker']=worker
                    # Start locally BEFORE reporting RUNNING. If the socket
                    # disconnects during the acknowledgement, the worker still
                    # persists its result and the outbox retransmits it.
                    worker.start()
                    if event['type']=='operation':
                        ws.send({'type':'operation.started','id':op_id})
        except (OSError,ValueError,KeyError,WebsocketError,ssl.SSLError,RuntimeError) as exc:
            # Emit types only; sensitive credential content must never enter logs.
            # Internal WSS exceptions contain only fixed diagnostic text and must
            # never include URLs, request headers, secrets or response content.
            if isinstance(exc, PermissionError):
                detail = 'VM identity or state permissions invalid'
            elif isinstance(exc, WebsocketError):
                detail = str(exc)[:95]
            elif isinstance(exc, ssl.SSLCertVerificationError):
                detail = 'certificate verification failed'
            elif isinstance(exc, OSError):
                detail = 'OS error ' + str(getattr(exc, 'errno', 'unknown'))
            else:
                detail = 'internal error'
            logger.warning('WSS sync/reconnect: %s (%s)', type(exc).__name__, detail)
            if isinstance(exc,ssl.SSLCertVerificationError):last_connect_error='TLS_VERIFY_FAILED'
            elif isinstance(exc,PermissionError):last_connect_error='IDENTITY_MISMATCH'
            elif isinstance(exc,ConnectionRefusedError):last_connect_error='CONNECTION_REFUSED'
            elif isinstance(exc,TimeoutError):last_connect_error='CONNECTION_TIMED_OUT'
            elif isinstance(exc,WebsocketError):last_connect_error='WEBSOCKET_ERROR'
            elif isinstance(exc,OSError):last_connect_error='OS_ERROR'
            else:last_connect_error='UNKNOWN_ERROR'
            failure_count=min(failure_count+1,7)
            # A failed WSS connection must not make an otherwise healthy Agent
            # disappear from the dashboard. HTTPS fallback is heartbeat-only:
            # remote commands/uploads still require authenticated WSS.
            if failure_count >= 3 and config_path.is_file():
                try:
                    fallback_cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
                    if fallback_cfg.get('agent_id') and fallback_cfg.get('agent_key'):
                        fallback_ctx=ssl.create_default_context(cafile=fallback_cfg.get('ca_bundle'))
                        fallback_report=legacy.snapshot_versions(fallback_cfg)
                        fallback_report['guest_agent']=AGENT_VERSION
                        fallback_report['transport']='https-fallback'
                        fallback_report['remote_commands_allowed']=True
                        legacy.api(fallback_cfg['server_url'],'/agent/heartbeat','POST',
                            {'report':fallback_report},headers={
                                'X-Agent-ID':fallback_cfg['agent_id'],
                                'X-Agent-Key':fallback_cfg['agent_key']
                            },ssl_context=fallback_ctx)
                        logger.info('HTTPS heartbeat fallback OK; WSS remote operations unavailable')
                except Exception:
                    logger.warning('HTTPS heartbeat fallback unavailable')
        finally:
            for bridge in list(app_streams.values()):bridge['stop'].set()
            app_streams.clear()
            for flag in list(http_streams.values()):flag.set()
            http_streams.clear()
            if ws:
                try:ws.close()
                except OSError:pass
        stop.wait(min(60,2**min(failure_count,5))+random.random())


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',default=str(Path(os.environ.get('PROGRAMDATA','C:/ProgramData'))/'VMO'/'agent.json'))
    p.add_argument('--service',action='store_true')
    p.add_argument('--console',action='store_true')
    a=p.parse_args()
    if a.service:
        from windows_service import start_service
        start_service(lambda stop:run_live(Path(a.config),stop))
    else:
        run_live(Path(a.config))


if __name__=='__main__':main()
