# -*- coding: utf-8 -*-
"""Submit an application startup report from ANY local Windows account.

Pass JSON through stdin, never through command-line arguments, logs or a file.
The SYSTEM Guest Agent owns the protected state; this client never accesses it.
"""
from __future__ import annotations
import json
import sys
from local_app_report import accept, submit_report, validate

if __name__ == "__main__":
    try:
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            raise ValueError("Payload too large")
        payload = json.loads(raw)
        validate(payload)
        submit_report(payload)
        print("Application registration accepted by local Guest Agent; WSS sync will follow.")
    except Exception as exc:
        # Fixed error class plus limited well-defined connection-reason text,
        # without printing JSON, passwords, tokens, or private client data.
        msg = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Application report rejected: " + msg[:120], file=sys.stderr)
        sys.exit(1)
