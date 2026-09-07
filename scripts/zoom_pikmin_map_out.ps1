param(
    [Parameter(Mandatory = $true)]
    [string]$ScrcpyPath,

    [Parameter(Mandatory = $true)]
    [string]$Serial,

    [ValidateRange(1, 8)]
    [int]$Repetitions = 4
)

$ErrorActionPreference = "Stop"

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class BloomPilotMouse {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll")]
    public static extern bool GetClientRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll")]
    public static extern bool ClientToScreen(IntPtr hWnd, ref POINT point);

    [DllImport("user32.dll")]
    public static extern bool GetCursorPos(out POINT point);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);
}
"@

if (-not (Test-Path -LiteralPath $ScrcpyPath -PathType Leaf)) {
    throw "scrcpy was not found: $ScrcpyPath"
}

$arguments = @(
    "--serial", $Serial,
    "--window-title", "BloomPilot-map-zoom-helper",
    "--window-width", "540",
    "--window-height", "1170",
    "--window-x", "20",
    "--window-y", "20",
    "--no-audio",
    "--no-clipboard-autosync"
)

$scrcpy = $null
$originalCursor = New-Object BloomPilotMouse+POINT
[BloomPilotMouse]::GetCursorPos([ref]$originalCursor) | Out-Null

try {
    $scrcpy = Start-Process -FilePath $ScrcpyPath -ArgumentList $arguments -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $handle = [IntPtr]::Zero
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
        $scrcpy.Refresh()
        if ($scrcpy.HasExited) {
            throw "scrcpy exited before its control window was ready."
        }
        $handle = $scrcpy.MainWindowHandle
        if ($handle -ne [IntPtr]::Zero) {
            break
        }
    }
    if ($handle -eq [IntPtr]::Zero) {
        throw "Timed out waiting for the scrcpy control window."
    }

    $rect = New-Object BloomPilotMouse+RECT
    if (-not [BloomPilotMouse]::GetClientRect($handle, [ref]$rect)) {
        throw "Could not read the scrcpy client dimensions."
    }
    $origin = New-Object BloomPilotMouse+POINT
    if (-not [BloomPilotMouse]::ClientToScreen($handle, [ref]$origin)) {
        throw "Could not read the scrcpy client position."
    }

    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 100 -or $height -lt 100) {
        throw "Unexpected scrcpy client dimensions: ${width}x${height}"
    }

    [BloomPilotMouse]::SetForegroundWindow($handle) | Out-Null
    Start-Sleep -Milliseconds 350

    $x = $origin.X + [Math]::Round($width * 0.50)
    $startY = $origin.Y + [Math]::Round($height * 0.15)
    $endY = $origin.Y + [Math]::Round($height * 0.47)
    $steps = 28

    for ($repeat = 0; $repeat -lt $Repetitions; $repeat++) {
        [BloomPilotMouse]::SetCursorPos($x, $startY) | Out-Null
        [BloomPilotMouse]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
        [BloomPilotMouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        for ($step = 1; $step -le $steps; $step++) {
            $y = $startY + [Math]::Round(($endY - $startY) * $step / $steps)
            [BloomPilotMouse]::SetCursorPos($x, $y) | Out-Null
            Start-Sleep -Milliseconds 12
        }
        [BloomPilotMouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        [BloomPilotMouse]::keybd_event(0x11, 0, 0x0002, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 450
    }

    Write-Output "PINCH_DONE repetitions=$Repetitions client=${width}x${height}"
}
finally {
    [BloomPilotMouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    [BloomPilotMouse]::keybd_event(0x11, 0, 0x0002, [UIntPtr]::Zero)
    [BloomPilotMouse]::SetCursorPos($originalCursor.X, $originalCursor.Y) | Out-Null
    if ($null -ne $scrcpy -and -not $scrcpy.HasExited) {
        Stop-Process -Id $scrcpy.Id -Force
        $scrcpy.WaitForExit(5000) | Out-Null
    }
}
