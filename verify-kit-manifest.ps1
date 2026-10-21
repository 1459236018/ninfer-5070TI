# verify-kit-manifest.ps1 -- check a kit's SHA256SUMS.txt against its own contents, BYTE-EXACT.
#
# WHAT IT CHECKS
#   Primary: every entry in SHA256SUMS.txt must match its file byte for byte. That is the only
#   check that means "these are the shipped bytes", and it is what an unzipped release must pass.
#
#   Secondary: docs/SHA256SUMS.lf.txt lists the TEXT files with digests taken after rewriting
#   every CRLF pair to a single LF. It exists because .gitattributes marks text files `text=auto`:
#   git stores them with LF, a Windows checkout and the release zip can carry CRLF, and one file
#   then legitimately has two digests. An entry that fails the primary check is looked up here and,
#   on a hit, reported as CRLF-ONLY: content identical, only line endings differed. It does not
#   fail the run.
#
#   The companion file is text-only by construction -- binaries are never normalised, because a
#   .dll packed with the byte pair 0x0D 0x0A uses it as machine code, not as a line break. If a
#   binary is missing from the companion file it can only ever pass the primary check.
#
# ENCODING
#   SHA256SUMS.txt is UTF-8 WITHOUT a BOM and carries non-ASCII file names. Windows PowerShell 5.1
#   reads a BOM-less file as ANSI, which mangles those names -- and a verifier that then reports
#   "MISSING" reports its own encoding bug as package corruption. Both manifests are therefore
#   read with an explicit UTF-8 decoder. A name that cannot be resolved is printed with its code
#   points so an encoding problem is never mistaken for a missing file.
#
# STALE MANIFESTS
#   An older kit had three engines overwritten while its manifest still carried the earlier date.
#   This script compares the manifest's mtime with the newest covered file and warns when a covered
#   file is newer, so a stale manifest is visible instead of silently green.
#
# RETURNS
#   0  every entry matched (byte-exact, or CRLF-only for a listed text file)
#   1  at least one real mismatch, missing file, or unparsable line
#   3  no manifest found
param(
  [Parameter(Mandatory = $true)][string]$Kit,
  [switch]$ShowAll
)
$ErrorActionPreference = 'Stop'
$Kit = (Resolve-Path -LiteralPath $Kit).Path

function Find-Manifest {
  param([string]$Root, [string]$Name)
  $direct = Join-Path $Root $Name
  if (Test-Path -LiteralPath $direct) { return $direct }
  $inDocs = Join-Path $Root (Join-Path 'docs' $Name)
  if (Test-Path -LiteralPath $inDocs) { return $inDocs }
  return $null
}

$sums = Find-Manifest $Kit 'SHA256SUMS.txt'
if ($null -eq $sums) {
  "REFUSE: no SHA256SUMS.txt in $Kit (nor in $Kit\docs)"
  exit 3
}
$sumsLf = Find-Manifest $Kit 'SHA256SUMS.lf.txt'

# Search bases: the kit itself, plus the directory one level above the manifest.
$bases = @($Kit, (Split-Path -Parent (Split-Path -Parent $sums))) |
         Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

# Read a manifest as UTF-8 regardless of the console code page.
function Read-ManifestLines {
  param([string]$Path)
  return [IO.File]::ReadAllLines($Path, (New-Object Text.UTF8Encoding($false)))
}

# Parse `<64 hex>  <path>` lines, ignoring blanks and '#' comments.
function ConvertFrom-ManifestLines {
  param([string[]]$Lines)
  $map = @{}
  $bad = @()
  foreach ($line in $Lines) {
    if ($null -eq $line) { continue }
    $s = $line.Trim()
    if ($s -eq '' -or $s.StartsWith('#')) { continue }
    if ($s -notmatch '^([0-9A-Fa-f]{64})\s+(.+?)\s*$') { $bad += $s; continue }
    $map[$Matches[2]] = $Matches[1].ToUpperInvariant()
  }
  return @{ Map = $map; Bad = $bad }
}

function Resolve-KitPath {
  param([string]$Rel, [string[]]$Bases)
  foreach ($b in $Bases) {
    $cand = Join-Path $b $Rel
    if (Test-Path -LiteralPath $cand) { return (Resolve-Path -LiteralPath $cand).Path }
  }
  return $null
}

function Get-Sha256Path {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

"kit      : $Kit"
"manifest : $sums"
"companion: $(if ($null -eq $sumsLf) { '<none -- CRLF fallback unavailable>' } else { $sumsLf })"
"manifest mtime : $((Get-Item -LiteralPath $sums).LastWriteTime)"
"search   : $($bases -join ' ; ')"
""

$primary = ConvertFrom-ManifestLines (Read-ManifestLines $sums)
$lfMap = @{}
if ($null -ne $sumsLf) {
  $lfMap = (ConvertFrom-ManifestLines (Read-ManifestLines $sumsLf)).Map
}

$ok = 0; $mismatch = 0; $missing = 0; $unparsed = 0; $crlfOnly = 0
$newest = $null; $newestName = ''

"--- primary check (byte-exact, SHA256SUMS.txt) ---"
foreach ($rel in ($primary.Map.Keys | Sort-Object)) {
  $want = $primary.Map[$rel]
  $path = Resolve-KitPath $rel $bases
  if ($null -eq $path) {
    $missing++
    $codes = (($rel.ToCharArray() | ForEach-Object { 'U+{0:X4}' -f [int]$_ }) -join ' ')
    "MISSING   $rel"
    "          codepoints: $codes"
    continue
  }
  $got = Get-Sha256Path $path
  if ($got -eq $want) {
    $ok++
    if ($ShowAll) { "ok        $rel" }
  } else {
    $lfWant = $lfMap[$rel]
    if ($null -ne $lfWant -and $lfWant -eq $got) {
      # The file already carries LF; the on-disk digest IS the normalised digest.
      $crlfOnly++
      "CRLF-ONLY $rel"
      "          file is LF-only; matches the normalised digest"
      "          got=$got"
    } elseif ($null -ne $lfWant) {
      $mismatch++
      "MISMATCH  $rel"
      "          byte-exact want=$want"
      "          byte-exact got =$got"
      "          normalised want=$lfWant"
      "          (the file matches NEITHER; line endings are not the difference)"
    } else {
      $mismatch++
      "MISMATCH  $rel"
      "          want=$want"
      "          got =$got"
      if ($rel -match '\.(dll|exe|ico|png|jpg|zip)$') {
        "          binary: line-ending normalisation never applies"
      } else {
        "          (not in the text companion manifest, so no CRLF fallback exists)"
      }
    }
  }
  $item = Get-Item -LiteralPath $path
  if ($null -eq $newest -or $item.LastWriteTime -gt $newest) { $newest = $item.LastWriteTime; $newestName = $rel }
}

$unparsed = $primary.Bad.Count
foreach ($b in $primary.Bad) { "UNPARSED  $b" }

""
"ok=$ok  mismatch=$mismatch  missing=$missing  unparsed=$unparsed  crlf_only=$crlfOnly"
if ($crlfOnly -gt 0) {
  "NOTE: $crlfOnly text file(s) differ from SHA256SUMS.txt ONLY in line endings."
  "      Content is identical. Expected for a git checkout: .gitattributes normalises to LF."
}
"newest covered file: $newestName  ($newest)"

# STALE uses a tolerance. Not every unzip tool preserves per-entry timestamps -- PowerShell's
# ZipFile.ExtractAll rewrites them all to "now" -- so a fresh, correct extraction can leave a
# covered file a few milliseconds NEWER than the manifest. The drift this check exists to catch
# (an engine replaced while the manifest kept its old date) is on the scale of days, so anything
# under the tolerance is noise, not a stale manifest.
$staleTolerance = [TimeSpan]::FromSeconds(60)
if ($null -ne $newest) {
  $drift = $newest - (Get-Item -LiteralPath $sums).LastWriteTime
  if ($drift -gt $staleTolerance) {
    "STALE: the newest covered file is $([int]$drift.TotalSeconds) s newer than the manifest."
    "       The manifest probably does not describe this kit."
  }
}
if ($mismatch -gt 0 -or $missing -gt 0 -or $unparsed -gt 0) { exit 1 }
exit 0
