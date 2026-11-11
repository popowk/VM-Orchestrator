"""Bounded localhost-only HTTP relay for the authenticated per-VM WSS channel.

The Guest initiates WSS; the host never connects to a Guest service or LAN IP.
The relay is not an open proxy: approved application IDs map to immutable,
locally registered http://127.0.0.1:<port>/ endpoints only.
"""
from __future__ import annotations
import base64
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit
try:
    from .app_registry import available_registry
except ImportError:
    from app_registry import available_registry

MAX_BODY=512*1024
MAX_REPLY=350*1024
HEADERS={'accept','content-type','cookie','authorization','range','x-requested-with','accept-language','x-snowluma-token','x-forwarded-prefix','x-vmo-app-base-path'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def local_http(root,request):
    apps=available_registry(root)
    app=request.get('app_id')
    if app not in ('snowluma','dsh-fish-soul') or app not in apps:
        raise ValueError('APP_NOT_REGISTERED')
    source=apps[app]['url']
    path=request.get('path','/')
    try:
        from .agent_v2 import safe_path_fragment
    except ImportError:
        from agent_v2 import safe_path_fragment
    safe=safe_path_fragment(path)
    method=request.get('method','GET').upper()
    if method not in ('GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'):
        raise ValueError('UNSUPPORTED_METHOD')
    body=base64.b64decode(request.get('body_b64',''),validate=True)
    if len(body)>MAX_BODY:raise ValueError('BODY_TOO_LARGE')
    target=source.rstrip('/')+safe
    incoming=request.get('headers') or {}
    if not isinstance(incoming,dict):raise ValueError('INVALID_HEADERS')
    forwarded={k:v for k,v in incoming.items() if k.lower() in HEADERS and isinstance(v,str)
               and len(v)<8192 and '\r' not in v and '\n' not in v}
    forwarded['Accept-Encoding']='identity'
    # Browser origin is the host gateway; upstream application expects its own origin.
    forwarded['Origin']=source.rstrip('/')
    forwarded['Referer']=source.rstrip('/')+'/'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req=urllib.request.Request(target, data=body if method not in ('GET','HEAD') else None,
                               headers=forwarded,method=method)
    try:result=opener.open(req,timeout=18)
    except urllib.error.HTTPError as ex:result=ex
    with result as r:
        content=r.read(MAX_REPLY+1)
        if len(content)>MAX_REPLY:raise ValueError('RESPONSE_TOO_LARGE')
        header_names=['Content-Type','Location','Cache-Control','Content-Disposition','Content-Encoding']
        headers={k:r.headers.get(k,'')[:500] for k in header_names if r.headers.get(k)}
        cookies=[x[:4000] for x in (r.headers.get_all('Set-Cookie') or [])[:12]]
        return {'status':int(r.status),'headers':headers,'set_cookies':cookies,
                'body_b64':base64.b64encode(content).decode('ascii')}

def local_http_reply(root,req):
    ident=req.get('id','')
    try:
        if not isinstance(ident,str) or len(ident)!=36:raise ValueError('INVALID_REQUEST_ID')
        result=local_http(root,req)
        return {'type':'app.http.response','id':ident,'ok':True,'response':result}
    except (ValueError,urllib.error.URLError,OSError) as e:
        return {'type':'app.http.response','id':ident,'ok':False,
                'error': type(e).__name__ if not isinstance(e,ValueError) else str(e)[:90]}

# Browser application WebSocket frames are relayed independently of remote
# commands. This is a narrow localhost-only RFC6455 bridge, not an HTTP CONNECT
# tunnel. It supports text and binary frames up to 96 KiB; oversized or
# fragmented application messages fail closed rather than exhausting memory.
import hashlib
import os
import queue
import select
import socket
import threading
from urllib.parse import urlsplit
MAX_WS_PAYLOAD=96*1024

class LocalAppWebSocket:
    def __init__(self,root,request):
        apps=available_registry(root)
        app=request.get('app_id')
        if app not in ('snowluma','dsh-fish-soul') or app not in apps:
            raise ValueError('APP_NOT_REGISTERED')
        try:
            from .agent_v2 import safe_path_fragment
        except ImportError:
            from agent_v2 import safe_path_fragment
        path=safe_path_fragment(request.get('path','/'))
        uri=urlsplit(apps[app]['url'])
        s=socket.create_connection(('127.0.0.1',uri.port),timeout=8)
        s.settimeout(8)
        self.sock=s
        self.lock=threading.RLock()
        key=base64.b64encode(os.urandom(16)).decode('ascii')
        cookie=(request.get('headers') or {}).get('cookie','')
        if not isinstance(cookie,str) or len(cookie)>8192 or '\r' in cookie or '\n' in cookie:
            raise ValueError('INVALID_WS_COOKIE')
        lines=[f'GET {path} HTTP/1.1',f'Host: 127.0.0.1:{uri.port}',
               'Upgrade: websocket','Connection: Upgrade',
               'Sec-WebSocket-Version: 13',f'Sec-WebSocket-Key: {key}',
               f'Origin: http://127.0.0.1:{uri.port}']
        if cookie:lines.append('Cookie: '+cookie)
        protocols=(request.get('headers') or {}).get('protocol','')
        if protocols and isinstance(protocols,str) and len(protocols)<160 and all(c.isalnum() or c in '_-., ' for c in protocols):
            lines.append('Sec-WebSocket-Protocol: '+protocols)
        s.sendall(('\r\n'.join(lines)+'\r\n\r\n').encode('ascii'))
        header=bytearray()
        while b'\r\n\r\n' not in header:
            fragment=s.recv(4096)
            if not fragment or len(header)+len(fragment)>16000:raise OSError('WS_HANDSHAKE_FAILED')
            header.extend(fragment)
        head,remain=bytes(header).split(b'\r\n\r\n',1)
        lines=head.decode('latin1').split('\r\n')
        if not lines[0].startswith('HTTP/1.1 101 '):raise OSError('LOCAL_WS_REJECTED')
        h={}
        for line in lines[1:]:
            if ':' in line:
                k,v=line.split(':',1);h[k.strip().lower()]=v.strip()
        expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if h.get('sec-websocket-accept')!=expected:raise OSError('WS_HANDSHAKE_INVALID')
        self.pending=bytearray(remain)
        s.settimeout(5)

    def _exact(self,n):
        while len(self.pending)<n:
            part=self.sock.recv(min(65536,n-len(self.pending)))
            if not part:raise OSError('LOCAL_WS_CLOSED')
            self.pending.extend(part)
        result=bytes(self.pending[:n]);del self.pending[:n]
        return result

    def receive(self):
        head=self._exact(2)
        opcode=head[0]&15
        if not head[0]&0x80:raise OSError('FRAGMENTED_WS_NOT_SUPPORTED')
        if head[1]&0x80:raise OSError('LOCAL_SERVER_MUST_NOT_MASK')
        n=head[1]&127
        if n==126:n=int.from_bytes(self._exact(2),'big')
        elif n==127:n=int.from_bytes(self._exact(8),'big')
        if n>MAX_WS_PAYLOAD:raise OSError('LOCAL_WS_MESSAGE_TOO_LARGE')
        return opcode,self._exact(n)

    def send(self,opcode,payload=b''):
        if not isinstance(payload,bytes) or len(payload)>MAX_WS_PAYLOAD:
            raise ValueError('WS_PAYLOAD_TOO_LARGE')
        mask=os.urandom(4)
        n=len(payload)
        header=bytes([0x80|(opcode&15)])
        if n<126:header+=bytes([0x80|n])
        elif n<65536:header+=bytes([0x80|126])+n.to_bytes(2,'big')
        else:header+=bytes([0x80|127])+n.to_bytes(8,'big')
        encrypted=bytes(b^mask[i%4] for i,b in enumerate(payload))
        with self.lock:self.sock.sendall(header+mask+encrypted)

    def close(self):
        try:self.send(8,b'')
        except (OSError,ValueError):pass
        try:self.sock.close()
        except OSError:pass


def ws_open_worker(root,request,streams,outbox):
    uid=request.get('id','')
    if not isinstance(uid,str) or len(uid)!=36:return
    conn=None
    entry={'stop':threading.Event(),'queue':queue.Queue(maxsize=100)}
    streams[uid]=entry
    def emit(event):
        try:outbox.put(event,timeout=1)
        except queue.Full:entry['stop'].set()
    try:
        conn=LocalAppWebSocket(root,request)
        emit({'type':'app.ws.opened','id':uid,'ok':True})
        while not entry['stop'].is_set():
            for _ in range(12):
                try:msg=entry['queue'].get_nowait()
                except queue.Empty:break
                if msg.get('type')=='close':entry['stop'].set();break
                raw=base64.b64decode(msg.get('body_b64',''),validate=True)
                conn.send(1 if msg.get('mode')=='text' else 2,raw)
            if entry['stop'].is_set():break
            ready,_,_=select.select([conn.sock],[],[],0.3)
            if not ready:continue
            opcode,raw=conn.receive()
            if opcode==8:break
            if opcode==9:conn.send(10,raw);continue
            if opcode==10:continue
            if opcode not in (1,2):raise OSError('UNSUPPORTED_WS_FRAME')
            emit({'type':'app.ws.data','id':uid,'mode':'text' if opcode==1 else 'bytes',
                  'body_b64':base64.b64encode(raw).decode('ascii')})
    except (OSError,ValueError,socket.timeout) as ex:
        emit({'type':'app.ws.opened' if conn is None else 'app.ws.closed','id':uid,
              'ok':False,'error':type(ex).__name__})
    finally:
        streams.pop(uid,None)
        if conn:conn.close()
        emit({'type':'app.ws.closed','id':uid})


def local_http_stream(root,request,emit,cancel):
    """Relay SSE and static SPA assets in bounded WSS messages, not one big frame."""
    uid=request.get('id','')
    if not isinstance(uid,str) or len(uid)!=36:return
    apps=available_registry(root)
    app=request.get('app_id')
    try:
        if app not in ('snowluma','dsh-fish-soul') or app not in apps:
            raise ValueError('APP_NOT_REGISTERED')
        try:
            from .agent_v2 import safe_path_fragment
        except ImportError:
            from agent_v2 import safe_path_fragment
        path=safe_path_fragment(request.get('path','/'))
        method=str(request.get('method','GET')).upper()
        if method not in ('GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'):
            raise ValueError('INVALID_STREAM_METHOD')
        raw_body=base64.b64decode(request.get('body_b64',''),validate=True)
        if len(raw_body)>MAX_BODY:raise ValueError('BODY_TOO_LARGE')
        url=apps[app]['url'].rstrip('/')+path
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        browser_headers=request.get('headers') or {}
        if not isinstance(browser_headers,dict):raise ValueError('INVALID_HEADERS')
        stream_headers={k:v for k,v in browser_headers.items()
                        if k.lower() in HEADERS and isinstance(v,str) and len(v)<8192
                        and '\r' not in v and '\n' not in v}
        stream_headers['Accept-Encoding']='identity'
        stream_headers['Origin']=apps[app]['url'].rstrip('/')
        stream_headers['Referer']=apps[app]['url'].rstrip('/')+'/' 
        snowluma_token=browser_headers.get('x-snowluma-token','')
        if (isinstance(snowluma_token,str) and 0<len(snowluma_token)<8192 and
                '\r' not in snowluma_token and '\n' not in snowluma_token and
                app=='snowluma'):
            stream_headers['X-SnowLuma-Token']=snowluma_token
        req=urllib.request.Request(url,method=method,headers=stream_headers,
             data=raw_body if method not in ('GET','HEAD') else None)
        try:response=opener.open(req,timeout=55 if method in ('POST','PUT','PATCH','DELETE') else 30)
        except urllib.error.HTTPError as ex:response=ex
        with response as r:
            headers={k:r.headers.get(k,'')[:500] for k in
                     ('Content-Type','Location','Cache-Control','Content-Disposition') if r.headers.get(k)}
            emit({'type':'app.http.stream_start','id':uid,'status':int(r.status),
                  'content_type':r.headers.get('Content-Type','application/octet-stream')[:120],
                  'headers':headers,
                  'set_cookies':[x[:4000] for x in (r.headers.get_all('Set-Cookie') or [])[:12]]})
            total=0
            is_events='text/event-stream' in r.headers.get('Content-Type','').lower()
            while not cancel.is_set() and total<64*1024*1024:
                limit=min(24*1024,64*1024*1024-total)
                # SSE must forward one short line immediately; read(n) buffers
                # until n bytes and would silently break real-time events.
                chunk=r.readline(limit) if is_events else r.read(limit)
                if not chunk:break
                total+=len(chunk)
                emit({'type':'app.http.stream_data','id':uid,
                     'body_b64':base64.b64encode(chunk).decode('ascii')})
    except (OSError,ValueError,TimeoutError,urllib.error.URLError) as ex:
        # Stable error codes only; no local URL, cookie, password or token in logs.
        code=str(ex)[:50] if isinstance(ex,ValueError) else type(ex).__name__
        emit({'type':'app.http.stream_error','id':uid,'error':code})
    finally:
        emit({'type':'app.http.stream_end','id':uid})
