# Windows build: vendor SDR tools -> PyInstaller folder -> portable zip -> MSIX.
# Run from the repository root in a Python environment with the app installed:
#   pip install ".[gui,hline]" pyinstaller pillow
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# Needs the Windows 10 SDK for makeappx/signtool (present on GitHub's windows runners).
# The MSIX is unsigned; the Store signs it at submission. For a local install test:
#   signtool sign /fd SHA256 /a /f test.pfx /p <pw> dist\aquila-radio-astronomy.msix  (self-signed cert)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root
$version = (Select-String -Path aquila\__init__.py -Pattern '__version__ = "(.+?)"').Matches[0].Groups[1].Value

# 1. vendor tools next to the exe (aquila.sdr.which looks in tools\ first)
New-Item -ItemType Directory -Force tools, build\dl | Out-Null
$dl = @{
  airspy = "https://github.com/airspy/airspyone_host/releases/download/v1.0.10/airspy_host_tools_win32_x86_x64_v1_0_10.zip"
  rtlsdr = "https://github.com/rtlsdrblog/rtl-sdr-blog/releases/download/V1.4.0/Release.zip"
}
foreach ($k in $dl.Keys) {
  $zip = "build\dl\$k.zip"
  if (-not (Test-Path $zip)) { Invoke-WebRequest $dl[$k] -OutFile $zip }
  Expand-Archive $zip -DestinationPath "build\dl\$k" -Force
}
# x64 binaries + their DLLs; airspy_rx also drives the HydraSDR (same USB protocol)
# the zips hold x86 and x64 side by side (and the airspy zip's root folder is named
# "x86_x64"), so match the x64 directory itself, not the whole path
Get-ChildItem build\dl\airspy -Recurse -Include airspy_rx.exe, airspy_info.exe, *.dll | Where-Object { $_.DirectoryName -match "[\\/]x64$" } | Copy-Item -Destination tools -Force
Get-ChildItem build\dl\rtlsdr -Recurse -Include rtl_sdr.exe, rtl_test.exe, rtl_biast.exe, *.dll | Where-Object { $_.DirectoryName -match "[\\/]x64$" } | Copy-Item -Destination tools -Force
# HackRF publishes no Windows binaries; CI installs the MSYS2 ucrt64 package and
# points HACKRF_BIN at its bin folder (hackrf_transfer + libhackrf + winpthread)
if ($env:HACKRF_BIN -and (Test-Path "$env:HACKRF_BIN\hackrf_transfer.exe")) {
  foreach ($f in "hackrf_transfer.exe", "hackrf_info.exe", "libhackrf.dll", "libwinpthread-1.dll") { Copy-Item "$env:HACKRF_BIN\$f" tools -Force }
} else { Write-Host "HACKRF_BIN not set: HackRF tools left out" }
Get-ChildItem tools | Select-Object -ExpandProperty Name | Write-Host

# 2. icons and the frozen app
python packaging\make_icons.py
pyinstaller --noconfirm --clean packaging\pyinstaller\aquila.spec
Compress-Archive -Path dist\aquila-radio-astronomy -DestinationPath "dist\aquila-radio-astronomy-$version-windows-x64.zip" -Force

# 3. MSIX
$pkg = "build\msix"
Remove-Item -Recurse -Force $pkg -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$pkg\Assets" | Out-Null
Copy-Item -Recurse dist\aquila-radio-astronomy "$pkg\aquila-radio-astronomy"
# written as UTF-8 without a byte order mark: makeappx rejects anything else
$manifest = (Get-Content -Raw packaging\windows\AppxManifest.xml) -creplace "@VERSION@", $version
[IO.File]::WriteAllText((Resolve-Path $pkg).Path + "\AppxManifest.xml", $manifest, (New-Object Text.UTF8Encoding $false))
Copy-Item build\icons\aquila-150.png "$pkg\Assets\Square150x150Logo.png"
Copy-Item build\icons\aquila-44.png  "$pkg\Assets\Square44x44Logo.png"
Copy-Item build\icons\aquila-71.png  "$pkg\Assets\Square71x71Logo.png"
Copy-Item build\icons\aquila-50.png  "$pkg\Assets\StoreLogo.png"
python -c "from PIL import Image; im=Image.open('build/icons/aquila-128.png').convert('RGBA'); w=Image.new('RGBA',(310,150),(0,0,0,0)); w.paste(im,(91,11)); w.save('build/msix/Assets/Wide310x150Logo.png')"
$sdk = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\10.*\x64\makeappx.exe" | Sort-Object FullName | Select-Object -Last 1
& $sdk.FullName pack /d $pkg /p "dist\aquila-radio-astronomy-$version.msix" /o
if ($LASTEXITCODE -ne 0) { throw "makeappx failed" }
Write-Host "built dist\aquila-radio-astronomy-$version-windows-x64.zip and dist\aquila-radio-astronomy-$version.msix"
