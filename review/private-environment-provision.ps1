# REVIEW ONLY. No execution is authorized by issue comment 6898051158.
# Windows PowerShell 5.1 x64. Run only in a dedicated console OUTSIDE Codex,
# after approval of this exact source/hash and its explicitly listed effects.
[CmdletBinding()]
param(
    [switch]$AfterCodeReviewApproval,
    [long]$ApprovalCommentId = 0,
    [string]$ApprovedScriptSha256 = ''
)
if (-not $AfterCodeReviewApproval -or $ApprovalCommentId -le 6898051158) {
    Write-Output '{"code":"REVIEW_ONLY","mutated":false}'
    exit 2
}
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$Name = 'NewsTelegramPrivate'
$Profile = 'C:\Users\NewsTelegramPrivate'
$Root = $Profile + '\PrivateState'
$Roles = @('store','fence','witness','trial','control')
$Marker = $Root + '\provisioning.json'
$Revision = 'issue43-private-provision-v2'
$SystemSid = 'S-1-5-18'
$AdminsSid = 'S-1-5-32-544'
$UsersSid = 'S-1-5-32-545'
$TiSid = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
$Pins = [Collections.Generic.List[object]]::new()
$Token = [IntPtr]::Zero
$Luid = [IntPtr]::Zero
$OldPrivilege = $null
$CleanupFailures = [Collections.Generic.List[string]]::new()
$AccountDisabledVerified = $null
$Password = $null
$Sid = ''
$Changed = $false
$Code = 'PREFLIGHT_REFUSED'
$ExitCode = 1
$Native = $null

function Require([bool]$Condition) {
    if (-not $Condition) { throw [InvalidOperationException]::new('REFUSED') }
}
function SamePath([string]$A,[string]$B) {
    return [string]::Equals(
        [IO.Path]::GetFullPath($A).TrimEnd('\'),
        [IO.Path]::GetFullPath($B).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase)
}
function Missing([string]$Path) {
    try { $null = Get-Item -LiteralPath $Path -Force; return $false }
    catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { return $true }
        throw
    }
}
function LocalAccount {
    try { return Get-LocalUser -Name $Name }
    catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { return $null }
        throw
    }
}
function AssertOutsideCodex {
    Require (-not [string]::IsNullOrEmpty($PSCommandPath))
    Require ($PSCommandPath -notmatch '(?i)[\\/](\.codex|\.git|worktrees)[\\/]')
    Require (-not (SamePath ([IO.Path]::GetDirectoryName($PSCommandPath)) $Root))
    Require (-not $PSCommandPath.StartsWith($Profile+'\',[StringComparison]::OrdinalIgnoreCase))
    Require (@(Get-ChildItem Env: | Where-Object Name -Match '^CODEX').Count -eq 0)
    $ancestor = [uint32]$PID
    for ($i=0; $i -lt 64; $i++) {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId="+$ancestor)
        Require ($null -ne $p)
        Require ($p.Name -notmatch '(?i)codex')
        $ancestor = [uint32]$p.ParentProcessId
        if ($ancestor -eq 0) { return }
    }
    throw 'REFUSED'
}
function ImportNative {
    # Reflection.Emit only: no Add-Type compiler, DLL file, temp file or download.
    $a = [Reflection.AssemblyName]::new('PrivateProvision_'+[guid]::NewGuid().ToString('N'))
    $ab = [AppDomain]::CurrentDomain.DefineDynamicAssembly($a,[Reflection.Emit.AssemblyBuilderAccess]::Run)
    $tb = $ab.DefineDynamicModule('memory').DefineType('Native',
        [Reflection.TypeAttributes]'Public,Abstract,Sealed')
    $definitions = @(
        @('CreateFileW','kernel32.dll',[IntPtr],[Type[]]@([string],[uint32],[uint32],[IntPtr],[uint32],[uint32],[IntPtr])),
        @('CloseHandle','kernel32.dll',[bool],[Type[]]@([IntPtr])),
        @('GetFileInformationByHandleEx','kernel32.dll',[bool],[Type[]]@([IntPtr],[int],[IntPtr],[uint32])),
        @('GetFinalPathNameByHandleW','kernel32.dll',[uint32],[Type[]]@([IntPtr],[Text.StringBuilder],[uint32],[uint32])),
        @('CreateDirectoryW','kernel32.dll',[bool],[Type[]]@([string],[IntPtr])),
        @('GetProfilesDirectoryW','userenv.dll',[bool],[Type[]]@([Text.StringBuilder],[uint32].MakeByRefType())),
        @('CreateProfile','userenv.dll',[int],[Type[]]@([string],[string],[Text.StringBuilder],[uint32])),
        @('OpenProcessToken','advapi32.dll',[bool],[Type[]]@([IntPtr],[uint32],[IntPtr].MakeByRefType())),
        @('LookupPrivilegeValueW','advapi32.dll',[bool],[Type[]]@([string],[string],[IntPtr])),
        @('GetTokenInformation','advapi32.dll',[bool],[Type[]]@([IntPtr],[int],[IntPtr],[uint32],[IntPtr])),
        @('AdjustTokenPrivileges','advapi32.dll',[bool],[Type[]]@([IntPtr],[bool],[IntPtr],[uint32],[IntPtr],[IntPtr]))
    )
    foreach ($d in $definitions) {
        $m = $tb.DefineMethod($d[0],
            [Reflection.MethodAttributes]'Public,Static,PinvokeImpl',$d[2],$d[3])
        $attr = [Runtime.InteropServices.DllImportAttribute]
        $ctor = $attr.GetConstructor([Type[]]@([string]))
        $fields = [Reflection.FieldInfo[]]@(
            $attr.GetField('EntryPoint'),$attr.GetField('ExactSpelling'),
            $attr.GetField('CharSet'),$attr.GetField('CallingConvention'),
            $attr.GetField('SetLastError'))
        $values = [object[]]@($d[0],$true,
            [Runtime.InteropServices.CharSet]::Unicode,
            [Runtime.InteropServices.CallingConvention]::Winapi,$true)
        $m.SetCustomAttribute([Reflection.Emit.CustomAttributeBuilder]::new(
            $ctor,[object[]]@([IO.Path]::Combine([Environment]::SystemDirectory,$d[1])),$fields,$values))
        $m.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
    }
    return $tb.CreateType()
}
function ObjectInfo([IntPtr]$Handle,[bool]$Directory) {
    $buf = [Runtime.InteropServices.Marshal]::AllocHGlobal(24)
    try {
        Require ($Native::GetFileInformationByHandleEx($Handle,9,$buf,8))
        $attrs = [Runtime.InteropServices.Marshal]::ReadInt32($buf)
        Require (($attrs -band 0x400) -eq 0)
        Require ($Native::GetFileInformationByHandleEx($Handle,1,$buf,24))
        Require (([Runtime.InteropServices.Marshal]::ReadByte($buf,21) -ne 0) -eq $Directory)
        if (-not $Directory) { Require ([Runtime.InteropServices.Marshal]::ReadInt32($buf,16) -eq 1) }
        Require ($Native::GetFileInformationByHandleEx($Handle,18,$buf,24))
        $bytes = [byte[]]::new(24)
        [Runtime.InteropServices.Marshal]::Copy($buf,$bytes,0,24)
        return [Convert]::ToBase64String($bytes)
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($buf) }
}
function OpenPinned([string]$Path,[bool]$Directory=$true) {
    # READ_CONTROL | READ_ATTRIBUTES, share read/write but never share delete.
    # OPEN_REPARSE_POINT prevents following the final link.
    $h = $Native::CreateFileW($Path,0x20080,3,[IntPtr]::Zero,3,0x02200000,[IntPtr]::Zero)
    Require ($h -ne [IntPtr]::new(-1) -and $h -ne [IntPtr]::Zero)
    # Retain even a handle whose inspection fails; outer cleanup owns it.
    $pin = [pscustomobject]@{ Path=$Path; Handle=$h; Id=''; Directory=$Directory }
    $Pins.Add($pin)
    $id = ObjectInfo $h $Directory
    $b = [Text.StringBuilder]::new(1024)
    $n = $Native::GetFinalPathNameByHandleW($h,$b,1024,0)
    Require ($n -gt 0 -and $n -lt 1024)
    $final = $b.ToString()
    Require ($final.StartsWith('\\?\C:\',[StringComparison]::OrdinalIgnoreCase))
    Require (SamePath $Path $final.Substring(4))
    $pin.Id=$id
    return $pin
}
function AssertAcl([string]$Path,[bool]$Private=$false,[bool]$File=$false) {
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $trusted = @($SystemSid,$AdminsSid)
    if ($Sid) { $trusted += $Sid }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
    Require ($null -ne $raw.DiscretionaryAcl)
    $rules = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if ($Private) {
        Require ($owner -eq $Sid -and $acl.AreAccessRulesProtected -and $rules.Count -eq 3)
        foreach ($s in @($Sid,$SystemSid,$AdminsSid)) {
            $r = @($rules | Where-Object { $_.IdentityReference.Value -eq $s })
            Require ($r.Count -eq 1)
            Require ($r[0].AccessControlType -eq 'Allow' -and -not $r[0].IsInherited)
            Require ([int64]$r[0].FileSystemRights -eq 2032127)
            $flags = 3
            if ($File) { $flags = 0 }
            Require ([int]$r[0].InheritanceFlags -eq $flags -and [int]$r[0].PropagationFlags -eq 0)
        }
    } else {
        Require (($owner -in $trusted) -or ((SamePath $Path 'C:\') -and $owner -eq $TiSid))
        foreach ($r in $rules) {
            if ($r.AccessControlType -ne 'Allow' -or
                ([int]$r.PropagationFlags -band 2) -or $r.IdentityReference.Value -in $trusted) { continue }
            # Existing validator's parent mutation mask plus generic rights.
            Require (([int64]$r.FileSystemRights -band [int64]4027384144) -eq 0)
            # Immediate parents of NEW names must not permit third-party creation.
            if ((SamePath $Path 'C:\Users') -or (SamePath $Path $Profile)) {
                Require (([int64]$r.FileSystemRights -band 6) -eq 0)
            }
        }
    }
}
function Recheck {
    foreach ($pin in $Pins.ToArray()) {
        Require ((ObjectInfo $pin.Handle $pin.Directory) -eq $pin.Id)
        $item = Get-Item -LiteralPath $pin.Path -Force
        Require (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
        if (SamePath $pin.Path $Root) { AssertAcl $pin.Path $true }
        elseif ($pin.Path -eq $Marker) { AssertAcl $pin.Path $true $true }
        elseif ($Roles | Where-Object { SamePath $pin.Path ($Root+'\'+$_) }) { AssertAcl $pin.Path $true }
        else { AssertAcl $pin.Path }
    }
}
function StandardAccount($User) {
    Require ($User.PrincipalSource -eq 'Local' -and -not $User.Enabled -and $User.Description -eq $Revision)
    $groups = @()
    foreach ($g in @(Get-LocalGroup)) {
        $members = @(Get-LocalGroupMember -Group $g)
        # Reject indirect local privileges through nesting of Users.
        Require (@($members | Where-Object { $_.SID.Value -eq $UsersSid }).Count -eq 0)
        if (@($members | Where-Object { $_.SID.Value -eq $User.SID.Value }).Count) { $groups += $g.SID.Value }
    }
    Require ($groups.Count -eq 1 -and $groups[0] -eq $UsersSid)
}
function RegistryProfile([string]$ExpectedSid) {
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
        'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList')
    Require ($null -ne $key)
    try {
        $matches = @()
        foreach ($name in $key.GetSubKeyNames()) {
            $sub = $key.OpenSubKey($name)
            try {
                $v = $sub.GetValue('ProfileImagePath',$null)
                if ($v -is [string] -and (SamePath ([Environment]::ExpandEnvironmentVariables($v)) $Profile)) {
                    $matches += $name
                }
            } finally { if ($null -ne $sub) { $sub.Dispose() } }
        }
        if ($ExpectedSid) { Require ($matches.Count -eq 1 -and $matches[0] -eq $ExpectedSid) }
        else { Require ($matches.Count -eq 0) }
    } finally { $key.Dispose() }
}
function PrivilegeAttributes {
    $len = [Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    $buf = [IntPtr]::Zero
    try {
        [Runtime.InteropServices.Marshal]::WriteInt32($len,0)
        $null=$Native::GetTokenInformation($Token,3,[IntPtr]::Zero,0,$len)
        $size=[Runtime.InteropServices.Marshal]::ReadInt32($len)
        Require ($size -ge 4 -and $size -le 65536)
        $buf=[Runtime.InteropServices.Marshal]::AllocHGlobal($size)
        Require ($Native::GetTokenInformation($Token,3,$buf,$size,$len))
        $count=[Runtime.InteropServices.Marshal]::ReadInt32($buf)
        Require ($count -ge 0 -and (4+12*$count) -le $size)
        for ($i=0; $i -lt $count; $i++) {
            $off=4+12*$i
            if ([Runtime.InteropServices.Marshal]::ReadInt64($buf,$off) -eq
                [Runtime.InteropServices.Marshal]::ReadInt64($Luid)) {
                return [Runtime.InteropServices.Marshal]::ReadInt32($buf,$off+8)
            }
        }
        throw 'REFUSED'
    } finally {
        if ($buf -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($buf) }
        [Runtime.InteropServices.Marshal]::FreeHGlobal($len)
    }
}
function SetRestorePrivilege([int]$Attributes) {
    $state=[Runtime.InteropServices.Marshal]::AllocHGlobal(16)
    try {
        [Runtime.InteropServices.Marshal]::WriteInt32($state,0,1)
        [Runtime.InteropServices.Marshal]::WriteInt64($state,4,[Runtime.InteropServices.Marshal]::ReadInt64($Luid))
        [Runtime.InteropServices.Marshal]::WriteInt32($state,12,$Attributes)
        Require ($Native::AdjustTokenPrivileges($Token,$false,$state,0,[IntPtr]::Zero,[IntPtr]::Zero))
        # BOOL success alone does not prove assignment. Re-query the token.
        Require (((PrivilegeAttributes) -band 2) -eq ($Attributes -band 2))
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($state) }
}
function CleanupFailure([string]$Reason) {
    $script:ExitCode=1
    $CleanupFailures.Add($Reason)
}
function WithRestorePrivilege([scriptblock]$Action) {
    $original=PrivilegeAttributes
    Require (($original -band 2) -eq 0)
    try {
        SetRestorePrivilege ($original -bor 2)
        & $Action
    } finally {
        # Runs even if enabling, creation, or the caller's validation throws.
        # A failure propagates; outer cleanup retries the initial token state.
        try { SetRestorePrivilege $original }
        catch { CleanupFailure 'PRIVILEGE_SCOPE_RESTORE_UNPROVEN'; throw }
    }
}
function WithSecurity([bool]$File,[scriptblock]$Action) {
    $aceFlags='OICI'
    if ($File) { $aceFlags='' }
    $sddl="O:$Sid"+"G:SYD:P(A;$aceFlags;FA;;;$Sid)(A;$aceFlags;FA;;;SY)(A;$aceFlags;FA;;;BA)"
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new($sddl)
    $bytes=[byte[]]::new($sd.BinaryLength)
    $sd.GetBinaryForm($bytes,0)
    $gc=[Runtime.InteropServices.GCHandle]::Alloc($bytes,[Runtime.InteropServices.GCHandleType]::Pinned)
    $sa=[IntPtr]::Zero
    try {
        $sa=[Runtime.InteropServices.Marshal]::AllocHGlobal(24) # SECURITY_ATTRIBUTES, x64 only.
        for ($i=0; $i -lt 24; $i++) { [Runtime.InteropServices.Marshal]::WriteByte($sa,$i,0) }
        [Runtime.InteropServices.Marshal]::WriteInt32($sa,0,24)
        [Runtime.InteropServices.Marshal]::WriteIntPtr($sa,8,$gc.AddrOfPinnedObject())
        & $Action $sa
    } finally {
        try { if ($sa -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($sa) } }
        finally { if ($gc.IsAllocated) { $gc.Free() } }
    }
}
function NewPrivateDirectory([string]$Path) {
    Recheck
    Require (Missing $Path)
    # Atomic create: FALSE on an existing name, no Set-Acl on existing objects.
    WithSecurity $false {
        param($sa)
        WithRestorePrivilege {
            Require ($Native::CreateDirectoryW($Path,$sa))
        }
    }
    $null=OpenPinned $Path
    AssertAcl $Path $true
    Recheck
}
function AssertInventory {
    $names=@(Get-ChildItem -LiteralPath $Root -Force | ForEach-Object Name)
    Require ($names.Count -eq 6 -and @($names | Where-Object { $_ -notin ($Roles+@('provisioning.json')) }).Count -eq 0)
    foreach ($role in $Roles) { Require (@(Get-ChildItem -LiteralPath ($Root+'\'+$role) -Force).Count -eq 0) }
}
try {
    Require ($MyInvocation.InvocationName -ne '.')
    Require ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -ge 1)
    Require ([Environment]::Is64BitProcess -and $ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage')
    Require ($ApprovedScriptSha256 -match '^[0-9a-fA-F]{64}$')
    AssertOutsideCodex
    Require ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash -eq $ApprovedScriptSha256)
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=[Security.Principal.WindowsPrincipal]::new($identity)
    Require ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
    Require ($identity.Name.Split('\')[-1] -ne $Name)
    Require (SamePath $PSHOME ([IO.Path]::Combine([Environment]::SystemDirectory,'WindowsPowerShell\v1.0')))
    Import-Module ([IO.Path]::Combine($PSHOME,'Modules\Microsoft.PowerShell.LocalAccounts\Microsoft.PowerShell.LocalAccounts.psd1')) -ErrorAction Stop
    $drive=[IO.DriveInfo]::new('C:\')
    Require ($drive.IsReady -and $drive.DriveType -eq 'Fixed' -and $drive.DriveFormat -eq 'NTFS')
    $Native=ImportNative
    $capacity=[uint32]1024; $profiles=[Text.StringBuilder]::new(1024)
    Require ($Native::GetProfilesDirectoryW($profiles,[ref]$capacity))
    Require (SamePath $profiles.ToString() 'C:\Users')
    foreach ($p in @('C:\','C:\Users')) { $null=OpenPinned $p; AssertAcl $p }
    $user=LocalAccount
    if ($null -ne $user) {
        # Existing account: read-only attestation only; no password/ACL repair.
        $Sid=$user.SID.Value
        StandardAccount $user
        RegistryProfile $Sid
        $null=OpenPinned $Profile; AssertAcl $Profile
        $null=OpenPinned $Root; AssertAcl $Root $true
        foreach ($r in $Roles) { $null=OpenPinned ($Root+'\'+$r); AssertAcl ($Root+'\'+$r) $true }
        $null=OpenPinned $Marker $false; AssertAcl $Marker $true $true
        AssertInventory
        Require ((Get-Item -LiteralPath $Marker -Force).Length -le 32768)
        $saved=[IO.File]::ReadAllText($Marker,[Text.Encoding]::UTF8) | ConvertFrom-Json
        Require (@($saved.PSObject.Properties | ForEach-Object Name).Count -eq 6)
        Require ($saved.revision -eq $Revision -and $saved.sid -eq $Sid)
        Require ($saved.approval -eq $ApprovalCommentId -and $saved.source_sha256 -eq $ApprovedScriptSha256.ToLowerInvariant())
        Require ($saved.account_disabled -eq $true -and $saved.ids.Count -eq $Pins.Count)
        foreach ($pin in $Pins.ToArray()) {
            $row=@($saved.ids | Where-Object { $_.path -eq $pin.Path })
            Require ($row.Count -eq 1 -and $row[0].id -eq $pin.Id)
            Require (@($row[0].PSObject.Properties | ForEach-Object Name).Count -eq 2)
        }
        Recheck
        $Code='ALREADY_PREPARED_DISABLED'; $ExitCode=0
    } else {
        Require (Missing $Profile)
        RegistryProfile ''
        # Refuse local group nesting before creating anything.
        foreach ($g in @(Get-LocalGroup)) {
            Require (@(Get-LocalGroupMember -Group $g | Where-Object { $_.SID.Value -eq $UsersSid }).Count -eq 0)
        }
        Require ($Native::OpenProcessToken([IntPtr]::new(-1),0x28,[ref]$Token))
        $Luid=[Runtime.InteropServices.Marshal]::AllocHGlobal(8)
        Require ($Native::LookupPrivilegeValueW($null,'SeRestorePrivilege',$Luid))
        $OldPrivilege=PrivilegeAttributes
        # Refuse an already-enabled restore privilege. No elevated restore
        # capability is retained across the interactive password prompt.
        Require (($OldPrivilege -band 2) -eq 0)
        $Password=Read-Host 'New account password (local only)' -AsSecureString
        Require ($Password.Length -ge 1)
        Recheck
        Require ($null -eq (LocalAccount) -and (Missing $Profile))
        $Code='PARTIAL_REVIEW_REQUIRED'; $Changed=$true
        $user=New-LocalUser -Name $Name -Password $Password -Disabled -Description $Revision
        $Sid=$user.SID.Value
        Require ($Sid -match '^S-1-5-21-(\d+-){3}\d+$' -and -not $user.Enabled)
        $users=Get-LocalGroup -SID $UsersSid
        if (-not @(Get-LocalGroupMember -Group $users | Where-Object { $_.SID.Value -eq $Sid }).Count) {
            Add-LocalGroupMember -Group $users -Member $user
        }
        StandardAccount (Get-LocalUser -SID $user.SID)
        Recheck
        Require (Missing $Profile)
        RegistryProfile ''
        $out=[Text.StringBuilder]::new(1024)
        Require ($Native::CreateProfile($Sid,$Name,$out,1024) -eq 0)
        Require (SamePath $out.ToString() $Profile)
        RegistryProfile $Sid
        $null=OpenPinned $Profile; AssertAcl $Profile
        # The profile ACL is inspected, never rewritten.
        NewPrivateDirectory $Root
        foreach ($r in $Roles) { NewPrivateDirectory ($Root+'\'+$r) }
        Require (-not (Get-LocalUser -SID $user.SID).Enabled)
        $manifest=[ordered]@{
            revision=$Revision; sid=$Sid; approval=$ApprovalCommentId
            source_sha256=$ApprovedScriptSha256.ToLowerInvariant()
            account_disabled=$true; ids=@($Pins | ForEach-Object { @{path=$_.Path;id=$_.Id} })
        }
        Require (Missing $Marker)
        WithSecurity $true {
            param($sa)
            $created=[pscustomobject]@{Handle=[IntPtr]::Zero}
            $safe=$null; $stream=$null
            try {
                WithRestorePrivilege {
                    $created.Handle=$Native::CreateFileW($Marker,0x40020080,0,$sa,1,0x80,[IntPtr]::Zero)
                    Require ($created.Handle -ne [IntPtr]::new(-1) -and $created.Handle -ne [IntPtr]::Zero)
                }
                # The holder is populated before restoration can throw.
                $h=$created.Handle
                $manifest.ids += @{path=$Marker;id=(ObjectInfo $h $false)}
                $safe=[Microsoft.Win32.SafeHandles.SafeFileHandle]::new($h,$false)
                $stream=[IO.FileStream]::new($safe,[IO.FileAccess]::Write)
                $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($manifest | ConvertTo-Json -Depth 6 -Compress))
                $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true)
            } finally {
                try {
                    if ($null -ne $stream) { $stream.Dispose() }
                } finally {
                    try { if ($null -ne $safe) { $safe.Dispose() } }
                    finally {
                        $h=$created.Handle
                        if ($h -ne [IntPtr]::Zero -and $h -ne [IntPtr]::new(-1)) {
                            try { Require ($Native::CloseHandle($h)) }
                            catch { CleanupFailure 'MANIFEST_HANDLE_CLOSE_UNPROVEN'; throw }
                        }
                    }
                }
            }
        }
        $null=OpenPinned $Marker $false; AssertAcl $Marker $true $true
        AssertInventory
        Recheck
        $Code='PREPARED_DISABLED'; $ExitCode=0
    }
} catch {
    # Never print exception, SDDL, real SID, password, paths or registry content.
    $ExitCode=1
} finally {
    # Each cleanup is isolated: one failure cannot skip the remaining resources.
    try {
        try { if ($null -ne $Password) { $Password.Dispose() } }
        catch { CleanupFailure 'PASSWORD_DISPOSE_UNPROVEN' }
        if ($null -ne $OldPrivilege -and $Token -ne [IntPtr]::Zero) {
            try { SetRestorePrivilege $OldPrivilege }
            catch { CleanupFailure 'PRIVILEGE_RESTORE_UNPROVEN' }
        }
        foreach ($pin in $Pins.ToArray()) {
            try { Require ($Native::CloseHandle($pin.Handle)) }
            catch { CleanupFailure 'PIN_HANDLE_CLOSE_UNPROVEN' }
        }
        if ($Luid -ne [IntPtr]::Zero) {
            try { [Runtime.InteropServices.Marshal]::FreeHGlobal($Luid) }
            catch { CleanupFailure 'LUID_BUFFER_FREE_UNPROVEN' }
        }
        if ($Token -ne [IntPtr]::Zero) {
            try { Require ($Native::CloseHandle($Token)) }
            catch { CleanupFailure 'TOKEN_HANDLE_CLOSE_UNPROVEN' }
        }
        if ($Sid) {
            try {
                $u=Get-LocalUser -SID ([Security.Principal.SecurityIdentifier]::new($Sid))
                Require ($u.Name -eq $Name -and -not $u.Enabled)
                $AccountDisabledVerified=$true
            } catch {
                $AccountDisabledVerified=$false
                CleanupFailure 'ACCOUNT_DISABLED_UNPROVEN'
            }
        } elseif ($Changed) {
            # Creation may have failed after SAM accepted it, before SID capture.
            # Never guess, adopt, enable or rewrite that account.
            CleanupFailure 'ACCOUNT_IDENTITY_UNPROVEN'
        }
    } catch {
        $ExitCode=1
        $Code='CLEANUP_UNPROVEN'
    } finally {
        # Even an unexpected cleanup or reporting exception terminates this host.
        try {
            $phase=$Code
            if ($CleanupFailures.Count) { $Code='CLEANUP_UNPROVEN'; $ExitCode=1 }
            [ordered]@{
                code=$Code;phase=$phase;mutation_started=$Changed
                account_disabled_verified=$AccountDisabledVerified
                cleanup_failures=@($CleanupFailures.ToArray())
                telegram_enabled=$false
            } | ConvertTo-Json -Compress
        } catch { $ExitCode=1 }
        finally { [Environment]::Exit($ExitCode) }
    }
}
