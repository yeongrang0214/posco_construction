param(
  [Parameter(Mandatory = $true)][string]$InputPath,
  [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$sourceFile = (Resolve-Path -LiteralPath $InputPath).Path
$pdfFile = [System.IO.Path]::GetFullPath($OutputPath)
if ([System.IO.Path]::GetExtension($sourceFile).ToLowerInvariant() -notin @('.doc', '.docx')) { throw 'Word source required.' }
if ([System.IO.Path]::GetExtension($pdfFile).ToLowerInvariant() -ne '.pdf') { throw 'PDF output required.' }
$word = $null
$document = $null
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $word.Options.UpdateLinksAtOpen = $false
  $word.Options.SaveNormalPrompt = $false
  $document = $word.Documents.Open($sourceFile, $false, $true, $false)
  $document.ExportAsFixedFormat($pdfFile, 17)
} finally {
  if ($null -ne $document) { $document.Close($false); [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document) }
  if ($null -ne $word) { $word.Quit(); [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) }
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
}
