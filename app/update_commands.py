"""Administrator plugin update command validation (not a general remote shell)."""
import re
from pathlib import PureWindowsPath

VERSION=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$')
# DSH plugin operations only: normal CLI, quoted absolute dsh.cmd, optional PowerShell call operator.
COMMAND=re.compile(
    r"^\s*(?:&\s*)?(?:dsh(?:\.cmd|\.exe)?|(?:'[^'\r\n]+\\dsh\.(?:cmd|exe)'|\"[^\"\r\n]+\\dsh\.(?:cmd|exe)\"))"
    r"\s+plugin\s+--profile\s+[A-Za-z0-9][A-Za-z0-9_-]{0,39}"
    r"\s+(?:update(?:\s+(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+)?|add\s+(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9._+-]{0,39})\s*$",re.I)

def validate_plugin_command(command,version):
    if not isinstance(version,str) or not VERSION.fullmatch(version):
        raise ValueError('Invalid target version')
    if not isinstance(command,str) or len(command)>1024 or re.search(r'[\r\n;|<>$`\x00]',command) or command.count('&')>1 or ('&' in command and not command.lstrip().startswith('& ')) or not COMMAND.fullmatch(command):
        raise ValueError('Only a single dsh plugin --profile ... update/add command is supported; no pipes, chained scripts or extra PowerShell operators')
    return command.strip()


def validate_install_dir(value):
    if not isinstance(value,str) or len(value)>230 or not value:
        raise ValueError('SnowLuma install directory required')
    p=PureWindowsPath(value)
    parts=[x.casefold() for x in p.parts]
    if p.drive.casefold()!='c:' or len(parts)<3 or parts[1]!='dsh' or any(x in ('.','..') for x in parts):
        raise ValueError('SnowLuma update directory must be a child of C:\\dsh\\')
    if not p.name.casefold().startswith('snowluma'):
        raise ValueError('The target folder must begin with SnowLuma')
    return str(p)
