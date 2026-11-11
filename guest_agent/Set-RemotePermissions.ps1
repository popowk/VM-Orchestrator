# V1.4.7 compatibility shim: Guest consent toggle no longer applies.
# Access is controlled by administrator authentication on Host 8765 and
# enrolled VM identity on Guest WSS 8766. This script cannot alter that.
param([ValidateSet('ReadOnly','SYSTEM')][string]$Mode='ReadOnly')
Write-Warning 'VM Orchestrator 1.4.7 no longer uses a Guest-side remote-command permission switch.'
Write-Host 'Authenticated Host administrators can run SYSTEM commands on enrolled Guests.'
Write-Host 'To prohibit remote commands, revoke the VM Agent identity or stop the Agent service.'
exit 0
