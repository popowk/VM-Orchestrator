# App Report v1.4.12 — any local Windows account can report

**No Administrator / UAC / SYSTEM permission is needed for the reporting application.**
The existing Guest Agent V2 **Windows service** remains SYSTEM and is the only
process allowed to write `state/app-registry.json`, `agent.json` and DPAPI secrets.

Flow: `SnowLuma / DSH plugin` -> local stdlib `Report-App-JSON.py` via stdin ->
`http://127.0.0.1:18767/v1/apps/report` (loopback ONLY) -> V2 service ->
protected registry -> existing authenticated WSS to host `8766` -> client portal.
The reporter must run **inside the same Windows VM** as the Guest Agent.
The browser/client portal must never send reports to this local receiver.

No new host-side software is needed for this change. Do not expose TCP 18767
on a physical, virtual, or public interface and do not create a firewall rule.

## Required JSON (submit real software-issued code at runtime)

SnowLuma:

```json
{"app_id":"snowluma","url":"http://127.0.0.1:5099/","version":"2.4.2","online":true,"auth":{"kind":"random_password","access_code":null}}
```

DSH:

```json
{"app_id":"dsh-fish-soul","url":"http://127.0.0.1:56781/","version":"0.15.5","online":true,"auth":{"kind":"token","access_code":null}}
```

`null` indicates that no new code is provided (does **not** make the program
passwordless). Previously stored credentials may be retained. For an accurate
new code, the application plugin must populate `access_code` from the
software's actual startup output. Never put it in the command line, URL,
normal file logs, or an on-disk plain JSON template.

The receiver limits unprivileged reports to only these loopback endpoints:
`SnowLuma:5099`, `DSH:56781/55701/3080`. This prevents clients from
exposing arbitrary local services through the customer relay. If the app
changes its port, update the allowlist *in the agent source* and reinstall.

## Node.js integration (non-admin, code passed through stdin)

```js
const { spawn } = require('node:child_process');
function reportApp(app) {
  const child = spawn('C:\\Program Files\\Python314\\python.exe',
    ['C:\\Program Files\\VM-Orchestrator\\GuestAgent\\Report-App-JSON.py'],
    {stdio:['pipe','pipe','pipe'], windowsHide:true});
  let error = '';
  child.stderr.on('data', c => {error += c.toString('utf8').slice(0,150)});
  child.on('close', code => console.log('VMO app report:', code === 0 ? 'accepted' : `exit-${code} ${error}`));
  child.stdin.end(JSON.stringify(app));
}
```

## PowerShell / manual test, no UAC required

```powershell
$py = 'C:\Program Files\Python314\python.exe'
$script = 'C:\Program Files\VM-Orchestrator\GuestAgent\Report-App-JSON.py'
@{app_id='dsh-fish-soul';url='http://127.0.0.1:56781/';version='0.15.5';online=$true;
  auth=@{kind='token';access_code=$null}} | ConvertTo-Json -Compress -Depth 5 | & $py $script
Write-Host "exit=$LASTEXITCODE"
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:18767/health
Get-Service VMO-GuestAgentV2
```

Successful report: `Application registration accepted by local Guest Agent;
WSS sync will follow.` `Exit code 0` means the Guest accepted it, **not**
that the host has already received it. Confirm the WSS status and `apps.report`
on the host customer/admin portal after ~20 seconds. The application should
retry if reporter not available (but not every few seconds forever).

**Security limitation:** Any process running under any local Windows account
on this VM can submit or overwrite the *two approved application reports*.
This is intentional for a single-customer VM, but should not be enabled on a
shared Windows machine with mutually untrusted users. The endpoint is not a
remote administration interface and does not grant users write access to
Agent credentials or protected state.
