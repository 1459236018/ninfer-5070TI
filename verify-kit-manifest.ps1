# verify-kit-manifest.ps1 -- check a kit's SHA256SUMS.txt against its own contents, BYTE-EXACT.
#
# WHY THIS EXISTS (measured 2026-10-01):
#   1) SHA256SUMS.txt in these kits is UTF-8 WITHOUT a BOM and carries NON-ASCII file names.
#   2) Windows PowerShell 5.1 -- what the kit's own notes assume, and what runs by default on a
#      cp936 machine -- reads a BOM-less file as ANSI, so those non-ASCII names arrive mangled.
#      A verifier that then reports "MISSING" is reporting its own encoding bug as package
#      corruption: a FALSE RED, and a direct violation of the "a name that does not exist makes
#      every check pass silently" rule in the pitfall table.
#   3) A manifest can also go STALE: an older kit's three engines were overwritten while its
#      SHA256SUMS.txt still carried the earlier date. This script reports the manifest's own mtime
#      against the newest file it covers, so a stale manifest is visible.
#
# 2026-10-21 changes (this repository):
#   - the manifest may live in the kit root OR in docs\ -- both are found
#   - manifest entries may be relative to the kit root OR to the repo root; both bases are tried,
#     so an entry like engine/x.dll resolves whether the manifest sits at the root or under docs\
#   - '#' lines are comments, not UNPARSED data
#
# Reads manifests as UTF-8 ALWAYS, and verifies by byte content, never by name resolution alone.
param(
  [Parameter(Mandatory = $true)][string]$Kit,
  [switch]$ShowAll
)
$ErrorActionPreference = 'Stop'
$Kit = (Resolve-Path -LiteralPath $Kit).Path

$sums = Join-Path $Kit 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $sums)) {
  $inDocs = Join-Path $Kit (Join-Path 'docs' 'SHA256SUMS.txt')
  if (Test-Path -LiteralPath $inDocs) { $sums = $inDocs }
}
if (-not (Test-Path -LiteralPath $sums)) {
  "REFUSE: no SHA256SUMS.txt in $Kit (nor in $Kit\docs)"
  exit 3
}

# Search bases: the kit itself, plus the directory one level above the manifest.
$bases = @($Kit, (Split-Path -Parent (Split-Path -Parent $sums))) |
         Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$lines = [IO.File]::ReadAllLines($sums, [Text.UTF8Encoding]::new($false))
"kit      : $Kit"
"manifest : $sums"
"manifest mtime : $((Get-Item -LiteralPath $sums).LastWriteTime)"
"lines    : $($lines.Count)"
"search   : $($bases -join ' ; ')"
""

$ok = 0; $mismatch = 0; $missing = 0; $unparsed = 0
$newest = $null; $newestName = ''
foreach ($line in $lines) {
  if ($line.Trim() -eq '') { continue }
  if ($line -match '^\s*#') { continue }
  if ($line -notmatch '^\s*([0-9A-Fa-f]{64})\s{2}(.+?)\s*$') {
    $unparsed++
    if ($ShowAll) { "UNPARSED  $line" }
    continue
  }
  $want = $Matches[1].ToUpperInvariant()
  $rel  = $Matches[2]

  $path = $null
  foreach ($b in $bases) {
    $cand = Join-Path $b $rel
    if (Test-Path -LiteralPath $cand) { $path = $cand; break }
  }
  if ($null -eq $path) {
    # A name that cannot be resolved is reported with its code points, so an encoding problem is
    # never mistaken for a missing file.
    $missing++
    $codes = (($rel.ToCharArray() | ForEach-Object { 'U+{0:X4}' -f [int]$_ }) -join ' ')
    "MISSING   $rel"
    "          codepoints: $codes"
    "          searched: $($bases -join ' ; ')"
    continue
  }

  $got = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
  if ($got -eq $want) {
    $ok++
    if ($ShowAll) { "ok        $rel" }
  } else {
    $mismatch++
    "MISMATCH  $rel"
    "          want=$want"
    "          got =$got"
  }
  $item = Get-Item -LiteralPath $path
  if ($null -eq $newest -or $item.LastWriteTime -gt $newest) { $newest = $item.LastWriteTime; $newestName = $rel }
}

""
"ok=$ok  mismatch=$mismatch  missing=$missing  unparsed=$unparsed"
"newest covered file: $newestName  ($newest)"
if ($null -ne $newest -and $newest -gt (Get-Item -LiteralPath $sums).LastWriteTime) {
  "STALE: a covered file is NEWER than the manifest -- the manifest does not describe this kit."
}
if ($mismatch -gt 0 -or $missing -gt 0 -or $unparsed -gt 0) { exit 1 }
exit 0
