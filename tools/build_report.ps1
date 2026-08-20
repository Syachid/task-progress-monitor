<#
Builds the standalone HTML report from CRM JSON pulled via MCP.

This exists because the machine has no Python runtime: it mirrors the bucket rules in
backend/buckets.py exactly, so the numbers here are the numbers the FastAPI app will
produce once it can run. If you change a rule in buckets.py, change it here too.

Reads every *list_records*.txt in the MCP tool-results directory and picks out:
  - Task        : record_type_id 100 (Indonesia) AND task_type "Task"
  - Opportunity : record_type_id 12  (Indonesia), open stages only

Plus two small lookup files written by hand from CRM queries (see README):
  _tagged_accounts.txt  accountId=Strategic|Hypercare;Strategic   (CSM column tags)
  _account_parents.txt  childAccountId=parentAccountId

Usage:  powershell -ExecutionPolicy Bypass -File tools\build_report.ps1
#>
[CmdletBinding()]
param(
  [string]$SourceDir,
  [string]$OutFile,
  [int]$StaleDays = 14
)

$ErrorActionPreference = 'Stop'
$JakartaOffset = [TimeSpan]::FromHours(7)

# $PSScriptRoot isn't populated while param() defaults are evaluated, so resolve here.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutFile) { $OutFile = Join-Path $root "..\Laporan-Task-Indonesia.html" }

# Stages that mean the deal is still live. Closed-Won / Closed-Lost are excluded.
$OPEN_STAGES = @('New','Proposal Submitted','Negotiation','EKYC Approval','Contract Sent',
                 'Agreed to Ship','Onboarding','Ready to Ship','Future Opportunity')

function Parse-CrmDate {
  param([object]$Value)
  if ($null -eq $Value) { return $null }
  $s = ([string]$Value).Trim()
  if ([string]::IsNullOrWhiteSpace($s)) { return $null }

  # Bare date ("2025-12-31") -> end of that day in Jakarta, so a task due today
  # isn't overdue this morning.
  if ($s.Length -eq 10 -and $s[4] -eq '-' -and $s[7] -eq '-') {
    try { $d = [datetime]::ParseExact($s, 'yyyy-MM-dd', $null) } catch { return $null }
    return [DateTimeOffset]::new($d.Date.AddHours(23).AddMinutes(59).AddSeconds(59), $JakartaOffset)
  }
  # No trailing Z / +hh:mm -> the CRM wrote a naive local timestamp; treat it as Jakarta.
  if ($s -notmatch '(Z|[+-]\d{2}:?\d{2})$') {
    try { $dt = [datetime]::Parse($s, [Globalization.CultureInfo]::InvariantCulture) } catch { return $null }
    return [DateTimeOffset]::new($dt, $JakartaOffset)
  }
  $dto = [DateTimeOffset]::MinValue
  if ([DateTimeOffset]::TryParse($s, [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$dto)) {
    return $dto.ToOffset($JakartaOffset)
  }
  return $null
}

function Redact-ContactInfo {
  <#
  Best-effort PII scrub for the published (Artifact) variant only — the local file keeps
  raw text. Emails match a standard pattern; phone numbers are a heuristic (9+ actual DIGITS,
  optionally spaced/dashed/plus-prefixed) since free-text CRM notes write them inconsistently
  ("081211689880", "+62 857-7022-4156", "6282121532535"). Counting digits (not match-string
  length) matters: an ISO date like "2026-06-24" is 10 characters but only 8 digits, and a
  naive length-based regex was redacting dates like proposal_submitted_date mentions as if
  they were phone numbers — caught during verification, fixed here by scoring each candidate
  match on its actual digit count via a MatchEvaluator instead of the raw string length.
  #>
  param([string]$Text)
  if ([string]::IsNullOrEmpty($Text)) { return $Text }
  $t = [regex]::Replace($Text, '[\w.+-]+@[\w-]+\.[\w.-]+', '[redacted-email]')
  $evaluator = [System.Text.RegularExpressions.MatchEvaluator]{
    param($m)
    $digits = ($m.Value.ToCharArray() | Where-Object { [char]::IsDigit($_) }).Count
    if ($digits -ge 9) { '[redacted-phone]' } else { $m.Value }
  }
  $t = [regex]::Replace($t, '(?<!\d)\+?\d[\d\-\s]{7,}\d(?!\d)', $evaluator)
  return $t
}

function Flatten-Value {
  <# service_level and friends arrive as either a string or a string[]. #>
  param([object]$v)
  if ($null -eq $v) { return $null }
  if ($v -is [array]) { return (($v | Where-Object { $_ }) -join ' · ') }
  return [string]$v
}

# ------------------------------------------------------------------ load + dedupe

if (-not $SourceDir) {
  $SourceDir = Join-Path $env:USERPROFILE ".claude\projects\C--Users-USer-Desktop-SO-Data-Sales-Ops-Claude-EKYC-Project\94106d04-0d12-4e18-9083-424dd96257a4\tool-results"
}

$tasksById = @{}
$oppsById  = @{}
foreach ($f in Get-ChildItem -Path $SourceDir -Filter "*list_records*.txt" -ErrorAction Stop) {
  # -Encoding UTF8 is mandatory: PS 5.1 falls back to the ANSI codepage for BOM-less
  # files, which mangles the en-dashes and accented text inside task descriptions.
  try { $j = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
  if (-not $j.items) { continue }
  switch ($j.object_type) {
    'Task' {
      foreach ($it in $j.items) {
        if ($it.record_type_id -ne 100 -or $it.task_type -ne 'Task') { continue }
        if ($it.PSObject.Properties.Name -contains 'sf_id' -and $it.sf_id) { continue }
        $tasksById[[string]$it.id] = $it
      }
    }
    'Opportunity' {
      foreach ($it in $j.items) {
        if ($it.record_type_id -ne 12) { continue }
        $oppsById[[string]$it.id] = $it
      }
    }
  }
}
$tasks = $tasksById.Values
Write-Host "Loaded $($tasks.Count) native Indonesia tasks, $($oppsById.Count) Indonesia opportunities."

# ---------------------------------------------------------------- account tagging
# STRATEGIC / HYPERCARE come from the Account's Customer Success Manager column, which
# is set on the PARENT account (children carry a raw Salesforce user id instead). So an
# opportunity is tagged when its own account is tagged, or when its account's parent is.
$tagged = @{}
$parents = @{}
$tagFile = Join-Path $SourceDir "_tagged_accounts.txt"
$parFile = Join-Path $SourceDir "_account_parents.txt"
if (Test-Path $tagFile) {
  Get-Content $tagFile -Encoding UTF8 | ForEach-Object {
    $p = $_ -split '='; if ($p.Count -eq 2) { $tagged[$p[0].Trim()] = $p[1].Trim() } }
}
if (Test-Path $parFile) {
  Get-Content $parFile -Encoding UTF8 | ForEach-Object {
    $p = $_ -split '='; if ($p.Count -eq 2) { $parents[$p[0].Trim()] = $p[1].Trim() } }
}

function Get-AccountTags {
  param([string]$AccountId)
  if (-not $AccountId) { return @() }
  $src = $null
  if ($tagged.ContainsKey($AccountId)) { $src = $tagged[$AccountId] }
  elseif ($parents.ContainsKey($AccountId) -and $tagged.ContainsKey($parents[$AccountId])) {
    $src = $tagged[$parents[$AccountId]]
  }
  if (-not $src) { return @() }
  $out = @()
  if ($src -match 'Hypercare') { $out += 'HYPERCARE' }
  if ($src -match 'Strategic') { $out += 'STRATEGIC' }
  return $out
}

# Manager roster — copied into the project from a Google Sheet the user exports by hand
# ("ID-User Master", tab "List"); ASM (column G in that tab) is each salesperson's direct
# manager. Matched against a task's Owner display name, since that's the identity actually
# shown in the report — 23 of 28 owners in this data set are covered; the rest (people not
# present in the roster at all, e.g. Corp Sales staff outside this particular tab) fall back
# to "Tanpa Manager" rather than being silently dropped.
$NO_MANAGER = 'Tanpa Manager'
$managerByOwner = @{}
$rosterFile = Join-Path $root 'manager_roster.csv'
if (Test-Path $rosterFile) {
  Import-Csv $rosterFile | ForEach-Object {
    $name = ([string]$_.Name).Trim()
    $asm = ([string]$_.ASM).Trim()
    if ($asm -eq '-' -or [string]::IsNullOrWhiteSpace($asm)) { $asm = $null }
    if ($name) { $managerByOwner[$name] = $asm }
  }
}
# Manual overrides, given directly by the user (2026-08-14) for people who ARE in the
# roster but whose ASM column is blank there — they sit at the top of that sheet's
# hierarchy, so the sheet has no "manager" for them even though one exists in practice.
# Checked before the CSV lookup so a blank ASM doesn't shadow this.
$managerOverrides = @{
  'Cendanawangi Lumoindong'      = 'Region Head'
  'Eleonora Irsya Listianingrum' = 'Cendanawangi Lumoindong'
  'Januar Aden Nugroho'          = 'Muhammad Yan Mallino'
  'Willa Rivoni'                 = 'Cendanawangi Lumoindong'
}
function Get-Manager {
  param([string]$OwnerName)
  if ($OwnerName -and $managerOverrides.ContainsKey($OwnerName)) { return $managerOverrides[$OwnerName] }
  if ($OwnerName -and $managerByOwner.ContainsKey($OwnerName) -and $managerByOwner[$OwnerName]) {
    return $managerByOwner[$OwnerName]
  }
  return $NO_MANAGER
}

# ------------------------------------------------------------- user name lookup
# The list endpoint returns owner_name but never created_by_name / assigned_to_name, so
# harvest names from rows where the owner IS the creator and reuse that map everywhere.
$names = @{}
foreach ($t in $tasks) {
  if ($t.owner_name -and $null -ne $t.owner_id -and -not $names.ContainsKey([string]$t.owner_id)) {
    $names[[string]$t.owner_id] = $t.owner_name
  }
}
foreach ($o in $oppsById.Values) {
  if ($o.owner_name -and $null -ne $o.owner_id -and -not $names.ContainsKey([string]$o.owner_id)) {
    $names[[string]$o.owner_id] = $o.owner_name
  }
}
function Name-Of { param($id) if ($null -eq $id) { return $null }
  $k = [string]$id; if ($names.ContainsKey($k)) { $names[$k] } else { "User #$k" } }

# Precompute tags per opportunity once, so both the task rows and the opportunity cards
# read from the same lookup instead of recomputing (and possibly drifting).
$oppTagsById = @{}
foreach ($o in $oppsById.Values) {
  $t = @()
  if ([string]$o.lead_source_detail -match 'Must\s*Win') { $t += 'MUST WIN' }
  $t += Get-AccountTags ([string]$o.account_id)
  if ($t.Count -gt 0) { $oppTagsById[[string]$o.id] = @($t) }
}

# ---------------------------------------------------------------------- bucket math

$now = [DateTimeOffset]::new((Get-Date), $JakartaOffset)
$rows = New-Object System.Collections.ArrayList

foreach ($t in $tasks) {
  $status = [string]$t.status
  $isOpen = ($status.ToLower() -ne 'completed')

  $created = Parse-CrmDate $t.created_at
  $updated = Parse-CrmDate $t.updated_at
  $due     = Parse-CrmDate $t.due_date

  $neverUpdated = ($t.version -eq 1) -or ($null -eq $updated) -or
                  ($null -ne $created -and $updated -eq $created)

  $touched = if ($updated) { $updated } else { $created }
  $daysSinceTouch = if ($touched) { [int][math]::Floor(($now - $touched).TotalDays) } else { $null }
  $daysOverdue = if ($due -and $due -lt $now) { [int][math]::Floor(($now - $due).TotalDays) } else { $null }

  # A due date earlier than the creation date is impossible as real work. In this data
  # it clusters at ~730 days (the year typed as 2024 instead of 2026), so these are
  # data-entry artifacts, not late work — split them out or they inflate "overdue".
  $dueBeforeCreated = $isOpen -and ($null -ne $due) -and ($null -ne $created) -and ($due -lt $created)

  $overdueRaw = $isOpen -and ($null -ne $due) -and ($due -lt $now)
  $overdue = $overdueRaw -and (-not $dueBeforeCreated)
  $noDue   = $isOpen -and ($null -eq $due)
  $stale   = $isOpen -and (-not $neverUpdated) -and ($null -ne $daysSinceTouch) -and ($daysSinceTouch -gt $StaleDays)

  # Tags live on the Opportunity (MUST WIN from lead_source_detail; HYPERCARE/STRATEGIC
  # from the parent account's CSM column) — carry them onto each task that points there.
  $taskTags = @()
  if ($t.related_object_type -eq 'Opportunity' -and $t.related_record_id) {
    $k = [string]$t.related_record_id
    if ($oppTagsById.ContainsKey($k)) { $taskTags = $oppTagsById[$k] }
  }

  $ownerName = Name-Of $t.owner_id

  [void]$rows.Add([ordered]@{
    id            = $t.id
    subject       = [string]$t.subject
    description   = [string]$t.description
    status        = $status
    priority      = [string]$t.priority
    creator       = Name-Of $t.created_by
    creatorId     = if ($null -ne $t.created_by) { [string]$t.created_by } else { '' }
    owner         = $ownerName
    manager       = Get-Manager $ownerName
    assignedTo    = if ($null -ne $t.assigned_to_id) { Name-Of $t.assigned_to_id } else { $null }
    relatedType   = [string]$t.related_object_type
    relatedId     = $t.related_record_id
    createdAt     = if ($created) { $created.ToString('yyyy-MM-dd') } else { $null }
    dueAt         = if ($due) { $due.ToString('yyyy-MM-dd') } else { $null }
    daysOverdue   = $daysOverdue
    daysSinceTouch= $daysSinceTouch
    neverUpdated  = [bool]$neverUpdated
    stale         = [bool]$stale
    overdue       = [bool]$overdue
    noDueDate     = [bool]$noDue
    badDueDate    = [bool]$dueBeforeCreated
    isOpen        = [bool]$isOpen
    mustWin       = $taskTags -contains 'MUST WIN'
    hypercare     = $taskTags -contains 'HYPERCARE'
    strategic     = $taskTags -contains 'STRATEGIC'
  })
}

# ------------------------------------------------------------- opportunity cards
# Only open-stage opportunities that actually carry an unfinished task.
$openTaskByOpp = @{}
foreach ($r in $rows) {
  if ($r.relatedType -ne 'Opportunity' -or -not $r.isOpen -or -not $r.relatedId) { continue }
  $k = [string]$r.relatedId
  if (-not $openTaskByOpp.ContainsKey($k)) { $openTaskByOpp[$k] = New-Object System.Collections.ArrayList }
  [void]$openTaskByOpp[$k].Add($r)
}

$cards = New-Object System.Collections.ArrayList
$missingOpp = 0
foreach ($k in $openTaskByOpp.Keys) {
  if (-not $oppsById.ContainsKey($k)) { $missingOpp++; continue }
  $o = $oppsById[$k]
  if ($OPEN_STAGES -notcontains [string]$o.stage) { continue }

  $tags = if ($oppTagsById.ContainsKey($k)) { $oppTagsById[$k] } else { @() }

  $product = @((Flatten-Value $o.nv_product_line), (Flatten-Value $o.service_level)) |
             Where-Object { $_ } | Select-Object -Unique
  # @(...) is load-bearing: Sort-Object unwraps a single-item collection, and .Count on a
  # bare OrderedDictionary returns its KEY count (21), not 1 — which silently inflated
  # taskCount on every card that had exactly one task.
  $cardTasks = @($openTaskByOpp[$k] | Sort-Object @{ e = { $null -eq $_.dueAt } }, dueAt)

  [void]$cards.Add([ordered]@{
    id          = $o.id
    name        = [string]$o.name
    stage       = [string]$o.stage
    owner       = [string]$o.owner_name
    manager     = Get-Manager ([string]$o.owner_name)
    account     = [string]$o.account_name
    product     = ($product -join ' · ')
    revenue     = if ($o.committed_revenue_mth) { [double]$o.committed_revenue_mth } else { $null }
    potential   = if ($o.total_potential_revenue_mth) { [double]$o.total_potential_revenue_mth } else { $null }
    currency    = if ($o.committed_revenue_mth__currency) { [string]$o.committed_revenue_mth__currency } else { 'IDR' }
    expClose    = [string]$o.expected_close_date
    tags        = @($tags)
    taskCount   = $cardTasks.Count
    overdueCount= @($cardTasks | Where-Object { $_.overdue }).Count
    tasks       = @($cardTasks | ForEach-Object {
                     [ordered]@{ id=$_.id; subject=$_.subject; status=$_.status; dueAt=$_.dueAt
                                 overdue=$_.overdue; neverUpdated=$_.neverUpdated
                                 badDueDate=$_.badDueDate; owner=$_.owner; assignedTo=$_.assignedTo } })
  })
}

$sorted = $cards | Sort-Object -Property @{e={$_.overdueCount};Descending=$true}, @{e={$_.taskCount};Descending=$true}
$open = ($rows | Where-Object { $_.isOpen }).Count
Write-Host ("Open={0}  NeverUpdated={1}  Stale={2}  Overdue={3}  NoDueDate={4}  BadDueDate={5}" -f `
  $open,
  ($rows | Where-Object { $_.neverUpdated }).Count,
  ($rows | Where-Object { $_.stale }).Count,
  ($rows | Where-Object { $_.overdue }).Count,
  ($rows | Where-Object { $_.noDueDate }).Count,
  ($rows | Where-Object { $_.badDueDate }).Count)
Write-Host ("Opportunity cards={0}  (skipped {1} closed-stage, {2} not fetched)" -f `
  $sorted.Count, ($openTaskByOpp.Count - $sorted.Count - $missingOpp), $missingOpp)
Write-Host ("Tagged: MUSTWIN={0} HYPERCARE={1} STRATEGIC={2}" -f `
  @($sorted | Where-Object { $_.tags -contains 'MUST WIN' }).Count,
  @($sorted | Where-Object { $_.tags -contains 'HYPERCARE' }).Count,
  @($sorted | Where-Object { $_.tags -contains 'STRATEGIC' }).Count)
$ownersInReport = @($rows | ForEach-Object { $_.owner } | Sort-Object -Unique)
$ownersWithManager = @($ownersInReport | Where-Object { (Get-Manager $_) -ne $NO_MANAGER })
Write-Host ("Manager roster: {0} nama di manager_roster.csv, {1}/{2} Owner di laporan punya manager" -f `
  $managerByOwner.Count, $ownersWithManager.Count, $ownersInReport.Count)

# --------------------------------------------------------------------------- render

$payload = [ordered]@{ tasks = @($rows); opportunities = @($sorted) }
$json = $payload | ConvertTo-Json -Depth 6 -Compress

$template = Get-Content (Join-Path $root 'report_template.html') -Raw -Encoding UTF8
$html = $template.Replace('"__DATA__"', $json).
                  Replace('__GENERATED__', $now.ToString('dd MMM yyyy HH:mm')).
                  Replace('__STALEDAYS__', [string]$StaleDays)

# ---------------------------------------------------------- redacted (publishable) variant
# Round-trip through JSON rather than re-deriving $rows/$cards: parses back into an object
# tree, then redact free-text fields in place before re-serializing. This guarantees the
# redacted payload is structurally identical to $json (same fields, same task/opportunity
# set) — only description/subject text differs.
$payloadRedacted = $json | ConvertFrom-Json
foreach ($t in $payloadRedacted.tasks) {
  $t.subject = Redact-ContactInfo $t.subject
  $t.description = Redact-ContactInfo $t.description
}
foreach ($o in $payloadRedacted.opportunities) {
  foreach ($t in $o.tasks) { $t.subject = Redact-ContactInfo $t.subject }
}
$jsonRedacted = $payloadRedacted | ConvertTo-Json -Depth 6 -Compress

$htmlRedactedFull = $template.Replace('"__DATA__"', $jsonRedacted).
                              Replace('__GENERATED__', $now.ToString('dd MMM yyyy HH:mm')).
                              Replace('__STALEDAYS__', [string]$StaleDays)

# Artifact publishing wraps content in its own <!doctype>/<html>/<head>/<body> skeleton, so
# strip ours — but keep <style> (move it to the front of the body) since Artifact allows
# inline <style>/<script> directly in the page content. <title>/favicon are passed as
# publish parameters instead of tags, since <head> is dropped entirely.
$styleMatch = [regex]::Match($htmlRedactedFull, '<style>.*?</style>', 'Singleline')
$style = if ($styleMatch.Success) { $styleMatch.Value } else { '' }
$bodyMatch = [regex]::Match($htmlRedactedFull, '<body>(.*?)</body>', 'Singleline')
$bodyInner = if ($bodyMatch.Success) { $bodyMatch.Groups[1].Value } else { $htmlRedactedFull }
$artifactHtml = $style + "`n" + $bodyInner.Trim()

$artifactResolved = [IO.Path]::GetFullPath((Join-Path $root "..\Laporan-Task-Indonesia-artifact.html"))
[IO.File]::WriteAllText($artifactResolved, $artifactHtml, (New-Object Text.UTF8Encoding($true)))
Write-Host "Wrote $artifactResolved (redacted, artifact-ready)"

$outResolved = [IO.Path]::GetFullPath($OutFile)
# BOM on purpose: the report is opened straight off disk via file://, where a BOM removes
# any chance of a browser guessing the wrong codepage for the Indonesian text and dashes.
[IO.File]::WriteAllText($outResolved, $html, (New-Object Text.UTF8Encoding($true)))
Write-Host "Wrote $outResolved"
