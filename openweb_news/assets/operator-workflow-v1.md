# Operator workflow

Prepared packages, owner receipts and immutable private attempt ledgers own
method choices and run evidence. The packaged method assets define the rules.

## First run and recovery

The primary workflow in INSTALL.md starts with `starter --simple` and accepts
plain questions. `prepare-question-workflow` binds the question list, settings,
review capacity and derivation. After an exact owner execution approval,
`question-workflow` authors contracts, obtains independent semantic admission,
and handles staged checkpoint review automatically. `starter` exports
constructed fixtures without approvals. `--mode fixture` at preparation selects
synthetic responses. `clean-run --fixture-response FILE` is accepted only for
fixture packages.
The starter selects `fixture_accounting: "conservative-rate-simulation-v1"`
so the existing frozen rates exercise nonzero synthetic reservations/holds.
This profile is rejected in live mode; it never represents billed service use.
Omitting `--archive-reference` declares archive comparison unavailable. Supplying
one preserves strict archived closure, scope and configuration checks.
`prepare-date-trust --no-date-diagnostics` explicitly records absent first-run
diagnostics, while source bytes and exact owner field choices remain mandatory.

The coordinator requires an exact independent checkpoint verdict after each
stage. The automated workflow dispatches and validates that review. In the
structured-contract workflow an external reviewer supplies the receipt. Preserve frozen
packages, ledgers, raw attempts, holds and date anchors for resume. `status` reads
current evidence without dispatch; `verify` checks authenticity offline.
A covered retry/recovery policy must be declared before execution. Same-work
`prepare --reuse-from PACKAGE` preserves bound search-dispatch receipts and
effective requests under the ordinary snapshot/import guards; it never
regenerates a missing anchor or authorizes historical execution under new code.

## Prepare the bank and method

`validate-bank --questions SOURCE_JSONL` checks question structure and method
compatibility. It does not verify semantic quality or redistribution rights.
`freeze-bank --questions SOURCE_JSONL --output NEW_DIRECTORY` preserves every raw
source byte and question field in source order. Optional `--phase-counts` must
cover the complete bank. It never rewrites questions in response to scores.
Existing admitted banks can be used directly through their frozen selection.

`prepare-date-capabilities --config CONFIG --output DIRECTORY` records the exact
endpoint/mode/date-control meaning and supported omissions before approval.
Publication bounds, event dates and mixed or rolling freshness remain distinct.
No request change is activated by capability discovery. Explicit request profiles
are separate choices; their receipts disclose widening, unsupported bounds and
unclear meanings. A retrieval filter never establishes date truth or eligibility.
Linkup's selected filter refers to an unspecified considered-result date; it is
not established as an original-publication filter. Widening its bounds does not
resolve that uncertainty. Parallel documents publication meaning but supplies no
upper bound. The owner selected both with these limitations retained.

`date-diagnostics --compiled-directory DIRECTORY --source-package PACKAGE --source-report REPORT --output DIRECTORY`
computes read-only checks from saved compiled occurrences and their exact source
configuration mapping. Write output outside the compiled source tree to preserve
that input scope. It reports URL-date and explicitly labelled own-dateline
comparisons, later-date rates, unique-URL and occurrence denominators, negative
date-role controls and unavailable/ambiguous evidence. No provider or model calls
are made. URL and page dates can share an extraction source; these checks are
supporting observations, not independent publication-date accuracy.
Optional `--thresholds JSON` supplies explicit recommendation-only thresholds.
Without thresholds there is no threshold-based suggestion. Thresholds never
change owner selections, native meanings, grades or execution permission.
For a threshold-based onboarding screen, provide the operator's reviewed
threshold JSON explicitly with
`--screening-profile within-one-day-supporting-datelines-v2`. Use
`date-diagnostics --help` to see the supported options and retain the exact
threshold file and decision with the diagnostic. An ad hoc report without
thresholds makes no screening suggestion.
This screening profile compares URL/native dates within one calendar day and
warns when native dates are later by more than one day. Labelled own datelines
support screening only when their comparable unique-URL count reaches the
declared minimum; scarcity alone does not prevent a suggestion. With adequate
datelines, their agreement also uses one calendar day. Exact-day counts remain
visible. The legacy explicit profile retains exact-day/required-dateline rules
for replay. These screening definitions never widen a question's grading window
or change date evidence, native roles, field admission or grades.

Brave Web Search's nested `article.date` is documented as article publication;
it is a candidate for the same occurrence review as other publication fields.
It is separate from mixed-role `page_age`. Candidate status is not owner admission
or verified original publication. Seltz `properties.fetched_at` is an explicit
diagnostic negative-control cohort; its saved native role is not reclassified,
and matching dates do not establish a publication meaning or an alias.

New provider onboarding currently requires reviewed code entries in
`provider_dates._ROWS` and `date_trust._FIELDS`, with tests for the exact
endpoint/mode and field roles. An unreviewed product is not automatically covered
by another product's documentation or by good diagnostic agreement.
Onboarding also reviews documented text-format controls for the exact judged
title/snippet/excerpt fields: record the default, affected field and limits.
Controls for unrequested full-page or scraped content do not clean the selected
snippets. Query-relevant highlights versus short descriptions are a content
choice, not merely markup removal. Disclose inherited non-default choices and
obtain the owner's decision before changing them; apply any chosen request
profile consistently across the benchmark. Preserve meaningful text and exact
quote traceability rather than stripping symbols indiscriminately.
The selected Firecrawl Web configuration deliberately keeps `highlights=false`,
an inherited non-default choice explicitly retained by the owner. It requests
plain site descriptions instead of query-relevant passages; no full-page scrape
is requested. This content choice is disclosed separately from Brave's supported
decoration cleanup and from the other providers' native Markdown output.

`prepare-date-trust --config CONFIG --diagnostic FILE --source FILE --output DIRECTORY`
pins reviewed source bytes and diagnostics, then writes undecided owner choices
for every selected configuration. Repeated diagnostic/source options are allowed.
The owner chooses exact eligible fields or explicit empty lists in the provided
receipt. Agreement with URL dates is a selected diagnostic, not an accuracy rate.
Native date meaning is separate from admission; occurrence applicability,
credibility, conflicts and all other question requirements still need review.
An excluded native field cannot pass or fail publication/availability predicates
or resolve an ambiguous own-text date for those predicates, even if its retained
native role is first publication. Article own-text evidence remains available.

For the selected `blind-own-evidence-projection-v1` input profile, set
`judge_input_profile` and `date_trust_package` in the configuration.
`prepare-method --config CONFIG --output DIRECTORY` freezes the exact new method,
reviewers, provider/configuration choices, trust receipt, code and assets without
requiring an earlier benchmark. Its owner receipt accepts that method only.
Method acceptance does not approve question scope, dispatch or spending.

The model view omits unnecessary structured scoring/rank/configuration hints and
uses neutral source aliases. Raw captures and source bindings remain private and
unchanged. Decoder restoration retains the raw model verdict and exact source
quote locations. Genuine evidence can still identify its origin; this is not a
claim of perfect blinding or human accuracy. Legacy packages retain their selected
older input contracts under their frozen runtimes.

## Freeze and run the selected clean lifecycle

Use the ordinary `prepare` command with the accepted method referenced by
`method_acceptance_package` to create a new native configuration template. It is
not an old report or inherited run authority, and preparation makes no calls.

`prepare-clean-run --template-package PACKAGE --selection SELECTION --accepted-method METHOD_PACKAGE --archive-reference REFERENCE_JSON --ceiling AMOUNT --output DIRECTORY`
freezes the complete predictable lifecycle. The approved selection declares
stage sizes. The original bank order and all stage prototypes are bound up front.
The shared API ceiling governs new native costs and holds across stages;
subscription quota/time is separate. Existing review stages require isolated
Codex app-server and Claude subscription backends. No API fallback is selected.
The generated `APPROVAL-REVIEW.md` explains the frozen stage scope, logical task
caps, conditional reviews and quota limits. Native preparation estimates based
on empty evidence are a baseline rather than a worst-case spend forecast; the
shared ceiling is an execution bound. Task caps do not establish available
subscription quota or promise a completion date.

When supplied, the archived reference binds the old report and closure. A report copied by an
accepted-review wrapper also binds that wrapper package and its owner receipt.
The archive supplies ranking diagnostics only; no old capture, vote, cost or
approval authority enters the clean run. Hash verification is not complete
historical judgment replay or proof of correctness.

After explicit owner approval of the exact lifecycle package, `record-approval
--package PACKAGE --receipt RECEIPT` records that existing decision without
dispatch or another permission loop. `clean-run --package PACKAGE --approval
RECEIPT --env-file EXPLICIT_EXTERNAL_FILE` runs or resumes the next covered stage.
Every stage performs native collection/judging, ordinary review, native completion
audit, pooled reporting, accepted-method review and, when an archive is supplied,
archived ranking comparison. First runs report that diagnostic unavailable.
Uncertain attempts and failures retain their evidence and existing policy; no
success is repeated merely to improve a score. Quota pauses follow the frozen
policy without automatic reset-credit consumption or API substitution.

The controller stops at `awaiting_stage_review`. An independent reviewer examines
actual emitted inputs, date/source/quote behavior, changed and unchanged cases,
ranking movement, execution invariants and cost holds. The supplied review receipt
binds the exact evidence. `record-stage-review --package PACKAGE --receipt REVIEW`
records it; it does not approve new paid scope. A covered continuation may then
resume under the original exact lifecycle approval. Material method changes or
unresolved material findings stop continuation; previous new-run results need an
explicit disposition before the method changes. Unknowns or rank movement alone
are not failure thresholds. Final200 remains a separate scope decision.

## Inspect without dispatch

`status --package PACKAGE` derives state, holds, stops, estimates and report paths
from existing private files without creating a run or account session. `verify
--package PACKAGE` applies supported offline package/evidence checks and reports
unclassified completions as a failed check. Status alone is not replay verification.

`check-credentials --package PACKAGE [--env-file EXPLICIT_FILE]` reports names and
presence only. It does not authenticate accounts, use a default dotenv path or
print values. Missing presence is a nonzero command result; passing presence does
not establish account access, quota or billing readiness.

`compare-reports --archived-report OLD --current-report NEW --output DIRECTORY`
shows provider rank/score movement on the same questions using existing family
weighting. Equivalent returned evidence is not required. Coverage, ties, unknowns
and partial rankings remain explicit. Investigate large unexplained movements
using traceable requests, inputs and results, not a target score or agreement
with the archive. Small checkpoints do not establish a full-benchmark ranking.

For a newly selected Tavily general mode, select `tavily-general` explicitly in
place of historical `tavily-news`. The catalog retains both identities for replay;
the clean roster still selects one Tavily product. General mode keeps native date
inclusion and strict date filtering at their defaults, with no new publication
field admission. A closed archive reference may explicitly declare
`configuration_replacements: {"tavily-general": "tavily-news"}`. The lifecycle
binds and displays this diagnostic pairing and rejects unsupported substitutions
or duplicate pairings. Returned evidence and votes are never imported. NDCG
comparison remains unavailable when configuration pools differ.

The optional `perplexity-documented-context-default-v1` request profile omits
only historical `search_context_size=high`; it leaves web mode, result depth and
date mapping unchanged. Historical requests retain their original bytes. A
search-only comparison can support the choice but cannot prove server behavior
or equivalence. Select the profile only in the reviewed new method package.
