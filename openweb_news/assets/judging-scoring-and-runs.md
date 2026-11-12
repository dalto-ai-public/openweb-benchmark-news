# Judging, scoring and runs

This guide describes the base domain contract and historical default repair
procedure. Explicit newer input, date-reading and review profiles retain their
own versioned bindings. See `operator-workflow-v1.md` for the current standalone
preparation commands, independent method acceptance and staged clean-run gates.

## Inputs and grades

A search cell is one question sent to one configuration. A returned occurrence is
one original result slot in that cell. The primary judge, Pro, means the Gemini
model explicitly pinned in `method.json`; its settings are read from that file.
The recovered core means the date-only schema restrictions and exact canonical
full-field date replay ported from the reviewed source composition. The standalone
schema and full domain prompt are bundled for every role.
There is no standalone-only policy-application field.

Grades: 2 is an own-evidence answer meeting every required condition; 1 is a specific
undisqualified lead with missing proof; 0 is neither or an established disqualifier.
Null means a genuine grade-changing ambiguity or unavailable judgment. Missing proof
is `not_demonstrated`, not an unsupported failure. Known failures are candidate-scoped
unless a condition applies to the whole record. Take the highest supported candidate
under the failure/unknown precedence; do not transfer a child failure to a separately
supported collection pointer or turn a collection lead into an answer.

The model interprets semantics and date roles. Code checks exact quote locations,
predicate applicability, date intervals, required fields and grade arithmetic.
These checks are conformance checks, not independent verification of news facts.
All later scoped policies in `policies.json` apply with their stated precedence,
including source/forum limits, collection specificity, fixed-article date exclusions,
event stage and frame meanings, effective adopted schedules, publication context,
publication estimates and geography roles. Private one-off case exceptions are not
global product rules.

## Independent review

Each occurrence gets a fresh Pro position and an independent Codex position. A blind
Claude position is conditional: exactly one valid conforming numeric first position,
or two different such grades, requires the third position. Neither numeric first
position means unknown without another round. Matching conforming grades from any
two permitted positions select the final grade. A lone vote, three distinct grades,
an invalid/null third output or an unsupported majority remains unknown. Never
average grades. No automatic extra round or backend substitution follows.

All roles use the same five states: `conforming_numeric`, `nonconforming_numeric`,
`valid_unknown`, `invalid`, `unjudged`. The final grade does not rewrite source-output
validity. Pro may remain invalid while agreeing Codex/Claude positions establish a
numeric final outcome. Mechanical grades are not independent votes.

The original model proposal and decoded structural result remain separate. A
matching supported grade can supply a numeric position. A policy/base difference
requires own-source review and remains unavailable in the unattended route until
that conformance is established. Free-text reasons and repeated agreement alone
cannot supply an override. Blind packets never include another position.

## Scoring

Cutoffs and pool depth are owned in `method.json`. Original ranks remain unchanged.
Only the first occurrence of an exact URL in a cell earns gain; later duplicates
earn zero without moving the remaining results. Different URLs stay distinct unless
an explicitly reviewed alias input joins them. Duplicate occurrences still receive
their own model tasks and remain in the retained evidence.

Gain equals the grade (0, 1 or 2). DCG sums gain divided by log2(rank + 1). NDCG
divides that value by a shared ideal: the best reviewed variant for each unique URL
pooled across compared configurations, sorted by gain. An unknown variant can keep
that ideal undetermined unless enough resolved grade-2 URLs fix the cutoff. Unknown
grades never become zero. A zero ideal is not applicable, not a perfect or zero NDCG.

Answer, lead and useful rates divide first-URL counts by the full cutoff, including
empty slots. Answer findings alone supply reviewed report/event/facet breadth. Empty
successful responses count as answered. Failed or uncollected calls deliver zero
where the metric is defined; undetermined shared ideals remain pending.

Availability selection reads answered status before quality. If common answered
coverage misses the configured target, remove the least available configuration
using the frozen tie order, stopping at two. Show the actual remaining coverage;
never assert the target was met just because removal stopped.

Complete-family weighting averages wordings within a need, needs within a family,
then families equally. A family enters a metric only when all declared members are
in scope and determined for every compared configuration. Conditional quality,
delivered utility and resolved-only/partial results have different denominators;
report them explicitly. Unknown, pending, N/A, empty and failure remain distinct.

## Execution and repair

Prepare a finite package binding questions, requests, code/assets, settings, backends,
credential reference names, spending estimates/policy, attempt bounds and private
destinations. A separate owner receipt must approve its exact hash before live
credentials or transport are used. Subscription calls need the same finite scope.
CLI dollar metadata and admission reservations are estimates, not invoices.

Fsync each attempt before dispatch. Resume uses the same package and saved results.
An interrupted attempt without a complete result is uncertain; inspect it without
automatic resend. Completed successes are reused regardless of result quality.
Failed attempts and full permitted responses remain retained. No deletion window or
automatic cleanup discards run evidence.

A legacy pilot repair branch is a conditional, separately bound continuation for one code repair
and affected validation in the approved pilot. It cannot change frozen rules,
recollect successful cells or start a second experiment. Uncovered scope needs a
new concrete decision. Review all failures and a reproducible varied sample of
twenty saved judgments after the approved pilot. Stop on a remaining blocking defect
or proceed with disclosed limits; do not tune repeatedly to finish a table.

## Returned titles, metadata and snippet lengths

Every new collection records numeric observations from the original returned
positions within the benchmark pool depth. Standard reports automatically include
these observations, including collection-only runs, resumed runs, later reviews
and pooled checkpoints. The versioned definitions are owned by
`returned-fields-v1.json`; no extra analysis command or model call is needed.

The HTML section emphasizes combined snippet length and separates primary and
additional snippets. Aggregate exports are `returned-fields.json`,
`returned-fields.csv` and `snippet-length-quality.csv`. They report title and
native-metadata availability, character/word distributions and per-provider
length-band grade breakdowns. These exports contain numeric observations and
controlled provider identifiers, not raw returned text.

Counts use measured returned positions, including repeated URLs; unreturned
slots and failed calls do not become empty snippets. Legacy runs without recorded
measurements remain explicitly unavailable. Unknown grades and ungraded positions
remain separate. Grade rates use resolved own grades and are not family-weighted
nDCG. Provider tables sort by median snippet characters, not quality. Longer text,
metadata presence or a supplied date does not prove a better answer or a valid
publication/event date. Length associations are descriptive, not causal.
