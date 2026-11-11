"""Same-origin browser bridge to the already-authenticated Guest HTTPS Gateway.

No customer browser opens 192.168.*:8766 or trusts the private Agent CA. The
Host Agent uses its *existing* private CA to communicate with port 8766 over
HTTPS/WSS. The Guest still authorizes every relay by bound customer+VM cookie.
Never expose /agent/* here; only /apps/* is forwarded.
"""
from __future__ import annotations
import asyncio
import os
import re
import ssl
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import Response, HTMLResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

PASSTHROUGH_HEADERS={'accept','accept-language','content-type','authorization',
                     'cookie','range','x-requested-with','cache-control',
                     'x-snowluma-token'}
RETURN_HEADERS={'content-type','location','cache-control','x-accel-buffering',
                'referrer-policy','content-security-policy','x-content-type-options'}


def trusted_gateway():
    url=os.environ.get('VMO_GUEST_GATEWAY_URL','').rstrip('/')
    ca=os.environ.get('VMO_GUEST_CA_PEM','')
    # When the Host Console was launched before 8766, its environment values
    # can be empty forever. Discover the SAME pre-provisioned gateway from
    # restricted local data, not from a browser-supplied URL or query param.
    if not url and not ca:
        from .config import Settings
        directory=Settings.from_env().db_path.parent
        network=directory/'guest-network.json'
        tls=directory/'guest-gateway-tls'
        ip='192.168.2.99';port=8766
        if network.exists():
            try:
                config=json.loads(network.read_text(encoding='utf-8-sig'))
                ip=str(config['ListenIp']);port=int(config['Port'])
            except (OSError,ValueError,KeyError,TypeError):
                raise RuntimeError('Invalid guest network configuration') from None
        try:
            addr=ipaddress.ip_address(ip)
            if not isinstance(addr,ipaddress.IPv4Address) or not addr.is_private or addr.is_loopback or port!=8766:
                raise RuntimeError('Unsafe guest gateway address')
        except ValueError:
            raise RuntimeError('Invalid guest gateway IPv4') from None
        file=tls/'gateway-url.txt'
        if file.is_file() and (tls/'ca.pem').is_file():
            recorded=file.read_text(encoding='utf-8-sig').strip().rstrip('/')
            if recorded==f'https://{ip}:{port}':
                url=recorded;ca=str(tls/'ca.pem')
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or not parsed.port or parsed.path not in ('','/'):
        raise RuntimeError('8766 Gateway URL not configured')
    if not ca or not Path(ca).is_file():
        raise RuntimeError('8766 Gateway CA missing')
    return url,ssl.create_default_context(cafile=ca)


def gateway_configured():
    try:
        trusted_gateway()
        return True
    except (RuntimeError,OSError,ValueError,ssl.SSLError):
        return False


def forwarded_cookies(cookies: list[str], request:Request):
    # Gateway cookies already bind each application to a particular customer.
    # A private LAN HTTP 8765 URL cannot use Secure cookies; without this,
    # /apps/open redirects successfully but its session cookie is discarded.
    # Use HTTPS for production/Internet and iframe embedding across origins.
    host=request.url.hostname or ''
    try: private_ip=ipaddress.ip_address(host).is_private
    except ValueError:private_ip=False
    private_http=request.url.scheme=='http' and (private_ip or host=='localhost')
    if not private_http:return cookies
    return [re.sub(r';\s*SameSite=None\b','; SameSite=Lax',
                   re.sub(r';\s*Secure\b','',cookie,flags=re.I),flags=re.I)
            for cookie in cookies]


def error_response():
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <title>应用连接失败</title><body style="font:15px/1.8 sans-serif;padding:32px;color:#24324d">
    <h3>暂时无法连接 SnowLuma / DSH 应用网关</h3>
    <p>宿主机 8765 未能安全连接 8766。请管理员检查 8766 网关、CA 证书及 Guest WSS 在线状态。</p>
    <p>当前应用窗口不会自动刷新，修复后点击上方「重新连接」。</p></body></html>''',status_code=502)


def upstream_http_client(ctx):
    return httpx.AsyncClient(verify=ctx,trust_env=False,
          timeout=httpx.Timeout(connect=8,read=55,write=60,pool=8),follow_redirects=False)


# These are the only localhost application endpoints accepted by the Guest
# registry. Never rewrite arbitrary localhost ports into a customer's relay.
APP_LOCAL_PORTS={'snowluma': (5099,3080), 'dsh-fish-soul': (56781,55701)}


def rewrite_scoped_asset(blob:bytes,prefix:str,app_id:str,content_type:str='',path:str='')->bytes:
    """Bounded app-specific URL rewrites for SPA bundles and stylesheets.

    URL rewriting happens only for a tenant-bound /apps/<id>/<app>/ route,
    never on the administrative root or another tenant's resources.  It is
    a compatibility adapter; properly subpath-aware applications are better.
    """
    if (app_id not in APP_LOCAL_PORTS or
            not re.fullmatch(r'/apps/[A-Za-z0-9_-]+/(?:snowluma|dsh-fish-soul)/',prefix) or
            not prefix.endswith('/'+app_id+'/')):
        return blob
    ct=content_type.split(';',1)[0].lower().strip()
    suffix=path.split('?',1)[0].lower()
    javascript=('javascript' in ct or ct in ('application/ecmascript','text/ecmascript') or
                suffix.endswith(('.js','.mjs')))
    stylesheet=('text/css' in ct or suffix.endswith('.css'))
    html_page='text/html' in ct
    # Standalone DSH's successful unlock may return an absolute loopback URL
    # inside JSON rather than an HTTP Location header. Scope only bridge
    # responses, not arbitrary application API JSON (tokens stay untouched).
    bridge_json=(app_id=='dsh-fish-soul' and 'json' in ct and
                 (suffix.startswith('__bridge/') or suffix.startswith('api/auth')))
    if not (javascript or stylesheet or html_page or bridge_json):return blob
    base=prefix.encode('ascii')
    if bridge_json:
        # DSH can reply with {redirect:"/"} rather than a 302. A redirect
        # to the Host Console root breaks tenant routing. Do not modify
        # unrelated fields (tokens, messages, application data).
        try:
            doc=json.loads(blob)
            nav_keys={'redirect','redirectUrl','next','nextUrl','url','href','location','path','target'}
            def adapt(node,depth=0):
                if depth>8:return node
                if isinstance(node,list):return [adapt(x,depth+1) for x in node]
                if not isinstance(node,dict):return node
                out={}
                for key,val in node.items():
                    if isinstance(val,str) and key in nav_keys:
                        if val=='/':val=prefix
                        else:
                            parsed=urlsplit(val)
                            if (parsed.hostname in ('127.0.0.1','localhost') and
                                 parsed.port in (*APP_LOCAL_PORTS[app_id],8765)):
                                val=prefix+parsed.path.lstrip('/')+('?' + parsed.query if parsed.query else '')
                    elif isinstance(val,(dict,list)):val=adapt(val,depth+1)
                    out[key]=val
                return out
            blob=json.dumps(adapt(doc),ensure_ascii=False,separators=(',',':')).encode('utf-8')
        except (ValueError,TypeError,UnicodeError):pass
    # Some standalone DSH builds compute an admin-origin absolute link from
    # their current proxy origin. In a DSH app page, never send that local
    # Web Console URL back to a customer browser. Only rewrite this literal
    # for DSH HTML/JS (not arbitrary JSON payloads or external links).
    if app_id=='dsh-fish-soul' and (javascript or html_page):
        for origin in (b'http://127.0.0.1:8765/',b'http://localhost:8765/'):
            blob=blob.replace(origin,base)
    # Hardcoded loopback addresses are otherwise navigated by the CLIENT's
    # browser, not the customer's VM. Scope only the app's known ports.
    for port in APP_LOCAL_PORTS[app_id]:
        for hostname in (b'127.0.0.1',b'localhost'):
            for scheme in (b'http://',b'https://',b'ws://',b'wss://'):
                target=scheme+hostname+b':'+str(port).encode('ascii')
                blob=blob.replace(target+b'/',base)
                # Only standalone literal endpoints, not hostname prefixes.
                blob=re.sub(re.escape(target)+rb'(?=["\'`\s<>])',base.rstrip(b'/'),blob)
    if javascript:
        if app_id=='dsh-fish-soul':
            # The successful bridge unlock may navigate to the SPA root.
            # Rewriting only literal navigation calls keeps arithmetic and API
            # payloads unchanged. The root recovery script handles dynamic forms.
            blob=re.sub(rb'(\b(?:location\.(?:assign|replace)|window\.open)\(\s*["\'])/(?:["\'])',
                        lambda m:m.group(1)+base,blob)
            blob=re.sub(rb'(\b(?:window\.)?location\.href\s*=\s*["\'])/(?:["\'])',
                        lambda m:m.group(1)+base,blob)
        # Root-absolute string literals generated by Vite / standalone bridge.
        # Do not rewrite arbitrary JS slash operators or third-party origins.
        known=(b'__bridge/',b'api/',b'assets/',b'logo.png',b'favicon.png',
               b'apple-touch-icon.png',b'socket.io/',b'events',b'login',b'dashboard')
        roots=b'|'.join(re.escape(item) for item in known)
        blob=re.sub(rb'(["\'`])/(?!/)(?=(?:'+roots+rb'))',
                    lambda m:m.group(1)+base,blob)
    if stylesheet:
        # CSS url(/logo.png) does not use the HTML shim. Limit to url() only.
        blob=re.sub(rb'url\(\s*(["\']?)/(?!/)',
                    lambda m:b'url('+m.group(1)+base,blob,flags=re.I)
    return blob


def rewrite_dsh_bridge_script(blob:bytes,prefix:str)->bytes:
    """Backward-compatible public adapter for earlier regression tests."""
    return rewrite_scoped_asset(blob,prefix,'dsh-fish-soul','application/javascript','script.js')


def install_same_origin_app_proxy(app:FastAPI):
    @app.api_route('/apps/open',methods=['POST'],include_in_schema=False)
    @app.api_route('/apps/{rest:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'],include_in_schema=False)
    async def app_http(request:Request,rest:str=''):
        # All HTTP auth, app-specific URI rewriting and relay tokens are
        # enforced by the 8766 gateway, not duplicated in this thin bridge.
        try:base,ctx=trusted_gateway()
        except (OSError,ValueError,RuntimeError):return error_response()
        path=request.url.path
        url=base+path+('?' + request.url.query if request.url.query else '')
        headers={k:v for k,v in request.headers.items() if k.lower() in PASSTHROUGH_HEADERS}
        try:
            body=await request.body()
            client=upstream_http_client(ctx)
            req=client.build_request(request.method,url,headers=headers,content=body)
            upstream=await client.send(req,stream=True)
        except (httpx.HTTPError,OSError):
            if 'client' in locals():await client.aclose()
            return error_response()
        outbound={k:v for k,v in upstream.headers.items() if k.lower() in RETURN_HEADERS}
        cookies=forwarded_cookies(upstream.headers.get_list('set-cookie'),request)
        match_scope=re.fullmatch(r'/apps/([A-Za-z0-9_-]+)/(snowluma|dsh-fish-soul)/(.*)',path)
        # When an app returns the public site's root as an absolute Location,
        # it meant its own root, not the VMO admin console. Scope the redirect
        # strictly to the current relay; do not introduce a root catch-all.
        if match_scope and 'location' in outbound:
            loc=urlsplit(outbound['location'])
            public_host=(os.environ.get('VMO_PUBLIC_CONSOLE_ORIGIN','').strip() or 'https://host.tmd.pet')
            site=urlsplit(public_host)
            if (loc.scheme in ('http','https') and loc.hostname==site.hostname and
                  loc.path in ('','/') and not loc.username and not loc.password):
                prefix=f'/apps/{match_scope.group(1)}/{match_scope.group(2)}/'
                outbound['location']=prefix+('?' + loc.query if loc.query else '')+('#'+loc.fragment if loc.fragment else '')

        async def close_response():
            await upstream.aclose();await client.aclose()
        # SSE must stream through both servers without waiting for its close.
        if upstream.headers.get('content-type','').lower().startswith('text/event-stream'):
            reply=StreamingResponse(upstream.aiter_raw(),status_code=upstream.status_code,
                     headers=outbound,background=BackgroundTask(close_response))
        else:
            try:payload=await upstream.aread()
            except (httpx.HTTPError,OSError):
                await close_response();return error_response()
            # Rewrite known static JS/CSS localhost and root-absolute URLs for
            # both apps; only after the 8766 per-VM session was authorized.
            match=re.fullmatch(r'/apps/([A-Za-z0-9_-]+)/(snowluma|dsh-fish-soul)/(.*)',path)
            if match and upstream.status_code==200 and len(payload)<=12*1024*1024:
                # fish-soul Bridge v6.9.24 supplies its own basepath-aware
                # __bridge/* JS and relative HTML resources. Do NOT rewrite
                # its static JS literals again: the old regex transforms
                # /api/plugins/fish-soul/ inside the native adapter itself.
                native_bridge_asset = (match.group(2)=='dsh-fish-soul' and
                    match.group(3).startswith('__bridge/') and
                    match.group(3).endswith(('.js','.mjs')))
                native_snow_bundle = (match.group(2)=='snowluma' and
                    match.group(3).startswith('assets/') and
                    match.group(3).endswith(('.js','.mjs')) and
                    (b'__vmoSnowUrl' in payload or b'window.__VMO_APP_BASE_PATH__' in payload
                     or b'router-_TYMdJui.vmo.js' in payload))
                if not native_bridge_asset and not native_snow_bundle:
                    payload=rewrite_scoped_asset(payload,
                        f'/apps/{match.group(1)}/{match.group(2)}/',
                        match.group(2),upstream.headers.get('content-type',''),match.group(3))
            reply=Response(content=payload,status_code=upstream.status_code,headers=outbound)
            await close_response()
        # Must use separate header entries; combining Set-Cookie breaks sessions.
        for cookie in cookies:reply.headers.append('Set-Cookie',cookie)
        return reply

    @app.websocket('/apps/{rest:path}')
    async def app_ws(ws:WebSocket,rest:str):
        try:base,ctx=trusted_gateway()
        except (RuntimeError,OSError,ValueError):
            await ws.close(code=1013);return
        origin=base
        url=base.replace('https://','wss://',1)+ws.url.path
        if ws.url.query:url+='?'+ws.url.query
        cookies=ws.headers.get('cookie','')
        # CSRF/origin is verified by the upstream gateway against its own
        # selected Host. Do not forward untrusted browser Origin verbatim.
        try:
            async with connect(url,ssl=ctx,origin=origin,
                    additional_headers={'Cookie':cookies} if cookies else {},
                    max_size=1024*1024,ping_interval=20,open_timeout=12,proxy=None) as upstream:
                await ws.accept()
                async def browser_to_guest():
                    while True:
                        message=await ws.receive()
                        if message.get('type')=='websocket.disconnect':break
                        data=message.get('bytes')
                        if data is not None:await upstream.send(data)
                        elif message.get('text') is not None:await upstream.send(message['text'])
                        else:break
                async def guest_to_browser():
                    async for message in upstream:
                        if isinstance(message,bytes):await ws.send_bytes(message)
                        else:await ws.send_text(message)
                tasks=[asyncio.create_task(browser_to_guest()),asyncio.create_task(guest_to_browser())]
                done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in pending:task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                for task in done:
                    if not task.cancelled():task.exception()  # consume safe disconnect
        except (OSError,RuntimeError,ConnectionClosed,WebSocketDisconnect,asyncio.TimeoutError):
            pass
        finally:
            try:await ws.close()
            except (OSError,RuntimeError):pass
