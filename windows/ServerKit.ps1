#Requires -Version 5.1
<#
.SYNOPSIS
    Run the full ServerKit panel on Windows.
.DESCRIPTION
    ServerKit manages a Linux server: nginx, systemd services, the firewall,
    cron, Docker, mail. None of that exists on Windows, so instead of
    rewriting those features this launcher gives the panel the Linux host it
    expects: a dedicated WSL2 distro (named "ServerKit", separate from any
    other distro on this PC) with systemd enabled, installed by the unmodified
    upstream install.sh. Every feature therefore behaves exactly as it does on
    a VPS, and the panel is reached from Windows at http://localhost.

    Messages are Turkish without diacritics so they survive any console
    code page under Windows PowerShell 5.1.
.EXAMPLE
    .\ServerKit.ps1 install
.EXAMPLE
    .\ServerKit.ps1 start
.EXAMPLE
    .\ServerKit.ps1 cli list-users
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('menu', 'install', 'start', 'stop', 'restart', 'status', 'open',
                 'setup-code', 'logs', 'shell', 'update', 'cli', 'theme',
                 'autostart-on', 'autostart-off', 'ensure', 'remote-on', 'remote-off', 'remote-link', 'sync-on', 'sync-off', 'sync-now', 'gateway-on', 'gateway-off', 'gateway-prompt', 'gateway-rotate',
                 'uninstall', 'help')]
    [string]$Command = 'menu',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest,

    [ValidateSet('minimal', 'standard', 'full')]
    [string]$Profile = 'standard',

    [string]$Distro = 'ServerKit',
    [string]$Image = 'Ubuntu-24.04',
    [switch]$NoBrowser,
    [switch]$Yes
)

$ErrorActionPreference = 'Continue'
$env:WSL_UTF8 = '1'

$RepoRoot = Split-Path -Parent $PSScriptRoot

# Owner-specific values live in windows\serverkit-windows.json (copy of
# serverkit-windows.example.json). install/start work without it; the gateway,
# sync and remote-publish commands require it (Get-SwkConfig -Required).
$ConfigFile = if ($env:SERVERKIT_WINDOWS_CONFIG) { $env:SERVERKIT_WINDOWS_CONFIG } else { Join-Path $PSScriptRoot 'serverkit-windows.json' }
function Get-SwkConfig {
    param([switch]$Required)
    if (-not (Test-Path $ConfigFile)) {
        if ($Required) {
            Write-Host "  x Ayar dosyasi yok: $ConfigFile" -ForegroundColor Red
            Write-Host '    windows\serverkit-windows.example.json dosyasini windows\serverkit-windows.json olarak kopyalayip kendi degerlerinizi yazin.' -ForegroundColor Yellow
            exit 1
        }
        return $null
    }
    try { return (Get-Content -Raw -Encoding UTF8 $ConfigFile | ConvertFrom-Json) }
    catch {
        Write-Host "  x Ayar dosyasi okunamadi ($ConfigFile): $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}
$Cfg = Get-SwkConfig
if ($Cfg) {
    if (-not $PSBoundParameters.ContainsKey('Distro') -and $Cfg.wsl -and $Cfg.wsl.distro) { $Distro = $Cfg.wsl.distro }
}
$PanelBase = if ($Cfg -and $Cfg.panel -and $Cfg.panel.url) { ([string]$Cfg.panel.url).TrimEnd('/') } else { 'http://localhost' }
$PanelUrl = $PanelBase
$HealthUrl = "$PanelBase/api/v1/system/health"
$KeepAliveArgs = "-d $Distro --exec /bin/sleep infinity"
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'ServerKit.lnk'
$WatchdogTask = 'ServerKit Watchdog'
$SyncTask = 'ServerKit Site Sync'

function Write-Step { param([string]$Text) Write-Host "  > $Text" -ForegroundColor Cyan }
function Write-Good { param([string]$Text) Write-Host "  + $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  ! $Text" -ForegroundColor Yellow }
function Stop-WithError {
    param([string]$Text)
    Write-Host "  x $Text" -ForegroundColor Red
    exit 1
}

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    $full = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($full -notmatch '^([A-Za-z]):\\(.*)$') {
        Stop-WithError "Bu klasor bir surucu harfi altinda degil: $full"
    }
    return '/mnt/' + $Matches[1].ToLower() + '/' + ($Matches[2] -replace '\\', '/')
}

function Test-Wsl {
    return [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
}

function Get-Distros {
    $lines = & wsl.exe -l -q 2>$null
    return @($lines | ForEach-Object { "$_".Replace("`0", '').Trim() } | Where-Object { $_ })
}

function Test-Distro {
    return (Get-Distros) -contains $Distro
}

function Test-DistroRunning {
    $lines = & wsl.exe -l -q --running 2>$null
    $running = @($lines | ForEach-Object { "$_".Replace("`0", '').Trim() } | Where-Object { $_ })
    return $running -contains $Distro
}

# Run the Linux-side helper as root. Output streams straight to the console.
function Invoke-Setup {
    param([string[]]$SetupArgs)
    $script = ConvertTo-WslPath (Join-Path $PSScriptRoot 'wsl-setup.sh')
    # Out-Host: keep the command's output out of this function's return value.
    & wsl.exe -d $Distro -u root --exec bash $script @SetupArgs | Out-Host
    return $LASTEXITCODE
}

function Invoke-Linux {
    param([string[]]$LinuxArgs)
    & wsl.exe -d $Distro -u root --exec @LinuxArgs | Out-Host
    return $LASTEXITCODE
}

# WSL stops a distro shortly after its last session ends, even with systemd
# services running. One hidden, idle session keeps the panel up.
function Get-KeepAlive {
    return @(Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($KeepAliveArgs) })
}

function Start-KeepAlive {
    if ((Get-KeepAlive).Count -gt 0) { return }
    Start-Process -FilePath 'wsl.exe' -ArgumentList $KeepAliveArgs -WindowStyle Hidden | Out-Null
}

function Stop-KeepAlive {
    foreach ($p in (Get-KeepAlive)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Test-PanelHealth {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-PanelHealth {
    param([int]$Seconds = 90)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PanelHealth) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-Port80Busy {
    $c = Get-NetTCPConnection -State Listen -LocalPort 80 -ErrorAction SilentlyContinue
    if (-not $c) { return $null }
    $owner = $c | Select-Object -First 1 -ExpandProperty OwningProcess
    $proc = Get-Process -Id $owner -ErrorAction SilentlyContinue
    # wslrelay / wslhost is WSL's own localhost forwarder, i.e. our panel.
    if ($proc -and $proc.ProcessName -match '^(wslrelay|wslhost|wsl)$') { return $null }
    if ($proc) { return $proc.ProcessName }
    return "PID $owner"
}

function Assert-Installed {
    if (-not (Test-Wsl)) { Stop-WithError 'WSL kurulu degil. Once: ServerKit.bat -> Kur' }
    if (-not (Test-Distro)) { Stop-WithError "ServerKit henuz kurulmamis. Once: ServerKit.bat -> Kur" }
}

function Show-SetupCode {
    $out = & wsl.exe -d $Distro -u root --exec /usr/local/bin/serverkit setup-code 2>$null
    $code = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$' }) |
        Select-Object -Last 1
    if ($code) {
        Write-Host ''
        Write-Host "  Kurulum kodu:  $code" -ForegroundColor White
        Write-Host '  Ilk acilista yonetici hesabi olustururken bu kod sorulur.'
    } else {
        Write-Good 'Yonetici hesabi zaten olusturulmus (kurulum kodu gerekmiyor).'
    }
}

function Invoke-Install {
    Write-Host ''
    Write-Host '  ServerKit - Windows kurulumu' -ForegroundColor White
    Write-Host '  ----------------------------'

    if (-not (Test-Wsl)) {
        Write-Warn 'Bu bilgisayarda WSL yok.'
        Write-Host '  Yonetici olarak bir PowerShell acip su komutu calistirin, bilgisayari'
        Write-Host '  yeniden baslatin, sonra kurulumu tekrar baslatin:'
        Write-Host '      wsl --install --no-distribution' -ForegroundColor White
        exit 1
    }

    $busy = Test-Port80Busy
    if ($busy) {
        Stop-WithError "Windows'ta 80 numarali kapiyi '$busy' kullaniyor. Panel http://localhost adresinden acilamaz; once onu kapatin."
    }

    if (Test-Distro) {
        Write-Good "'$Distro' Linux ortami zaten var; uzerine kurulum/onarim yapiliyor."
    } else {
        Write-Step "'$Distro' adinda ayri bir Linux ortami olusturuluyor ($Image, ~1 GB indirme)..."
        & wsl.exe --install $Image --name $Distro --no-launch
        if ($LASTEXITCODE -ne 0 -or -not (Test-Distro)) {
            Write-Warn 'Ortam olusturulamadi. WSL eski olabilir; su komutla guncelleyip tekrar deneyin:'
            Write-Host '      wsl --update' -ForegroundColor White
            exit 1
        }
        Write-Good 'Linux ortami olusturuldu.'
    }

    Write-Step 'Linux ortami sunucu gibi davranacak sekilde ayarlaniyor (systemd)...'
    $state = (& wsl.exe -d $Distro -u root --exec bash (ConvertTo-WslPath (Join-Path $PSScriptRoot 'wsl-setup.sh')) configure) | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'Linux ortami ayarlanamadi.' }
    if ("$state".Trim() -eq 'changed' -or -not (Test-DistroRunning)) {
        Stop-KeepAlive
        & wsl.exe --terminate $Distro | Out-Null
        Start-Sleep -Seconds 3
    }
    Start-KeepAlive
    if ((Invoke-Setup @('wait-systemd')) -ne 0) { Stop-WithError 'systemd baslamadi. "wsl --update" calistirip tekrar deneyin.' }
    Write-Good 'Linux ortami hazir.'

    Write-Step "ServerKit kuruluyor (profil: $Profile). Bu 5-15 dakika surebilir..."
    $env:SERVERKIT_PROFILE = $Profile
    $env:WSLENV = (@($env:WSLENV, 'SERVERKIT_PROFILE') | Where-Object { $_ }) -join ':'
    $code = Invoke-Setup @('install', (ConvertTo-WslPath $RepoRoot))
    if ($code -ne 0) { Stop-WithError "Kurulum hata verdi (kod $code). Yukaridaki ciktiya bakin; komutu tekrar calistirmak guvenlidir." }

    Write-Step "Panel Windows'tan deneniyor ($PanelUrl)..."
    if (-not (Wait-PanelHealth -Seconds 90)) {
        Stop-WithError "Kurulum bitti ama panel $PanelUrl adresinden cevap vermiyor. 'ServerKit.ps1 status' ile bakin."
    }
    Write-Good "Panel calisiyor: $PanelUrl"
    Show-SetupCode
    Write-Host ''
    Write-Host '  Baslat / durdur / durum icin: ServerKit.bat'
    if (-not $NoBrowser) { Start-Process $PanelUrl }
}

function Invoke-Start {
    Assert-Installed
    Write-Step 'ServerKit baslatiliyor...'
    Start-KeepAlive
    if ((Invoke-Setup @('wait-systemd')) -ne 0) { Stop-WithError 'Linux ortami baslamadi.' }
    if (-not (Wait-PanelHealth -Seconds 90)) {
        Stop-WithError "Panel $PanelUrl adresinden cevap vermiyor. 'ServerKit.ps1 status' ve 'ServerKit.ps1 logs' ile bakin."
    }
    Write-Good "Panel calisiyor: $PanelUrl"
    if (-not $NoBrowser) { Start-Process $PanelUrl }
}

function Invoke-Stop {
    Assert-Installed
    Write-Step 'ServerKit durduruluyor...'
    Stop-KeepAlive
    & wsl.exe --terminate $Distro | Out-Null
    Write-Good 'Durduruldu. (Barindirilan siteler ve uygulamalar da durdu.)'
}

function Invoke-Status {
    if (-not (Test-Wsl)) { Write-Warn 'WSL kurulu degil.'; return }
    if (-not (Test-Distro)) { Write-Warn 'ServerKit henuz kurulmamis.'; return }
    if (-not (Test-DistroRunning)) {
        Write-Warn 'ServerKit kapali. Baslatmak icin: ServerKit.ps1 start'
        return
    }
    Invoke-Setup @('status') | Out-Null
    if (Test-PanelHealth) {
        Write-Good "Panel Windows'tan erisilebilir: $PanelUrl"
    } else {
        Write-Warn "Panel $PanelUrl adresinden cevap vermiyor."
    }
    if (Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue) {
        Write-Good 'Otomatik baslatma / bekci: ACIK (oturum acilinca, ag geri gelince ve 2 dakikada bir kontrol)'
    } else {
        Write-Host '    Otomatik baslatma / bekci: kapali'
    }
}

# Watchdog: bring the panel back whenever it is not answering. Cheap when all
# is well (one HTTP probe). Escalates: keep-alive session -> wait -> restart
# the distro. Logged to %LOCALAPPDATA%\ServerKit\watchdog.log.
function Invoke-Ensure {
    if (-not (Test-Wsl) -or -not (Test-Distro)) { return }
    if (Test-PanelHealth) { Start-KeepAlive; return }

    $logDir = Join-Path $env:LOCALAPPDATA 'ServerKit'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $log = Join-Path $logDir 'watchdog.log'
    function Write-Log { param([string]$Text) Add-Content -Path $log -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $Text) }

    Write-Log 'panel not answering; starting keep-alive'
    Start-KeepAlive
    Invoke-Setup @('wait-systemd') | Out-Null
    if (Wait-PanelHealth -Seconds 60) { Write-Log 'panel back'; return }

    Write-Log 'still not answering; restarting the Linux environment'
    Stop-KeepAlive
    & wsl.exe --terminate $Distro | Out-Null
    Start-Sleep -Seconds 3
    Start-KeepAlive
    Invoke-Setup @('wait-systemd') | Out-Null
    if (Wait-PanelHealth -Seconds 120) { Write-Log 'panel back after restart' } else { Write-Log 'panel STILL down after restart' }
}

# Remote link: a Cloudflare quick tunnel to the panel (windows/remote-link.sh).
# The address is random and changes whenever the tunnel restarts.
function Invoke-Remote {
    param([string]$Action)
    Assert-Installed
    Start-KeepAlive
    if ($Action -eq 'on') {
        $src = ConvertTo-WslPath (Join-Path $PSScriptRoot 'remote-link.sh')
        Invoke-Linux @('install', '-m', '755', $src, '/usr/local/sbin/serverkit-remote-link') | Out-Null
    }
    $out = & wsl.exe -d $Distro -u root --exec /usr/local/sbin/serverkit-remote-link $Action 2>$null
    $url = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^https://' }) | Select-Object -Last 1
    if ($url) {
        Write-Good "Paneli her yerden acmak icin: $url"
        Write-Host '    (Adres tunel her yeniden basladiginda degisir; guncelini: ServerKit.bat remote-link)'
    } elseif ($Action -eq 'off') {
        Write-Good 'Uzaktan baglanti kapatildi.'
    } else {
        Write-Warn 'Uzaktan baglanti kapali. Acmak icin: ServerKit.bat remote-on'
    }
}

# Auto-deploy fallback (windows/sites/sync.py): default auto_deploy_mode is
# "webhook" (the panel's own GitHub webhook, registered by publish.py), which
# needs no scheduled task. With auto_deploy_mode "poll" this task checks GitHub
# every 3 minutes and republishes sites whose panel "Auto deploy" switch is on.
function Set-SiteSync {
    param([bool]$Enable)
    if (-not $Enable) {
        Unregister-ScheduledTask -TaskName $SyncTask -Confirm:$false -ErrorAction SilentlyContinue
        Write-Good 'GitHub otomatik yayin kontrolu kapatildi.'
        return
    }
    $conf = Get-SwkConfig -Required
    if ($conf.auto_deploy_mode -ne 'poll') {
        Write-Warn 'auto_deploy_mode "webhook": otomatik yayin panelin kendi GitHub webhook ile calisir, zamanlanmis gorev gerekmez.'
        Write-Warn 'Eski 3 dakikalik kontrolu istiyorsaniz serverkit-windows.json icinde "auto_deploy_mode": "poll" yapip tekrar deneyin.'
        return
    }
    $python = $null
    if ($conf.windows -and $conf.windows.python) { $python = [string]$conf.windows.python }
    if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
    if (-not $python -or -not (Test-Path $python)) { Stop-WithError 'python.exe bulunamadi (serverkit-windows.json: windows.python).' }
    $script = Join-Path $PSScriptRoot 'sites\sync.py'
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless `"$python`" `"$script`"" -WorkingDirectory (Join-Path $PSScriptRoot 'sites')
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $every3 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 3)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $SyncTask -Action $action -Trigger $every3 -Settings $settings -Principal $principal `
        -Description 'Republishes ServerKit sites whose GitHub branch moved (panel Auto deploy switch on).' -Force | Out-Null
    Write-Good 'Acik: 3 dakikada bir GitHub kontrol edilir; panelde "Auto deploy" acik olan siteler yeni commit gelince yeniden yayinlanir.'
}

# Publish gateway for AIs that are not on this PC (windows/gateway):
# https://<gateway_label>.<domain>, bearer-token protected.
function Invoke-Gateway {
    param([string]$Action)
    $conf = Get-SwkConfig -Required
    $gwHost = "$($conf.domain.gateway_label).$($conf.domain.base)"
    $gwMode = if ($conf.gateway -and $conf.gateway.mode) { [string]$conf.gateway.mode } else { 'local' }
    $owner = @($conf.github.owners)[0]
    Assert-Installed
    Start-KeepAlive
    $installer = ConvertTo-WslPath (Join-Path $PSScriptRoot 'gateway\install.sh')
    switch ($Action) {
        'on'     { Invoke-Linux @('bash', $installer, 'install') | Out-Null; Write-Good "Gecit acik ($gwMode): https://$gwHost" }
        'off'    { Invoke-Linux @('bash', $installer, 'off') | Out-Null; Write-Good 'Gecit kapatildi.' }
        'rotate' { Invoke-Linux @('bash', $installer, 'rotate') | Out-Null; Write-Good 'Yeni anahtar uretildi; eskisi artik calismaz. Yeni metin icin: ServerKit.bat gateway-prompt' }
        'prompt' {
            $token = (& wsl.exe -d $Distro -u root --exec bash $installer token 2>$null | Select-Object -Last 1).Trim()
            if ($gwMode -eq 'core') {
                # core mode: the client's key is a per-user panel API key, not the gateway token.
                Write-Warn 'gateway.mode "core": anahtar panelden alinan kisisel API anahtaridir (Ayarlar > API); metne <PANEL-API-ANAHTARI> yazildi.'
                $token = '<PANEL-API-ANAHTARI>'
            } elseif (-not $token) { Stop-WithError 'Anahtar okunamadi. Once: ServerKit.bat gateway-on' }
            $template = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'gateway\PROMPT-TEMPLATE.txt')
            $template = $template.Replace('{{GATEWAY_HOST}}', $gwHost).Replace('{{DOMAIN}}', [string]$conf.domain.base).Replace('{{GITHUB_OWNER}}', [string]$owner)
            $out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'serverkit-yayin-metni.txt'
            [IO.File]::WriteAllText($out, $template.Replace('{{TOKEN}}', $token), (New-Object Text.UTF8Encoding $false))
            Write-Good "Bulut yapay zekasina verilecek metin masaustunde: $out"
            # claude.ai custom connectors take only an address, so the key rides in the path.
            $conn = Join-Path ([Environment]::GetFolderPath('Desktop')) 'serverkit-claude-baglayici.txt'
            $connText = "Claude.ai > Ayarlar > Baglayicilar > Ozel baglayici ekle`r`n`r`nAd: ServerKit`r`nUzak MCP sunucu URL:`r`nhttps://$gwHost/mcp/$token`r`n`r`nBu adres baglanti anahtarini icerir: kimseyle paylasma. Anahtari degistirirsen bu adres de degisir.`r`n"
            [IO.File]::WriteAllText($conn, $connText, (New-Object Text.UTF8Encoding $false))
            Write-Good "Claude.ai baglayici adresi masaustunde: $conn"
            Write-Warn 'Bu dosyada baglanti anahtari var; kimseyle paylasma, isin bitince silebilirsin.'
            Start-Process notepad.exe $out
        }
    }
}

function Set-Autostart {
    param([bool]$Enable)
    # Older versions used a Startup-folder shortcut; the task replaces it.
    if (Test-Path $StartupLink) { Remove-Item $StartupLink -Force }
    if (-not $Enable) {
        Unregister-ScheduledTask -TaskName $WatchdogTask -Confirm:$false -ErrorAction SilentlyContinue
        Write-Good 'Otomatik baslatma ve bekci kapatildi.'
        return
    }
    Assert-Installed

    # conhost --headless runs PowerShell with no window at all.
    $psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" ensure -Distro $Distro"
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe $psArgs" -WorkingDirectory $PSScriptRoot
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $every2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
    # "Connected to a network" (NetworkProfile event 10000): react as soon as
    # the internet comes back instead of waiting for the next 2-minute tick.
    $eventClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
    $onNetwork = New-CimInstance -CimClass $eventClass -ClientOnly
    $onNetwork.Enabled = $true
    $onNetwork.Subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[(EventID=10000)]]</Select></Query></QueryList>'
    $onNetwork.Delay = 'PT15S'
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $WatchdogTask -Action $action -Trigger @($atLogon, $every2, $onNetwork) `
        -Settings $settings -Principal $principal -Description 'Keeps the ServerKit panel and its sites running.' -Force | Out-Null
    Write-Good 'Acik: Windows oturumu acilinca, internet/ag geri gelince ve 2 dakikada bir panel kontrol edilip gerekirse yeniden baslatilacak.'
}

function Invoke-Theme {
    Assert-Installed
    Start-KeepAlive
    $file = @($Rest | Where-Object { $_ }) | Select-Object -First 1
    if (-not $file) { $file = Join-Path $PSScriptRoot 'themes\mono-square.json' }
    if (-not (Test-Path $file)) { Stop-WithError "Tema dosyasi bulunamadi: $file" }
    if ((Invoke-Setup @('theme', (ConvertTo-WslPath $file))) -ne 0) { Stop-WithError 'Tema uygulanamadi.' }
    Write-Good 'Tema uygulandi. Tarayicida sayfayi yenileyin (Ayarlar > Gorunum bolumunden degistirilebilir).'
}

function Invoke-Uninstall {
    if (-not (Test-Distro)) { Write-Warn 'Kaldirilacak bir kurulum yok.'; return }
    Write-Warn "Bu islem '$Distro' Linux ortamini ICINDEKI HER SEYLE (siteler, veritabanlari, yedekler) kalici olarak siler."
    if (-not $Yes) {
        $answer = Read-Host "  Onaylamak icin SIL yazin"
        if ($answer -cne 'SIL') { Write-Host '  Vazgecildi.'; return }
    }
    Stop-KeepAlive
    & wsl.exe --terminate $Distro | Out-Null
    & wsl.exe --unregister $Distro
    Unregister-ScheduledTask -TaskName $WatchdogTask -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $StartupLink) { Remove-Item $StartupLink -Force }
    Write-Good 'ServerKit kaldirildi.'
}

function Show-Help {
    Write-Host ''
    Write-Host '  ServerKit.ps1 <komut>' -ForegroundColor White
    Write-Host ''
    Write-Host '    install         Kur (veya mevcut kurulumu onar). -Profile minimal|standard|full'
    Write-Host '    start           Baslat ve tarayicida ac'
    Write-Host '    stop            Durdur'
    Write-Host '    restart         Yeniden baslat'
    Write-Host '    status          Durumu goster'
    Write-Host '    open            Paneli tarayicida ac'
    Write-Host '    setup-code      Ilk yonetici hesabi icin kurulum kodunu goster'
    Write-Host '    logs            Panel gunlugunu canli izle (Ctrl+C ile cik)'
    Write-Host '    shell           Linux ortaminda root kabugu ac'
    Write-Host '    update          ServerKit''i guncelle'
    Write-Host '    cli <...>       serverkit komut satiri aracini calistir (or: cli list-users)'
    Write-Host '    theme [dosya]   Siyah-beyaz kose temayi (veya verilen theme.json) panele uygula'
    Write-Host '    autostart-on    Oturum acilinca, ag geri gelince ve 2 dk bir kontrol edip gerekirse baslat'
    Write-Host '    autostart-off   Otomatik baslamayi kapat'
    Write-Host '    remote-on       Paneli internetten acilabilir yap (https://...trycloudflare.com)'
    Write-Host '    remote-link     Guncel uzaktan baglanti adresini goster'
    Write-Host '    remote-off      Uzaktan baglantiyi kapat'
    Write-Host '    sync-on/off     Yedek: 3 dk bir GitHub kontrolu (yalnizca auto_deploy_mode "poll"; varsayilan webhook)'
    Write-Host '    sync-now        GitHub kontrolunu simdi calistir'
    Write-Host '    gateway-prompt  Bulut yapay zekasina verilecek yayin metnini (anahtarli) masaustune yaz'
    Write-Host '    gateway-rotate  Baglanti anahtarini degistir (eskisi gecersiz olur)'
    Write-Host '    gateway-on/off  Uzaktan yayin gecidini ac / kapat'
    Write-Host '    uninstall       Linux ortamini ve icindeki her seyi sil'
    Write-Host ''
}

function Show-Menu {
    while ($true) {
        Write-Host ''
        Write-Host '  ===== ServerKit (Windows) =====' -ForegroundColor White
        Write-Host '   1) Kur / onar'
        Write-Host '   2) Baslat'
        Write-Host '   3) Durdur'
        Write-Host '   4) Durum'
        Write-Host '   5) Paneli tarayicida ac'
        Write-Host '   6) Kurulum kodunu goster'
        Write-Host '   7) Guncelle'
        Write-Host '   8) Otomatik baslat + bekci (ac)'
        Write-Host '   9) Otomatik baslamayi kapat'
        Write-Host '   u) Uzaktan baglanti adresini goster'
        Write-Host '   0) Cikis'
        $choice = Read-Host '  Secim'
        switch ($choice) {
            '1' { Invoke-Self 'install' }
            '2' { Invoke-Self 'start' }
            '3' { Invoke-Self 'stop' }
            '4' { Invoke-Self 'status' }
            '5' { Start-Process $PanelUrl }
            '6' { Invoke-Self 'setup-code' }
            '7' { Invoke-Self 'update' }
            '8' { Invoke-Self 'autostart-on' }
            '9' { Invoke-Self 'autostart-off' }
            'u' { Invoke-Self 'remote-link' }
            '0' { return }
            default { Write-Warn 'Gecersiz secim.' }
        }
    }
}

# The menu must survive a failed action; the helpers exit on error, so each
# menu action runs in a child PowerShell of this same script.
function Invoke-Self {
    param([string]$Name)
    & (Get-Command powershell.exe).Source -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath $Name -Distro $Distro -Profile $Profile
}

switch ($Command) {
    'menu'          { Show-Menu }
    'install'       { Invoke-Install }
    'start'         { Invoke-Start }
    'stop'          { Invoke-Stop }
    'restart'       { Invoke-Stop; Invoke-Start }
    'status'        { Invoke-Status }
    'open'          { Start-Process $PanelUrl }
    'setup-code'    { Assert-Installed; Start-KeepAlive; Show-SetupCode }
    'logs'          { Assert-Installed; Start-KeepAlive; Invoke-Linux @('journalctl', '-u', 'serverkit', '-n', '100', '-f') | Out-Null }
    'shell'         { Assert-Installed; Start-KeepAlive; & wsl.exe -d $Distro -u root --cd '~' }
    'update'        { Assert-Installed; Start-KeepAlive; $code = Invoke-Linux @('/usr/local/bin/serverkit', 'update'); Invoke-Setup @('panel-ipv6') | Out-Null; Invoke-Setup @('ai-extras') | Out-Null; exit $code }
    'cli'           { Assert-Installed; Start-KeepAlive; exit (Invoke-Linux (@('/usr/local/bin/serverkit') + @($Rest | Where-Object { $_ }))) }
    'theme'         { Invoke-Theme }
    'ensure'        { Invoke-Ensure }
    'remote-on'     { Invoke-Remote 'on' }
    'remote-off'    { Invoke-Remote 'off' }
    'remote-link'   { Invoke-Remote 'url' }
    'gateway-on'    { Invoke-Gateway 'on' }
    'gateway-off'   { Invoke-Gateway 'off' }
    'gateway-prompt' { Invoke-Gateway 'prompt' }
    'gateway-rotate' { Invoke-Gateway 'rotate' }
    'sync-on'       { Set-SiteSync $true }
    'sync-off'      { Set-SiteSync $false }
    'sync-now'      { $c = Get-SwkConfig -Required; $py = if ($c.windows.python) { [string]$c.windows.python } else { 'python.exe' }; & $py (Join-Path $PSScriptRoot 'sites\sync.py') --now }
    'autostart-on'  { Set-Autostart $true }
    'autostart-off' { Set-Autostart $false }
    'uninstall'     { Invoke-Uninstall }
    'help'          { Show-Help }
}
