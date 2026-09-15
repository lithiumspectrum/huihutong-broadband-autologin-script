<#
.SYNOPSIS
    Dorm Network SSO Login Flow Capture Script (v2)
.DESCRIPTION
    Captures the full broadband.215123.cn SSO login flow.
    Run this when NOT authenticated (first connect or after forgetting WiFi).
    This script captures the SSO pages and API endpoints.
.NOTES
    Usage:
      1. Forget dorm WiFi, reconnect (ensure NOT authenticated)
      2. Run: powershell -ExecutionPolicy Bypass -File capture_sso.ps1
      3. After capture, authenticate via browser (WeChat scan)
      4. Run again: powershell -ExecutionPolicy Bypass -File capture_sso.ps1
         (to capture the authenticated state)
#>

param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Continue"

if (-not $OutputDir) {
    $OutputDir = ".\sso_capture_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path "$OutputDir\capture_log.txt" -Value $line
}

function Save-Output {
    param([string]$Name, [string]$Content)
    $file = "$OutputDir\$Name"
    $Content | Out-File -FilePath $file -Encoding UTF8
    Write-Log "Saved: $Name"
}

Write-Log "========================================"
Write-Log "  SSO Login Flow Capture v2"
Write-Log "  Output: $OutputDir"
Write-Log "========================================"
Write-Host ""

# ==============================================================================
# 1. Check network status
# ==============================================================================
Write-Log "Step 1: Check network status"

$portalCheck = & curl.exe -s -o /dev/null -w "%{http_code}" -I "http://connect.rom.miui.com/generate_204" 2>$null
Write-Log "  Portal check HTTP status: $portalCheck"

if ($portalCheck -eq "204") {
    Write-Log "  Already ONLINE (204). To capture login flow, you need to:" "WARN"
    Write-Log "  1. Forget the WiFi network" "WARN"
    Write-Log "  2. Reconnect" "WARN"
    Write-Log "  3. Run this script BEFORE authenticating" "WARN"
    Write-Host ""
    Write-Host "  Press Y to continue anyway, or any other key to exit:" -ForegroundColor Yellow
    $key = Read-Host
    if ($key -notmatch "^[Yy]") {
        Write-Log "User chose to exit."
        exit 0
    }
} else {
    Write-Log "  NOT online (status: $portalCheck). Good - can capture login flow."
}

Write-Host ""

# ==============================================================================
# 2. Capture portal redirect
# ==============================================================================
Write-Log "Step 2: Capture portal redirect"

$portalUrls = @(
    "http://connect.rom.miui.com/generate_204",
    "http://www.baidu.com",
    "http://neverssl.com"
)

foreach ($url in $portalUrls) {
    Write-Log "  Trying: $url"
    $resp = & curl.exe -s -D - --max-time 10 $url 2>&1
    $respText = $resp -join "`n"
    Save-Output "02_portal_$(($url -replace 'https?://','' -replace '[^\w]','_')).txt" $respText

    # Extract redirect URL
    $locMatch = [regex]::Match($respText, "(?im)^location:\s*(.+)$")
    if ($locMatch.Success) {
        $redirUrl = $locMatch.Groups[1].Value.Trim()
        Write-Log "  Got redirect: $redirUrl"

        # Follow this redirect
        Write-Log "  Following redirect..."
        $redirResp = & curl.exe -s -D - --max-time 10 $redirUrl 2>&1
        $redirRespText = $redirResp -join "`n"
        Save-Output "02b_portal_redirect_target.txt" $redirRespText
        break
    }
}

Write-Host ""

# ==============================================================================
# 3. Capture broadband SSO pages
# ==============================================================================
Write-Log "Step 3: Capture broadband.215123.cn SSO pages"

$ssoUrls = @(
    "https://broadband.215123.cn/sso/login.html",
    "https://broadband.215123.cn/sso/broadband.html"
)

foreach ($url in $ssoUrls) {
    Write-Log "  Fetching: $url"
    $resp = & curl.exe -s -D - --max-time 15 -k $url 2>&1
    $respText = $resp -join "`n"
    $fileName = $url -replace 'https?://','' -replace '[^\w]','_' -replace '_+','_'
    Save-Output "03_$fileName.txt" $respText
}

Write-Host ""

# ==============================================================================
# 4. Capture portal page at 10.10.16.101
# ==============================================================================
Write-Log "Step 4: Capture portal page at 10.10.16.101:8080"

$portalUrl = "http://10.10.16.101:8080/eportal/portal/index.jsp"
Write-Log "  Fetching: $portalUrl"
$portalResp = & curl.exe -s -D - --max-time 15 $portalUrl 2>&1
Save-Output "04_portal_index.txt" ($portalResp -join "`n")

# Also try the root
$portalRoot = "http://10.10.16.101:8080/"
Write-Log "  Fetching: $portalRoot"
$portalRootResp = & curl.exe -s -D - --max-time 15 $portalRoot 2>&1
Save-Output "04b_portal_root.txt" ($portalRootResp -join "`n")

Write-Host ""

# ==============================================================================
# 5. Test API endpoints (unauthenticated)
# ==============================================================================
Write-Log "Step 5: Test API endpoints (unauthenticated state)"

$apiTests = @(
    "https://api.215123.cn/web-app/auth/certificateLogin",
    "https://api.215123.cn/ac/auth/oauthRedirect",
    "https://api.215123.cn/web-app/auth/certificateLogin?openId=test",
    "https://api.215123.cn/ac/auth/oauthRedirect?serviceName=chinaTelecom"
)

$i = 0
foreach ($url in $apiTests) {
    $i++
    Write-Log "  Test $i : $url"
    $resp = & curl.exe -s -D - --max-time 15 $url 2>&1
    Save-Output "05_api_test_$i.txt" ($resp -join "`n")
}

Write-Host ""

# ==============================================================================
# 6. Capture full redirect chain from portal
# ==============================================================================
Write-Log "Step 6: Follow full redirect chain from portal"

# Start from the portal check URL and follow all redirects
$startUrl = "http://connect.rom.miui.com/generate_204"
Write-Log "  Starting from: $startUrl"

$chainLog = @()
$currentUrl = $startUrl
$hop = 0
$maxHops = 15

while ($currentUrl -and $hop -lt $maxHops) {
    $hop++
    Write-Log "  Hop $hop : $currentUrl"

    $resp = & curl.exe -s -D - --max-time 10 -k $currentUrl 2>&1
    $respText = $resp -join "`n"

    $chainLog += "=== Hop $hop ==="
    $chainLog += "URL: $currentUrl"
    $chainLog += $respText
    $chainLog += ""

    # Extract next redirect
    $nextUrl = ""
    $locMatch = [regex]::Match($respText, "(?im)^location:\s*(.+)$")
    if ($locMatch.Success) {
        $nextUrl = $locMatch.Groups[1].Value.Trim()
    } else {
        $hrefMatch = [regex]::Match($respText, "location\.href=['""]([^'""]+)['""]")
        if ($hrefMatch.Success) {
            $nextUrl = $hrefMatch.Groups[1].Value
        } else {
            $metaMatch = [regex]::Match($respText, "(?i)<meta[^>]+url=([^""'>]+)")
            if ($metaMatch.Success) {
                $nextUrl = $metaMatch.Groups[1].Value
            }
        }
    }

    if ($nextUrl -and $nextUrl -ne $currentUrl) {
        # Handle relative URLs
        if ($nextUrl -match "^/") {
            $uri = [System.Uri]$currentUrl
            $nextUrl = "$($uri.Scheme)://$($uri.Host)$nextUrl"
        }
        $currentUrl = $nextUrl
    } else {
        $chainLog += "=== END (no more redirects) ==="
        break
    }
}

Save-Output "06_full_redirect_chain.txt" ($chainLog -join "`n")

Write-Host ""

# ==============================================================================
# 7. Summary and instructions
# ==============================================================================
Write-Log "========================================"
Write-Log "  Capture complete!"
Write-Log "========================================"
Write-Log ""
Write-Log "Files in $OutputDir :"
$files = Get-ChildItem $OutputDir -File | Sort-Object Name
foreach ($f in $files) {
    $size = "{0:N1}" -f ($f.Length / 1KB)
    Write-Log "  $($f.Name)  (${size} KB)"
}
Write-Log ""
Write-Log "IMPORTANT - Next steps for capturing OpenID:"
Write-Log ""
Write-Log "Method A - Browser F12 (RECOMMENDED):"
Write-Log "  1. Open Chrome/Edge, press F12"
Write-Log "  2. Go to Network tab, check 'Preserve log'"
Write-Log "  3. Filter by 'api.215123.cn'"
Write-Log "  4. Complete login via browser (WeChat scan)"
Write-Log "  5. Find 'certificateLogin' request"
Write-Log "  6. Copy openId from URL params"
Write-Log "  7. Right-click requests -> Save all as HAR"
Write-Log "  8. Send HAR file + openId to AI"
Write-Log ""
Write-Log "Method B - After getting OpenID, re-run original script:"
Write-Log "  powershell -ExecutionPolicy Bypass -File capture.ps1 -OpenId 'your_openId'"
Write-Log ""

Write-Host ""
Write-Host "Done! Output: $OutputDir" -ForegroundColor Green
Write-Host ""
Write-Host "To find OpenID, use browser F12 method (see capture_log.txt)" -ForegroundColor Yellow
Write-Host "After getting HAR file, run: .\parse_har.ps1 -HarFile 'file.har'" -ForegroundColor Yellow
