param(
  [Parameter(Mandatory = $true)][string]$InputPath,
  [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutput)

if ([System.IO.Path]::GetExtension($resolvedInput).ToLowerInvariant() -ne '.doc') {
  throw 'Input must be a legacy .doc file.'
}
if ([System.IO.Path]::GetExtension($resolvedOutput).ToLowerInvariant() -ne '.docx') {
  throw 'Output must be a .docx file.'
}
if (-not [System.IO.Directory]::Exists($outputDirectory)) {
  throw 'Output directory does not exist.'
}

$word = $null
$document = $null
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $word.Options.UpdateLinksAtOpen = $false
  $word.Options.SaveNormalPrompt = $false
  $document = $word.Documents.Open($resolvedInput, $false, $true, $false)
  $document.SaveAs2($resolvedOutput, 16)
} finally {
  if ($document -ne $null) {
    $document.Close($false)
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document)
  }
  if ($word -ne $null) {
    $word.Quit()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
  }
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
}

if (-not (Test-Path -LiteralPath $resolvedOutput)) {
  throw 'Converted DOCX was not created.'
}
