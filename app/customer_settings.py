"""Customer announcement delivery and per-service monthly operation quotas.

Counters use Asia/Shanghai calendar months; dates are stored as UTC epoch seconds.
"""
from __future__ import annotations
import json, sqlite3, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT=timezone(timedelta(hours=8))
DEFAULTS={'snapshot_limit':3,'monthly_restore_limit':3,'monthly_reinstall_limit':1}

class CustomerSettings:
    def __init__(self,path:Path):
        self.path=str(path);self.lock=threading.RLock()
        with self.db() as d:
            d.executescript('''
            CREATE TABLE IF NOT EXISTS customer_settings_global (id INTEGER PRIMARY KEY CHECK(id=1), snapshot_limit INTEGER NOT NULL DEFAULT 3, monthly_restore_limit INTEGER NOT NULL DEFAULT 3, monthly_reinstall_limit INTEGER NOT NULL DEFAULT 1);
            INSERT OR IGNORE INTO customer_settings_global(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS customer_settings_service (service_ref TEXT PRIMARY KEY,snapshot_limit INTEGER,monthly_restore_limit INTEGER,monthly_reinstall_limit INTEGER);
            CREATE TABLE IF NOT EXISTS customer_monthly_usage (service_ref TEXT NOT NULL, month TEXT NOT NULL, action TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(service_ref,month,action));
            CREATE TABLE IF NOT EXISTS customer_announcements (id TEXT PRIMARY KEY,body TEXT NOT NULL,repeat_hours INTEGER NOT NULL,enabled INTEGER NOT NULL,created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS customer_announcement_seen (service_ref TEXT NOT NULL,announcement_id TEXT NOT NULL,shown_at INTEGER NOT NULL,PRIMARY KEY(service_ref,announcement_id));
            ''')
    @contextmanager
    def db(self):
        d=sqlite3.connect(self.path,timeout=20);d.row_factory=sqlite3.Row
        try:
            d.execute('PRAGMA busy_timeout=20000')
            with d: yield d
        finally:d.close()
    def defaults(self):
        with self.db() as d:return dict(d.execute('SELECT snapshot_limit,monthly_restore_limit,monthly_reinstall_limit FROM customer_settings_global WHERE id=1').fetchone())
    def limits(self,service_ref):
        values=self.defaults()
        with self.db() as d:r=d.execute('SELECT * FROM customer_settings_service WHERE service_ref=?',(service_ref,)).fetchone()
        if r:
            for key in DEFAULTS:
                if r[key] is not None:values[key]=r[key]
        return values
    def set_limits(self,values,service_ref=None):
        for key,val in values.items():
            if key not in DEFAULTS or not isinstance(val,int) or isinstance(val,bool) or not 0<=val<=100:
                raise ValueError('Limits must be integers from 0 to 100')
        if set(values)!=set(DEFAULTS):raise ValueError('All three limit fields are required')
        with self.lock,self.db() as d:
            if service_ref:
                d.execute('INSERT INTO customer_settings_service VALUES(?,?,?,?) ON CONFLICT(service_ref) DO UPDATE SET snapshot_limit=excluded.snapshot_limit,monthly_restore_limit=excluded.monthly_restore_limit,monthly_reinstall_limit=excluded.monthly_reinstall_limit',(service_ref,*(values[k] for k in DEFAULTS)))
            else:
                d.execute('UPDATE customer_settings_global SET snapshot_limit=?,monthly_restore_limit=?,monthly_reinstall_limit=? WHERE id=1',tuple(values[k] for k in DEFAULTS))
        return self.limits(service_ref) if service_ref else self.defaults()
    def month(self):return datetime.now(BJT).strftime('%Y-%m')
    def usage(self,service_ref):
        m=self.month()
        with self.db() as d:rows=d.execute('SELECT action,used FROM customer_monthly_usage WHERE service_ref=? AND month=?',(service_ref,m)).fetchall()
        return {'month':m,'restore_used':next((r['used'] for r in rows if r['action']=='snapshot_restore'),0),'reinstall_used':next((r['used'] for r in rows if r['action']=='reinstall'),0)}
    def reserve(self,service_ref,action):
        mapping={'snapshot_restore':'monthly_restore_limit','reinstall':'monthly_reinstall_limit'}
        if action not in mapping:return
        m=self.month();limit=self.limits(service_ref)[mapping[action]]
        with self.lock,self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            current=d.execute('SELECT used FROM customer_monthly_usage WHERE service_ref=? AND month=? AND action=?',(service_ref,m,action)).fetchone()
            used=current['used'] if current else 0
            if used>=limit:raise ValueError('本月操作次数已用完，请联系管理员')
            d.execute('INSERT INTO customer_monthly_usage VALUES(?,?,?,1) ON CONFLICT(service_ref,month,action) DO UPDATE SET used=used+1',(service_ref,m,action))
        return m
    def release(self,service_ref,action,month):
        if month is None:return
        with self.lock,self.db() as d:d.execute('UPDATE customer_monthly_usage SET used=MAX(0,used-1) WHERE service_ref=? AND month=? AND action=?',(service_ref,month,action))
    def active_announcement(self):
        with self.db() as d:r=d.execute('SELECT * FROM customer_announcements WHERE enabled=1 ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        return dict(r) if r else None
    def publish(self,body,repeat_hours,enabled):
        if not isinstance(body,str) or len(body.strip())>4000 or (enabled and not body.strip()):raise ValueError('公告正文必填，最多4000字符')
        if not isinstance(repeat_hours,int) or isinstance(repeat_hours,bool) or not 1<=repeat_hours<=720:raise ValueError('公告间隔为1至720小时')
        with self.lock,self.db() as d:
            d.execute('UPDATE customer_announcements SET enabled=0')
            if enabled:
                id_=str(uuid.uuid4());d.execute('INSERT INTO customer_announcements VALUES(?,?,?,?,?)',(id_,body.strip(),repeat_hours,1,int(time.time())))
            else:id_=None
        return {'id':id_,'enabled':bool(enabled),'repeat_hours':repeat_hours,'body':body.strip() if enabled else ''}
    def pending(self,service_ref):
        a=self.active_announcement()
        if not a:return None
        with self.db() as d:r=d.execute('SELECT shown_at FROM customer_announcement_seen WHERE service_ref=? AND announcement_id=?',(service_ref,a['id'])).fetchone()
        if r and time.time()-r['shown_at']<a['repeat_hours']*3600:return None
        return {'id':a['id'],'body':a['body'],'repeat_hours':a['repeat_hours'],'published_at':a['created_at']}
    def acknowledge(self,service_ref,id_):
        a=self.active_announcement()
        if not a or a['id']!=id_:raise ValueError('公告已经更新或撤销')
        with self.lock,self.db() as d:d.execute('INSERT INTO customer_announcement_seen VALUES(?,?,?) ON CONFLICT(service_ref,announcement_id) DO UPDATE SET shown_at=excluded.shown_at',(service_ref,id_,int(time.time())))
        return {'acknowledged':True}
