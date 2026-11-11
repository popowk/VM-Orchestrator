"""Separate guest-facing API. MUST use HTTPS bound to one approved private host IPv4.

Run separately from the loopback-only admin app; never expose admin endpoints
on a guest-accessible interface.
"""
from __future__ import annotations
from pathlib import Path
import os
import asyncio
import json
import time
import uuid
import base64
import os
import re
import html
import json
import secrets
import logging
from urllib.parse import urlsplit
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Request, Response, Form
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from .remote_ops import RemoteStore, RemoteError
from .relay_limits import RelayStreamAdmission, RelayBusy
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .fleet import FleetStore, FleetError
from .agent_update import AgentUpdateManager, build_bootstrap, newer_release
from .config import Settings

class Enroll(BaseModel):
    vm_id: str
    one_time_token: str = Field(min_length=20, max_length=200)

class Heartbeat(BaseModel):
    report: dict = Field(default_factory=dict)

class TaskResult(BaseModel):
    succeeded: bool
    message: str = Field(max_length=800)


def create_guest_app(db_path: Path | None = None):
    settings = Settings.from_env()
    db_path = db_path or settings.db_path
    fleet = FleetStore(db_path)
    app = FastAPI(title='VMO Guest Agent Gateway', version='1.4.40', docs_url=None, redoc_url=None)
    artifact_root = db_path.parent / 'approved_packages'
    remote = RemoteStore(db_path)
    agent_updates = AgentUpdateManager(db_path, Path(__file__).resolve().parent.parent / 'guest_agent')
    auto_agent_update = os.environ.get('VMO_GUEST_AGENT_AUTO_UPDATE', '1').strip().lower() not in ('0','off','false','no')
    upgrade_logger = logging.getLogger('vmo.guest_agent_upgrade')

    def maybe_auto_upgrade(vm_id: str, report: dict):
        """Offer the bundled, authenticated, pinned Agent update to older V2 Guests.

        The initial transition works with existing 2.0.21 Agents: they already
        accept authorized WSS commands and have the verified rollback installer.
        Only the Host reads GitHub Releases; Guest never runs public URLs.
        """
        if not auto_agent_update or not isinstance(report, dict):
            return
        running = report.get('guest_agent')
        if not newer_release(running):
            return
        if not agent_updates.auto_retry_due(vm_id):
            return
        # Respect all queued/in-flight remote operations and software jobs.
        # Do not stop the Agent while a customer's command is running.
        with remote.db() as d:
            remote_busy = d.execute('''SELECT 1 FROM remote_guest_ops
                WHERE vm_id=? AND state IN ('QUEUED','CLAIMED','RUNNING') LIMIT 1''',
                (vm_id,)).fetchone()
            software_busy = d.execute('''SELECT 1 FROM guest_tasks
                WHERE vm_id=? AND status IN ('QUEUED','CLAIMED','RUNNING') LIMIT 1''',
                (vm_id,)).fetchone()
        if remote_busy or software_busy:
            return
        package = None
        try:
            package = agent_updates.prepare(vm_id, source='auto')
            command = build_bootstrap(package['id'], package['sha256'], package['version'])
            op_id = remote.queue(vm_id,'COMMAND',{
                'shell':'powershell','command':command,'privilege':'system',
                'confirmed':True,'run_mode':'wait','timeout_seconds':120},ttl=420)
            agent_updates.attach(package['id'],op_id)
            upgrade_logger.info('Queued auto Guest Agent upgrade for VM %s: %s to %s',
                                vm_id, running, package['version'])
        except Exception as exc:
            if package is not None:
                agent_updates.discard(package['id'])
            # No confidential URLs, credentials, or remote command text in logs.
            upgrade_logger.warning('Auto Guest Agent upgrade scheduling failed: %s',
                                   type(exc).__name__)

    # Per-process in-memory WSS fanout. Run one uvicorn worker, no reload.
    active = {}
    # Internal only: allows offline regression tests to simulate a Guest WSS link.
    app.state._guest_app_channels = active
    MAX_RELAY_REQ=512*1024
    MAX_RELAY_RESP=350*1024
    MAX_BUFFERED_API_REPLY=8*1024*1024
    client_origin=os.environ.get('VMO_PUBLIC_CLIENT_ORIGIN','').rstrip('/')
    if client_origin and (not client_origin.startswith('https://') or any(c in client_origin for c in '\r\n; ')):
        raise RuntimeError('VMO_PUBLIC_CLIENT_ORIGIN must be a single HTTPS origin')
    def ws_limit():
        try:return max(1,min(64,int(os.environ.get('VMO_APP_WS_CONNECTIONS','32'))))
        except ValueError:return 32
    def app_authorize(relay_id,app_id,cookie):
        try:return remote.resolve_app_session(relay_id,app_id,cookie)
        except RemoteError:raise HTTPException(401,'Application session expired; reopen from customer portal')
    def gateway_cookie(request,relay_id):
        return request.cookies.get('vmo_app_'+relay_id,'')
    def safe_path(p):
        if '\x00' in p or '\\' in p or any(x=='..' for x in p.split('/')) or p.startswith('//'):
            raise HTTPException(400,'Invalid application path')
        return p

    @app.post('/apps/open', include_in_schema=False)
    async def app_open(request: Request):
        # Ticket never appears in URLs, Referer or the application HTTP logs.
        if request.headers.get('content-length','0').isdigit() and int(request.headers['content-length'])>1024:
            raise HTTPException(413,'Launch request too large')
        from urllib.parse import parse_qs
        form=parse_qs((await request.body()).decode('ascii','ignore'))
        ticket=form.get('ticket',[''])[0]
        try:relay_id,app_id,secret=remote.exchange_app_launch(ticket)
        except RemoteError as ex:raise HTTPException(401,str(ex))
        path=f'/apps/{relay_id}/{app_id}/'
        reply=RedirectResponse(path,status_code=303)
        reply.set_cookie('vmo_app_'+relay_id,secret,httponly=True,secure=True,
                         samesite='none',max_age=3*3600,path=f'/apps/{relay_id}')
        reply.headers['Cache-Control']='no-store'
        reply.headers['Referrer-Policy']='no-referrer'
        return reply

    def rewrite_html(blob,root):
        """Conservative relative/root-absolute resource URL rewriting.
        Some SPA JS hardcodes root URLs; full support requires app-specific adapters.
        """
        data=blob.decode('utf-8','replace')
        def prefix(m): return m.group(1)+root
        data=re.sub(r'((?:href|src|action|poster)\s*=\s*[\"\'])/(?!/)',prefix,data,flags=re.I)
        # Minimal HTML from the standalone DSH login bridge may have no
        # <head>. Previously we skipped all URL rewriting for those documents,
        # so a POST to /__bridge/unlock hit the 8765 admin app and returned 404.
        # Always insert the shim before any application scripts run.
        # Unscoped /__bridge routes are NOT installed on the Host: they cannot
        # be mapped to a VM safely when several customer iframes are open.
        shim = r"""<script>(function(){
         'use strict';const base=__BASE__;
         // Contract for applications that can mount their router under a base path.
         // This must run before the application's own scripts.
         const basePath=base.replace(/\/$/,'');
         try{
           Object.defineProperty(window,'__VMO_APP_BASE_PATH__',{
             value:basePath,configurable:false,writable:false
           });
           Object.defineProperty(window,'__VMO_APP_ORIGIN__',{
             value:location.origin,configurable:false,writable:false
           });
         }catch(e){}
         
         // A standalone DSH tab may be navigated to '/' by application code.
         // A browser-window marker lets the root page recover this tenant path
         // without guessing from a global /__bridge route or sharing sessions.
         try{if(/^\/apps\/[A-Za-z0-9_-]+\/(snowluma|dsh-fish-soul)\/$/.test(base))
               window.name='vmo-route:'+base;}catch(e){}
         function rewrite(input){
           if(input instanceof URL)input=input.href;
           if(typeof input!=='string')return input;
           if(input.startsWith(base))return input;
           if(input.startsWith('/')&&!input.startsWith('//'))return base+input.slice(1);
           try{const x=new URL(input,location.href);
             if(x.hostname==='localhost'||x.hostname==='127.0.0.1'){
               return base+x.pathname.replace(/^\//,'')+x.search+x.hash;
             }
             if(x.origin===location.origin&&x.pathname.startsWith('/')&&!x.pathname.startsWith(base)){
               return base+x.pathname.slice(1)+x.search+x.hash;
             }
           }catch(e){}return input;
         }
         // SPA navigations must remain under this instance's scoped path.
         if(window.history && window.history.pushState && window.history.replaceState){
           const h=window.history;
           const push=h.pushState.bind(h),replace=h.replaceState.bind(h);
           h.pushState=function(st,title,url){return push(st,title,url==null?url:rewrite(String(url)));};
           h.replaceState=function(st,title,url){return replace(st,title,url==null?url:rewrite(String(url)));};
         }
         const originalOpen=window.open;
         if(originalOpen)window.open=function(url,...args){return originalOpen.call(window,rewrite(url),...args);};
         const f=window.fetch.bind(window);
         window.fetch=function(input,opt){
           if(input instanceof Request){const u=rewrite(input.url);if(u!==input.url)input=new Request(u,input);}
           else input=rewrite(input);return f(input,opt);
         };
         const open=XMLHttpRequest.prototype.open;
         XMLHttpRequest.prototype.open=function(method,url,...rest){return open.call(this,method,rewrite(url),...rest);};
         // A native form POST bypasses fetch() and XHR. Rewrite forms at
         // submission time, including dynamically created/login forms.
         function formAction(form){
           if(!form || !form.getAttribute)return;
           const action=form.getAttribute('action');
           if(action){const url=rewrite(action);if(url!==action)form.action=url;}
         }
         document.addEventListener('submit',e=>formAction(e.target),true);
         document.addEventListener('click',e=>{
           const a=e.target&&e.target.closest?e.target.closest('a[href]'):null;
           if(a){const href=a.getAttribute('href');const scoped=rewrite(href);
             if(scoped!==href)a.href=scoped;}
         },true);
         const originalSubmit=HTMLFormElement.prototype.submit;
         HTMLFormElement.prototype.submit=function(){formAction(this);return originalSubmit.call(this);};
         if(HTMLFormElement.prototype.requestSubmit){
           const originalRequestSubmit=HTMLFormElement.prototype.requestSubmit;
           HTMLFormElement.prototype.requestSubmit=function(...args){formAction(this);return originalRequestSubmit.apply(this,args);};
         }
         const RealWS=window.WebSocket;
         function RoutedWebSocket(input,protocols){
           const u=new URL(rewrite(String(input)),location.href);
           if(u.protocol==='http:')u.protocol='ws:';
           else if(u.protocol==='https:')u.protocol='wss:';
           return protocols===undefined?new RealWS(u.href):new RealWS(u.href,protocols);
         }
         RoutedWebSocket.prototype=RealWS.prototype;
         Object.setPrototypeOf(RoutedWebSocket,RealWS);
         window.WebSocket=RoutedWebSocket;
         if(window.EventSource){const RealES=window.EventSource;
           function RoutedEventSource(url,options){return new RealES(rewrite(String(url)),options);}
           RoutedEventSource.prototype=RealES.prototype;
           window.EventSource=RoutedEventSource;
         }
        })();</script>""".replace('__BASE__',json.dumps(root))
        if '<base' not in data.lower():
            shim='<meta name="vmo-app-base-path" content="'+html.escape(root.rstrip('/'),quote=True)+'"><base href="'+html.escape(root,quote=True)+'">'+shim
        if re.search(r'<head(?:\s|>)',data,re.I):
            data=re.sub(r'(<head[^>]*>)',lambda m:m.group(1)+shim,data,count=1,flags=re.I)
        elif re.search(r'<html(?:\s|>)',data,re.I):
            data=re.sub(r'(<html[^>]*>)',lambda m:m.group(1)+'<head>'+shim+'</head>',data,count=1,flags=re.I)
        else:
            # HTML fragments also need the login bridge shim before scripts.
            data='<head>'+shim+'</head>'+data
        return data.encode('utf-8')

    @app.api_route('/apps/{relay_id}/{app_id}/{part:path}',
                   methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'],include_in_schema=False)
    async def app_relay(relay_id:str,app_id:str,part:str,request:Request):
        vm=app_authorize(relay_id,app_id,gateway_cookie(request,relay_id))
        connection=active.get(vm)
        if not connection:raise HTTPException(503,'Guest WSS tunnel offline')
        payload=await request.body()
        if len(payload)>MAX_RELAY_REQ:raise HTTPException(413,'Request exceeds application relay limit')
        safe_path('/'+part)
        uid=str(uuid.uuid4())
        # Modern SPA pages request dozens of hashed assets in parallel. The old
        # `len(streams)>=4 -> HTTP 429` rejected ordinary JS imports and caused
        # blank pages. Allocate up to 16 asset streams and 8 long-lived SSE
        # streams, then WAIT for capacity rather than rejecting valid bursts.
        is_events=(request.method=='GET' and
            'text/event-stream' in request.headers.get('accept','').lower())
        is_asset=(request.method=='GET' and
            re.search(r'\.(?:js|mjs|css|map|wasm|png|jpg|jpeg|webp|avif|gif|svg|ico|woff2?|ttf|otf|mp4|webm|mp3|ogg)(?:$|\?)',part,re.I) is not None)
        # Application API replies can exceed the legacy 350 KiB single-frame cap.
        # Buffer only bounded API responses; static assets and SSE remain streamed.
        is_api_response=(not is_events and (request.method not in ('GET','HEAD')
                         or part.startswith(('api/','__bridge/'))))
        is_stream=is_events or is_asset or is_api_response
        lease=None
        loop=asyncio.get_running_loop();future=loop.create_future()
        if is_stream:
            try:
                lease=await connection['stream_admission'].acquire('events' if is_events else 'assets')
            except RelayBusy:
                raise HTTPException(503,'Application stream capacity busy; try again shortly',
                                    headers={'Retry-After':'2'})
            # A queued request must not attach to a superseded Guest WSS link.
            if active.get(vm) is not connection:
                lease.release()
                raise HTTPException(503,'Guest WSS tunnel reconnected; reload this resource')
            q=asyncio.Queue(maxsize=90)
            connection['streams'][uid]=q
        else:connection['pending'][uid]=future
        headers={k:v for k,v in request.headers.items() if k.lower() in (
            'accept','content-type','cookie','authorization','range','x-requested-with','accept-language',
            'x-snowluma-token')}
        if app_id!='snowluma':headers.pop('x-snowluma-token',None)
        headers['x-forwarded-prefix']=f'/apps/{relay_id}/{app_id}'
        headers['x-vmo-app-base-path']=headers['x-forwarded-prefix']
        # No Host value from browser is forwarded to a Guest localhost app.
        # Apps should use the prefix to generate their own routes/redirects.
        headers['cookie']='; '.join(p.strip() for p in headers.get('cookie','').split(';')
                                     if p.strip() and not p.strip().startswith('vmo_app_'))
        raw_path='/'+part
        if request.url.query:raw_path+='?'+request.url.query
        try:
            await connection['send']({'type':'app.http.request','request':{
                'id':uid,'app_id':app_id,'method':request.method,'path':raw_path,
                'headers':headers,'body_b64':base64.b64encode(payload).decode('ascii'),
                'stream':is_stream}})
            result=await asyncio.wait_for(q.get() if is_stream else future,timeout=50 if is_api_response else 24)
        except asyncio.TimeoutError:
            if lease:
                connection['streams'].pop(uid,None)
                lease.release()
            raise HTTPException(504,'Application relay timed out; do not automatically replay POST')
        except (OSError,RuntimeError):
            if lease:
                connection['streams'].pop(uid,None)
                lease.release()
            raise HTTPException(503,'Guest WSS tunnel lost')
        except asyncio.CancelledError:
            if lease:
                connection['streams'].pop(uid,None)
                lease.release()
            raise
        finally:
            if not is_stream:connection['pending'].pop(uid,None)
        if is_stream:
            if result.get('type')!='app.http.stream_start':
                connection['streams'].pop(uid,None)
                lease.release()
                raise HTTPException(502,'Guest application stream unavailable: '+
                                    str(result.get('error','START_FAILED'))[:70])
            if is_api_response:
                # Consume response in pieces without assuming API responses fit in
                # one WSS frame. Never replay POST after a timeout/disconnect.
                try:
                    chunks=[];total=0;complete=False
                    deadline=loop.time()+60
                    while loop.time()<deadline:
                        event=await asyncio.wait_for(q.get(),timeout=min(20,max(.01,deadline-loop.time())))
                        kind=event.get('type')
                        if kind=='app.http.stream_end':complete=True;break
                        if kind=='app.http.stream_error':
                            raise HTTPException(502,'Guest API response failed: '+str(event.get('error','UPSTREAM_ERROR'))[:70])
                        if kind!='app.http.stream_data':continue
                        try:piece=base64.b64decode(event.get('body_b64',''),validate=True)
                        except (ValueError,TypeError):raise HTTPException(502,'Invalid Guest API response chunk')
                        total+=len(piece)
                        if total>MAX_BUFFERED_API_REPLY:raise HTTPException(502,'API response exceeds 8 MiB relay limit')
                        chunks.append(piece)
                    if not complete:raise HTTPException(504,'Guest API response incomplete; do not retry POST automatically')
                    response={'body_b64':base64.b64encode(b''.join(chunks)).decode('ascii'),
                        'status':result.get('status',502),'headers':result.get('headers') or {},
                        'set_cookies':result.get('set_cookies') or []}
                    result={'ok':True,'response':response}
                except asyncio.TimeoutError:
                    raise HTTPException(504,'Guest API response timed out; do not automatically replay POST')
                finally:
                    connection['streams'].pop(uid,None)
                    lease.release()
                    try:await connection['send']({'type':'app.http.cancel','id':uid})
                    except (OSError,RuntimeError):pass
            else:
                if result.get('status')!=200:
                    connection['streams'].pop(uid,None)
                    lease.release()
                    status=result.get('status')
                    raise HTTPException(status if isinstance(status,int) and 400<=status<600 else 502,
                                        'Application stream returned HTTP '+str(status))
            if is_api_response:
                pass # Fall through into the common cookie/Location/JSON response adapter.
            else:
                async def stream_body():
                    try:
                        while True:
                            try:event=await asyncio.wait_for(q.get(),timeout=35)
                            except asyncio.TimeoutError:break
                            if event.get('type') in ('app.http.stream_end','app.http.stream_error'):break
                            if event.get('type')!='app.http.stream_data':continue
                            try:yield base64.b64decode(event.get('body_b64',''),validate=True)
                            except (ValueError,TypeError):break
                    finally:
                        connection['streams'].pop(uid,None)
                        lease.release()
                        try:await connection['send']({'type':'app.http.cancel','id':uid})
                        except (OSError,RuntimeError):pass
                response_stream=StreamingResponse(stream_body(),media_type=result.get('content_type','application/octet-stream'),headers={
                    'Cache-Control':'no-store','X-Accel-Buffering':'no','Referrer-Policy':'no-referrer'})
                return response_stream
        if not result.get('ok'):raise HTTPException(502,'Guest application: '+str(result.get('error','request failed'))[:100])
        response=result.get('response') or {}
        try: body=base64.b64decode(response.get('body_b64',''),validate=True)
        except (ValueError,TypeError):raise HTTPException(502,'Invalid app response')
        if len(body)>(MAX_BUFFERED_API_REPLY if is_api_response else MAX_RELAY_RESP):
            raise HTTPException(502,'Response exceeds relay limit')
        status=int(response.get('status',502))
        if not 100<status<600:status=502
        prefix=f'/apps/{relay_id}/{app_id}/'
        hdr=response.get('headers') or {}
        content_type=hdr.get('Content-Type','application/octet-stream')
        if 'text/html' in content_type.lower():
            # HTML can contain a meta refresh, inline link or literal localhost
            # location assignment that bypasses fetch/XHR interception.
            from .same_origin_apps import rewrite_scoped_asset
            body=rewrite_scoped_asset(body,prefix,app_id,'text/html',part)
            body=rewrite_html(body,prefix)
        elif 'text/css' in content_type.lower():
            body=re.sub(rb'url\(\s*([\"\']?)/(?!/)',
                lambda m:b'url('+m.group(1)+prefix.encode('ascii'),body,flags=re.I)
        reply=Response(content=body,status_code=status,media_type=None)
        reply.headers['Content-Type']=content_type
        reply.headers['Cache-Control']='no-store'
        reply.headers['Referrer-Policy']='no-referrer'
        reply.headers['X-Content-Type-Options']='nosniff'
        location=hdr.get('Location','')
        if location:
            loc=urlsplit(location)
            if not loc.scheme and not loc.netloc:
                reply.headers['Location']=prefix+location.lstrip('/')
            elif loc.hostname in ('127.0.0.1','localhost'):
                reply.headers['Location']=prefix+loc.path.lstrip('/')+('?' + loc.query if loc.query else '')
            elif loc.scheme=='https':reply.headers['Location']=location
        # Upstream app session cookie is scoped to its own relay path, never root.
        for entry in response.get('set_cookies',[])[:12]:
            if not isinstance(entry,str) or '\r' in entry or '\n' in entry:continue
            parts=[part.strip() for part in entry.split(';') if part.strip()]
            if not parts or '=' not in parts[0] or parts[0].split('=',1)[0].startswith('vmo_app_'):continue
            clean=[parts[0]]+[x for x in parts[1:] if x.lower().split('=',1)[0] not in ('path','domain','samesite','secure')]
            # TLS on 8766 is mandatory. Session isolation is enforced by cookie path.
            clean.extend(['Path='+prefix,'Secure','SameSite=None'])
            reply.headers.append('Set-Cookie','; '.join(clean))
        if 'text/html' in content_type.lower():
            reply.headers['Content-Security-Policy']='frame-ancestors '+("'self' " + client_origin if client_origin else "'self'")
        return reply


    @app.websocket('/apps/{relay_id}/{app_id}/{part:path}')
    async def app_websocket(relay_id:str,app_id:str,part:str,ws:WebSocket):
        # A browser may only open an app socket for its already-issued, bound
        # relay session. No direct VM IP or alternative app_id is accepted.
        try:
            vm=remote.resolve_app_session(relay_id,app_id,ws.cookies.get('vmo_app_'+relay_id,''))
        except RemoteError:
            await ws.close(code=1008);return
        origin=ws.headers.get('origin','')
        expected='https://'+ws.headers.get('host','')
        if origin and origin!=expected:
            await ws.close(code=1008);return
        try:safe_path('/'+part)
        except HTTPException:
            await ws.close(code=1008);return
        channel=active.get(vm)
        if not channel or len(channel['sockets'])>=ws_limit():
            await ws.close(code=1013);return
        await ws.accept()
        uid=str(uuid.uuid4());events=asyncio.Queue(maxsize=128)
        channel['sockets'][uid]=events
        cookies='; '.join(item.strip() for item in ws.headers.get('cookie','').split(';')
                          if item.strip() and not item.strip().startswith('vmo_app_'))
        path='/'+part
        if ws.url.query:path+='?'+ws.url.query
        async def send_guest(msg):
            await channel['send'](msg)
        try:
            await send_guest({'type':'app.ws.open','request':{
                'id':uid,'app_id':app_id,'path':path,'headers':{'cookie':cookies,
                'protocol':ws.headers.get('sec-websocket-protocol','')[:160]}}})
            try:opened=await asyncio.wait_for(events.get(),timeout=12)
            except asyncio.TimeoutError:
                await ws.close(code=1011);return
            if opened.get('type')!='app.ws.opened' or not opened.get('ok'):
                await ws.close(code=1011);return
            while True:
                inbound=asyncio.create_task(ws.receive())
                outbound=asyncio.create_task(events.get())
                done,pending=await asyncio.wait((inbound,outbound),return_when=asyncio.FIRST_COMPLETED)
                for job in pending:job.cancel()
                if inbound in done:
                    event=inbound.result()
                    if event.get('type')=='websocket.disconnect':break
                    raw=event.get('bytes')
                    if raw is None:
                        text=event.get('text')
                        if not isinstance(text,str):break
                        raw=text.encode('utf-8');mode='text'
                    else:mode='bytes'
                    if len(raw)>96*1024:
                        await ws.close(code=1009);break
                    await send_guest({'type':'app.ws.client','id':uid,'mode':mode,
                                      'body_b64':base64.b64encode(raw).decode('ascii')})
                if outbound in done:
                    message=outbound.result()
                    if message.get('type')=='app.ws.closed':break
                    if message.get('type')!='app.ws.data':continue
                    try:raw=base64.b64decode(message.get('body_b64',''),validate=True)
                    except (ValueError,TypeError):break
                    if message.get('mode')=='text':await ws.send_text(raw.decode('utf-8','replace'))
                    else:await ws.send_bytes(raw)
        except (RuntimeError,OSError,WebSocketDisconnect,asyncio.CancelledError):
            pass
        finally:
            channel['sockets'].pop(uid,None)
            try:await send_guest({'type':'app.ws.close','id':uid})
            except (RuntimeError,OSError):pass
            try:await ws.close()
            except (RuntimeError,OSError):pass

    @app.get('/agent/health')
    def health():
        return {'status':'ok','service':'guest-only'}

    @app.post('/agent/enroll')
    def enroll(body: Enroll):
        try:
            return fleet.enroll(body.vm_id, body.one_time_token)
        except FleetError as ex:
            raise HTTPException(403, str(ex))

    def agent(x_agent_id: str | None, x_agent_key: str | None) -> str:
        try:
            return fleet.authorized_vm(x_agent_id or '', x_agent_key or '')
        except FleetError:
            raise HTTPException(401, 'Guest Agent authentication failed')

    @app.post('/agent/heartbeat')
    def heartbeat(body: Heartbeat, x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        vm = agent(x_agent_id,x_agent_key)
        try: fleet.heartbeat(vm, body.report)
        except FleetError as ex: raise HTTPException(400,str(ex))
        return {'ok':True}

    @app.get('/agent/tasks/next')
    def next_task(x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        return {'task':fleet.claim(agent(x_agent_id,x_agent_key))}

    @app.post('/agent/tasks/{task_id}/result')
    def task_result(task_id: str, body: TaskResult, x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        vm = agent(x_agent_id,x_agent_key)
        try: fleet.report_task(vm,task_id,body.succeeded,body.message)
        except FleetError as ex: raise HTTPException(409,str(ex))
        return {'ok':True}

    @app.get('/agent/artifacts/{package_id}')
    def artifact(package_id: str, x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        agent(x_agent_id,x_agent_key)
        pkg = fleet.package(package_id)
        # Never accept a file path from the request; resolve a database ID only.
        if not pkg: raise HTTPException(404, 'Package not found')
        path = artifact_root / (pkg['id'] + '.zip')
        if not path.is_file(): raise HTTPException(404, 'Package bytes are missing')
        return FileResponse(path, media_type='application/zip', filename='approved-update.zip', headers={'Cache-Control':'no-store'})

    @app.get('/agent/agent-updates/{update_id}')
    def guest_agent_update_package(update_id: str, x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        vm = agent(x_agent_id,x_agent_key)
        try:
            from .agent_update import AgentUpdateManager
            package = AgentUpdateManager(db_path, Path(__file__).resolve().parent.parent / 'guest_agent').package_for_vm(vm, update_id)
        except (ValueError,FileNotFoundError):
            raise HTTPException(404,'Agent update package not available for this VM')
        return FileResponse(package, media_type='application/zip', headers={'Cache-Control':'no-store'})

    @app.get('/agent/uploads/{operation_id}')
    def guest_upload(operation_id: str, x_agent_id: str | None = Header(default=None), x_agent_key: str | None = Header(default=None)):
        vm = agent(x_agent_id,x_agent_key)
        try:
            path=remote.upload_file(vm,operation_id)
        except RemoteError as exc: raise HTTPException(404,str(exc))
        return FileResponse(path, media_type='application/octet-stream',headers={'Cache-Control':'no-store'})

    @app.websocket('/agent/ws')
    async def guest_live(ws: WebSocket):
        """Guest-initiated WSS only. Never put secrets in URL parameters."""
        try:
            vm=agent(ws.headers.get('x-agent-id'),ws.headers.get('x-agent-key'))
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        connection_id=str(uuid.uuid4())
        remote.link_connected(vm,connection_id)
        send_lock=asyncio.Lock()
        async def send_event(event):
            async with send_lock:
                await ws.send_json(event)
        channel={'send':send_event,'pending':{},'sockets':{},'streams':{},
                 'stream_admission':RelayStreamAdmission()}
        old=active.get(vm)
        if old:
            for pending in old['pending'].values():
                if not pending.done():pending.set_exception(RuntimeError('Replaced WSS link'))
        if old:
            for events in old['sockets'].values():
                try:events.put_nowait({'type':'app.ws.closed'})
                except asyncio.QueueFull:pass
        if old:
            for events in old['streams'].values():
                try:events.put_nowait({'type':'app.http.stream_end'})
                except asyncio.QueueFull:pass
        active[vm]=channel
        last_heartbeat = 0.0
        inflight = None
        last_checked = 0.0
        await send_event({'type':'connected','vm_id':vm,'heartbeat_seconds':30})
        try:
            while True:
                # A claim lost over a broken socket must not pin a VM forever.
                # Only diagnostics may be safely replayed; commands are never retried.
                tick=time.monotonic()
                if tick-last_checked>5:
                    remote.recover_stale(vm)
                    if inflight:
                        prior=remote.get(inflight)
                        if prior and prior['state'] in ('QUEUED','TIMED_OUT','EXPIRED','SUCCEEDED','FAILED'):
                            inflight=None
                    last_checked=tick
                # Persist every operation before sending; client must acknowledge outcome.
                if inflight is None:
                    op=remote.claim(vm)
                    if op:
                        inflight=op['id']
                        await send_event({'type':'operation','operation':op})
                    else:
                        task=fleet.claim(vm)
                        if task:
                            inflight=task['id']
                            await send_event({'type':'software_update','task':task})
                try:
                    event=await asyncio.wait_for(ws.receive_json(),timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(event,dict): continue
                kind=event.get('type')
                if kind=='heartbeat':
                    report=event.get('report',{})
                    try: fleet.heartbeat(vm,report)
                    except FleetError:
                        remote.link_client_error(vm,'IDENTITY_MISMATCH')
                        await ws.close(code=1008)
                        return
                    last_heartbeat=time.monotonic()
                    remote.link_heartbeat(vm,connection_id)
                    # Auto-updates are optional maintenance: a packaging/database
                    # error must NEVER kill the authenticated WSS connection.
                    try:
                        maybe_auto_upgrade(vm, report)
                    except Exception as exc:
                        upgrade_logger.warning('Auto Agent check deferred: %s',type(exc).__name__)
                    await send_event({'type':'heartbeat_ack'})
                elif kind=='operation.started':
                    oid=event.get('id','')
                    if remote.started(vm,oid):
                        await send_event({'type':'operation.start_ack','id':oid})
                elif kind=='operation.result':
                    try:
                        remote.complete(vm,event.get('id',''),event.get('ok',False),event.get('result',{}))
                        if inflight==event.get('id'):inflight=None
                        await send_event({'type':'operation.ack','id':event.get('id')})
                    except RemoteError: pass
                elif kind=='software.progress':
                    try:fleet.report_phase(vm,event.get('id',''),event.get('phase',''))
                    except FleetError:pass
                elif kind=='software.result':
                    try:
                        fleet.report_task(vm,event.get('id',''),bool(event.get('ok')),str(event.get('message',''))[:800])
                        if inflight==event.get('id'):inflight=None
                        await send_event({'type':'software.ack','id':event.get('id')})
                    except FleetError: pass
                elif kind=='client.identity_error':
                    remote.link_client_error(vm,'IDENTITY_MISMATCH')
                elif kind=='client.reconnect':
                    remote.link_client_error(vm,event.get('reason','UNKNOWN_ERROR'))
                elif kind=='apps.report':
                    try: remote.app_report(vm,event.get('apps',[]))
                    except RemoteError: pass
                elif kind=='app.secret':
                    try: remote.app_secret(vm,event.get('app_id'),event.get('secret'),event.get('reported_at'))
                    except (RemoteError,RuntimeError,ValueError): pass
                elif kind in ('app.ws.opened','app.ws.data','app.ws.closed'):
                    events=channel['sockets'].get(event.get('id',''))
                    if events:
                        try:events.put_nowait(event)
                        except asyncio.QueueFull:
                            try:events.put_nowait({'type':'app.ws.closed'})
                            except asyncio.QueueFull:pass
                elif kind in ('app.http.stream_start','app.http.stream_data','app.http.stream_end','app.http.stream_error'):
                    events=channel['streams'].get(event.get('id',''))
                    if events:
                        try:events.put_nowait(event)
                        except asyncio.QueueFull:
                            try:events.put_nowait({'type':'app.http.stream_end'})
                            except asyncio.QueueFull:pass
                elif kind=='app.http.response':
                    pending=channel['pending'].get(event.get('id',''))
                    if pending and not pending.done():pending.set_result(event)
                elif kind=='ping':
                    await send_event({'type':'pong'})
        except WebSocketDisconnect as ex:
            reason='websocket_close_'+str(ex.code)
            return
        except (RuntimeError,OSError) as ex:
            reason=type(ex).__name__
            return
        finally:
            if active.get(vm) is channel:active.pop(vm,None)
            for events in channel['sockets'].values():
                try:events.put_nowait({'type':'app.ws.closed'})
                except asyncio.QueueFull:pass
            for events in channel['streams'].values():
                try:events.put_nowait({'type':'app.http.stream_end'})
                except asyncio.QueueFull:pass
            for pending in channel['pending'].values():
                if not pending.done():pending.set_exception(RuntimeError('WSS disconnected'))
            remote.link_disconnected(vm,connection_id,locals().get('reason','connection_ended'))

    @app.middleware('http')
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp
    return app


# Start via: uvicorn app.guest_api:create_guest_app --factory
# Do not create a database on module import (helps offline test safety).
