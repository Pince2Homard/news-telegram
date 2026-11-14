"""Inspection ACL en lecture seule ; aucun credential n'est lu ici."""
import json
import os
from pathlib import Path
import subprocess

SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
  Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
  $paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
  $rows = @()
  foreach ($path in $paths) {
    $item = Get-Item -LiteralPath $path -Force
    $acl = Get-Acl -LiteralPath $path
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($acl.Sddl)
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
      @{sid=$_.IdentityReference.Value; allow=($_.AccessControlType -eq 'Allow');
        rights=[int64]$_.FileSystemRights; inherit_only=(($_.PropagationFlags -band 2) -ne 0)}
    })
    $rows += @{path=$item.FullName; owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;
      null_dacl=($null -eq $raw.DiscretionaryAcl); reparse=(($item.Attributes -band 1024) -ne 0); rules=$rules}
  }
  @{user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; rows=$rows} | ConvertTo-Json -Depth 8 -Compress
} catch { exit 2 }
'''


def inspect(paths):
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', SCRIPT],
        input=json.dumps([str(p) for p in paths]), text=True, capture_output=True,
        timeout=20, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError('ACL unavailable')
    return json.loads(result.stdout)


def validate(snapshot, paths):
    user = snapshot['user']
    if not isinstance(user, str) or not user.startswith('S-1-5-21-'):
        raise ValueError('user SID missing')
    trusted = {user, 'S-1-5-18', 'S-1-5-32-544'}  # compte, SYSTEM, Administrateurs
    rows = snapshot['rows']
    if len(rows) != len(paths):
        raise ValueError('incomplete ACL chain')
    # Parents : empecher remplacement, suppression enfant, changement DACL/owner.
    parent_mutation = 0x10000 | 0x40 | 0x40000 | 0x80000 | 0x100 | 0x10
    for index, (row, path) in enumerate(zip(rows, paths)):
        if (os.path.normcase(row['path']) != os.path.normcase(str(path)) or row['owner'] not in trusted
                or row['null_dacl'] is not False or row['reparse'] is not False):
            raise ValueError('unsafe owner, DACL or link')
        for rule in row['rules']:
            if type(rule['rights']) is not int or type(rule['allow']) is not bool or type(rule['inherit_only']) is not bool:
                raise ValueError('invalid ACE')
            if rule['inherit_only'] or not rule['allow'] or rule['sid'] in trusted:
                continue
            # Fichier : aucun acces tiers, y compris une ACE heritee.
            if index == 0 or rule['rights'] & (parent_mutation | 0xF0000000):
                raise ValueError('untrusted access')


def verify(path):
    path = Path(path).absolute()
    paths = [path, *path.parents]
    validate(inspect(paths), paths)
