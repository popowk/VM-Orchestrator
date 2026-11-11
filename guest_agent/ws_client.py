"""RFC6455 WSS client using Python stdlib, with certificate validation.

One TLS connection initiated by the Windows Guest. No inbound guest listener or
third-party WebSocket module required. Frame parsing is timeout-safe; a partial
header/frame is retained intact until the full message is available.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

MAX_FRAME=1024*1024

class WebsocketError(OSError): pass


class Websocket:
    def __init__(self,url,headers,ca_bundle,timeout=10):
        p=urlsplit(url)
        if p.scheme!='wss' or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise WebsocketError('Only credential-free WSS URLs allowed')
        port=p.port or 443
        raw=socket.create_connection((p.hostname,port),timeout)
        context=ssl.create_default_context(cafile=ca_bundle)
        self.sock=context.wrap_socket(raw,server_hostname=p.hostname)
        self.sock.settimeout(timeout)
        self.pending=bytearray()
        key=base64.b64encode(os.urandom(16)).decode('ascii')
        requested=f'{p.path or "/"}'
        host=f'{p.hostname}:{port}'
        lines=[f'GET {requested} HTTP/1.1',f'Host: {host}','Upgrade: websocket',
            'Connection: Upgrade',f'Sec-WebSocket-Key: {key}','Sec-WebSocket-Version: 13']
        for k,v in headers.items():
            if k.lower() not in ('x-agent-id','x-agent-key') or any(x in str(v) for x in '\r\n'):
                raise WebsocketError('Unsafe WebSocket header')
            lines.append(f'{k}: {v}')
        self.sock.sendall(('\r\n'.join(lines)+'\r\n\r\n').encode('ascii'))
        buf=bytearray()
        while b'\r\n\r\n' not in buf:
            b=self.sock.recv(4096)
            if not b or len(buf)+len(b)>16384: raise WebsocketError('Invalid WSS handshake')
            buf.extend(b)
        head,body=bytes(buf).split(b'\r\n\r\n',1)
        rows=head.decode('latin1').split('\r\n')
        if not rows[0].startswith('HTTP/1.1 101 ') and not rows[0].startswith('HTTP/1.0 101 '):
            raise WebsocketError('Gateway rejected WSS handshake: HTTP ' + rows[0].split(' ')[1][:3])
        h={}
        for row in rows[1:]:
            if ':' in row:
                name,value=row.split(':',1);h[name.lower()]=value.strip()
        expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        if h.get('sec-websocket-accept')!=expected or h.get('upgrade','').lower()!='websocket':
            raise WebsocketError('WebSocket handshake verification failed')
        self.pending.extend(body)
        self.sock.settimeout(1)

    def recv_exact(self,size):
        while len(self.pending)<size:
            part=self.sock.recv(min(65536,size-len(self.pending)))
            if not part:raise WebsocketError('WebSocket closed')
            self.pending.extend(part)
        out=bytes(self.pending[:size]);del self.pending[:size]
        return out

    def frame(self,opcode,data=b''):
        if len(data)>MAX_FRAME:raise WebsocketError('Message too large')
        mask=os.urandom(4)
        size=len(data)
        header=bytes([0x80 | opcode])
        if size<126:header+=bytes([0x80|size])
        elif size<65536:header+=bytes([0x80|126])+size.to_bytes(2,'big')
        else:header+=bytes([0x80|127])+size.to_bytes(8,'big')
        masked=bytes(b^mask[i%4] for i,b in enumerate(data))
        self.sock.sendall(header+mask+masked)

    def send(self,event:dict):
        self.frame(1,json.dumps(event,ensure_ascii=False,separators=(',',':')).encode('utf-8'))

    def recv(self):
        """Read one complete frame; never consume a *partial* frame on timeout.

        Windows service sockets have a short receive timeout so we can send
        heartbeats and command results. TCP may split even a WebSocket's 2-byte
        extended-size header. Older versions consumed the header first; a timeout
        midway corrupted the frame boundary and often produced a fictitious
        `Server message exceeded size limit` error, dropping the whole WSS link.
        """
        try:
            self._buffer_at_least(2)
            first,second=self.pending[0],self.pending[1]
            opcode=first&0x0f
            if first&0x70:raise WebsocketError('Unexpected WebSocket RSV bits')
            if second&0x80:raise WebsocketError('Server MUST NOT mask frames')
            size=second&0x7f
            extra=2 if size==126 else 8 if size==127 else 0
            self._buffer_at_least(2+extra)
            if size==126:size=int.from_bytes(self.pending[2:4],'big')
            elif size==127:size=int.from_bytes(self.pending[2:10],'big')
            if size>MAX_FRAME:
                raise WebsocketError('Server frame exceeds configured limit ('+str(size)+' bytes)')
            if opcode in (8,9,10) and (size>125 or not first&0x80):
                raise WebsocketError('Invalid WebSocket control frame')
            if opcode not in (1,8,9,10) or not first&0x80:
                raise WebsocketError('Unsupported fragmented or binary frame')
            total=2+extra+size
            self._buffer_at_least(total)
            data=bytes(self.pending[2+extra:total])
            del self.pending[:total]   # Commit only after all bytes have arrived.
            if opcode==8:raise WebsocketError('Gateway closed connection')
            if opcode==9:self.frame(10,data);return None
            if opcode==10:return None
            event=json.loads(data.decode('utf-8'))
            return event if isinstance(event,dict) else None
        except socket.timeout:
            return None

    def _buffer_at_least(self,size):
        while len(self.pending)<size:
            part=self.sock.recv(min(65536,size-len(self.pending)))
            if not part:raise WebsocketError('WebSocket closed')
            self.pending.extend(part)

    def close(self):
        try:self.frame(8)
        except (OSError,ValueError):pass
        self.sock.close()
