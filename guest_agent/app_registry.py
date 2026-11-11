"""Protected local app registry. All local accounts report via SYSTEM loopback listener.
The agent.json identity and DPAPI-encrypted secrets remain SYSTEM/Admin-only.
"""
from __future__ import annotations
import argparse
import base64
import ctypes
from ctypes import wintypes
import getpass
import json
import os
import socket
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

APPS={'snowluma','dsh-fish-soul'}


def good_url(url):
    try:
        p=urlparse(url)
        return p.scheme=='http' and p.hostname in ('127.0.0.1','localhost') and p.port and not p.username and not p.password \
            and not p.fragment and p.path in ('','/') and not p.query
    except ValueError:return False


def _crypt(payload:bytes, decrypt=False):
    if os.name!='nt':raise RuntimeError('Application secrets require Windows DPAPI')
    # Machine-scope DPAPI; access to the encrypted file is separately restricted
    # by ProgramData/VMO ACL (SYSTEM + Administrators only).
    class Blob(ctypes.Structure):
        _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_byte))]
    src=ctypes.create_string_buffer(payload)
    srcblob=Blob(len(payload),ctypes.cast(src,ctypes.POINTER(ctypes.c_byte)))
    out=Blob()
    flags=0x01 # CRYPTPROTECT_UI_FORBIDDEN
    fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not decrypt:flags|=0x04 # CRYPTPROTECT_LOCAL_MACHINE
    ok=fn(ctypes.byref(srcblob),None,None,None,None,flags,ctypes.byref(out)) if decrypt \
        else fn(ctypes.byref(srcblob),None,None,None,None,flags,ctypes.byref(out))
    if not ok:raise ctypes.WinError()
    try:return ctypes.string_at(out.pbData,out.cbData)
    finally:
        free=ctypes.windll.kernel32.LocalFree
        free.argtypes=[wintypes.HLOCAL];free.restype=wintypes.HLOCAL
        free(out.pbData)


def registry_path(root):return Path(root)/'app-registry.json'


def read_registry(root):
    p=registry_path(root)
    if not p.is_file():return {}
    try: data=json.loads(p.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    return {k:v for k,v in data.items() if k in APPS and isinstance(v,dict) and good_url(v.get('url',''))}


def register(root,app,url,code='',version='',auth_kind='none'):
    if app not in APPS or not good_url(url):raise ValueError('Only approved loopback application endpoints allowed')
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    data=read_registry(root)
    stamp=datetime.now(timezone.utc).isoformat()
    entry={'url':url,'version':version[:40], 'reported_at':stamp,
           'auth_state':'configured' if code else 'unconfigured', 'auth_kind':auth_kind}
    if code:
        # Every explicit credential report replaces the previous encrypted value.
        # A metadata-only heartbeat must NOT reset its report timestamp.
        entry['secret_dpapi']=base64.b64encode(_crypt(code.encode('utf-8'))).decode('ascii')
        entry['secret_reported_at']=stamp
    elif app in data and data[app].get('secret_dpapi'):
        entry['secret_dpapi']=data[app]['secret_dpapi'];entry['auth_state']='configured'
        entry['secret_reported_at']=data[app].get('secret_reported_at','')
        if auth_kind == 'none':entry['auth_kind']=data[app].get('auth_kind','none')
    data[app]=entry
    try: from .agent import atomic_json
    except ImportError: from agent import atomic_json
    atomic_json(registry_path(root),data)


def load_app_secret(root,app):
    data=read_registry(root).get(app,{})
    try:
        enc=data.get('secret_dpapi')
        return _crypt(base64.b64decode(enc),decrypt=True).decode('utf-8') if enc else None
    except (ValueError,OSError):return None


# Only expected loopback ports AND recognizable application signatures may be
# auto-registered. An arbitrary service on localhost must never become exposed
# to a customer via the 8766 relay. Do not infer or reveal login passwords.
_AUTO_ENDPOINTS={
    'snowluma': (('http://127.0.0.1:5099/',), ('snowluma','snow luma')),
    'dsh-fish-soul': (('http://127.0.0.1:56781/','http://127.0.0.1:55701/','http://127.0.0.1:3080/'),
                      ('dsh web authentication','deepseek harness','dsh web')),
}
_probe_cache={}

def _probe_entry(app):
    if app not in _AUTO_ENDPOINTS:return None
    now=time.monotonic()
    cached=_probe_cache.get(app)
    if cached and now-cached[0]<18:return cached[1]
    endpoints,signatures=_AUTO_ENDPOINTS[app]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result=None
    for endpoint in endpoints:
        try:
            req=urllib.request.Request(endpoint,headers={'Accept':'text/html',
                                        'Accept-Encoding':'identity'})
            try: reply=opener.open(req,timeout=0.8)
            except urllib.error.HTTPError as error:reply=error
            with reply as resp:sample=resp.read(20000).decode('utf-8','replace').lower()
            if any(marker in sample for marker in signatures):
                result={'url':endpoint,'version':'','auth_state':'not_reported'}
                break
        except (OSError,ValueError,TimeoutError):pass
    _probe_cache[app]=(now,result)
    return result

def _registered_online(url):
    """An explicit ADMIN-registered loopback port is authoritative; test its TCP listener."""
    try:
        p=urlparse(url)
        with socket.create_connection(('127.0.0.1',p.port),timeout=0.6):return True
    except (OSError,ValueError,TypeError):return False

def available_registry(root):
    """Locally registered entries plus conservatively verified running apps."""
    result=read_registry(root)
    for app in _AUTO_ENDPOINTS:
        if app not in result:
            detection=_probe_entry(app)
            if detection:result[app]=detection
    return result

def reported_apps(root):
    registered=read_registry(root)
    entries=available_registry(root)
    result=[]
    for app,entry in entries.items():
        online=_registered_online(entry['url']) if app in registered else bool(_probe_entry(app))
        result.append({'app_id':app,'url':entry['url'],'version':entry.get('version',''),
            'auth_state':entry.get('auth_state','not_reported'),
            'auth_kind':entry.get('auth_kind','none'),
            'reported_at':entry.get('reported_at'),
            'secret_reported_at':entry.get('secret_reported_at'),
            'online':online})
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['register','list'])
    parser.add_argument('--app',choices=sorted(APPS))
    parser.add_argument('--url')
    parser.add_argument('--version',default='')
    parser.add_argument('--code-stdin',action='store_true')
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent/'state'))
    a=parser.parse_args()
    if a.action=='list':
        print(json.dumps(reported_apps(Path(a.root)),ensure_ascii=False))
    else:
        if not a.app or not a.url:parser.error('register requires --app and --url')
        code=input() if a.code_stdin else getpass.getpass('Application access code (hidden, press Enter to skip): ')
        try:
            from .local_app_report import submit_report
        except ImportError:
            from local_app_report import submit_report
        submit_report({'app_id':a.app,'url':a.url,'version':a.version,'online':True,
                       'auth':{'kind':('random_password' if a.app=='snowluma' else 'token'),
                               'access_code':code or None}})
        print('Application report accepted by local Guest Agent; access code not printed')
