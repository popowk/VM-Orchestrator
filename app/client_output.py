"""Safe, bounded textual results for a customer's own authorized self-action.

Never send command source, Agent identity, working directory or full operation
JSON. Sensitive output lines are redacted rather than exposed to tenants.
"""
from __future__ import annotations

import re

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_SECRET_ASSIGNMENT = re.compile(
    r'(?i)(?:password|passwd|pwd|token|secret|api[_-]?key|authorization|cookie)'
    r'\s*[:=]\s*\S+')
_SECRET_FORMAT = re.compile(r'(?i)\b(?:bearer\s+\S+|(?:ghp_|github_pat_)[A-Za-z0-9_]{10,})')
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def safe_excerpt(value: object, max_chars: int = 800) -> str:
    if not isinstance(value, str) or not value:
        return ''
    text = _ANSI.sub('', value).replace('\r\n', '\n').replace('\r', '\n')
    text = _CONTROL.sub('', text)
    text = _SECRET_ASSIGNMENT.sub('[敏感信息已隐藏]', text)
    text = _SECRET_FORMAT.sub('[敏感信息已隐藏]', text)
    lines = [line.strip()[:240] for line in text.splitlines() if line.strip()]
    selected = '\n'.join(lines[:8]).strip()
    return selected[:max_chars] + ('…' if len(selected) > max_chars or len(lines) > 8 else '')


def customer_command_result(result: object, status: str) -> dict:
    if not isinstance(result, dict):
        result = {}
    if status == 'SUCCEEDED':
        if result.get('launched') is True:
            return {'outcome': 'START_ACCEPTED', 'output_text': '启动命令已被客户机接收；程序是否正常运行仍需检查。'}
        content = safe_excerpt(result.get('stdout')) or safe_excerpt(result.get('stderr'))
        return {'outcome': 'EXIT_CODE_ZERO',
                'output_text': content or '执行成功（程序没有输出内容）。'}
    if status not in ('FAILED', 'TIMED_OUT'):
        return {}
    if result.get('error') == 'TimeoutExpired' or status == 'TIMED_OUT':
        return {'output_text': '等待执行结果超时，不能确定命令是否完成，请联系管理员核对。'}
    # stderr is first priority. stdout is useful when a Windows program
    # incorrectly prints its failure only to stdout.
    content = safe_excerpt(result.get('stderr')) or safe_excerpt(result.get('stdout'))
    if not content:
        content = safe_excerpt(result.get('error'))
    if not content and isinstance(result.get('exit_code'), int):
        content = '执行失败（退出码 ' + str(result['exit_code']) + '），程序没有提供错误输出。'
    return {'output_text': content or '执行失败；客户机未返回具体错误信息。'}
