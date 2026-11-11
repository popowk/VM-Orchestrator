"""Read-only HTTPS gateway test; no token/key values leave the guest."""
from pathlib import Path
import json
import ssl
import sys
import urllib.request
import urllib.error

state=Path(__file__).resolve().parent/'state'
config=state/'agent.json'
try:
    cfg=json.loads(config.read_text(encoding='utf-8-sig'))
    ctx=ssl.create_default_context(cafile=cfg.get('ca_bundle'))
    url=cfg['server_url'].rstrip('/')+'/agent/health'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
       urllib.request.HTTPSHandler(context=ctx))
    with opener.open(url, timeout=8) as response:
        ok=response.status==200
        print('Gateway TLS and IP SAN verified:',ok)
        print('HTTP status:',response.status)
        print('VM ID configured:',bool(cfg.get('vm_id')))
        print('Registered identity present:',bool(cfg.get('agent_id') and cfg.get('agent_key')))
        sys.exit(0 if ok else 1)
except Exception as exc:
    if isinstance(exc,urllib.error.HTTPError): print('HTTP ERROR:',exc.code)
    elif isinstance(exc,ssl.SSLCertVerificationError): print('TLS certificate trust error')
    elif isinstance(exc,urllib.error.URLError): print('Gateway connection error:',type(exc.reason).__name__)
    else: print('Gateway test failed:',type(exc).__name__)
    sys.exit(1)
