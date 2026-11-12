# Installation and private setup

Use Python 3.11 or later on a POSIX host (the attempt lock uses `fcntl`). Runtime
code has no third-party Python dependencies. Building/installing the package uses
the public setuptools build dependency. Question preparation uses Python's
`zoneinfo` and the operating system's public IANA timezone database.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
.venv/bin/python -m unittest discover -v
.venv/bin/openweb-news example --output "$HOME/openweb-news-example"
```

The example is entirely synthetic and makes no network or model calls. Use a
dedicated private output directory. `python -m openweb_news` is the equivalent
entry point when working directly from a checkout. The packaged method documents
are [purpose/scope](openweb_news/assets/purpose-and-scope.md),
[questions/references](openweb_news/assets/questions-and-references.md), and
[judging/scoring/runs](openweb_news/assets/judging-scoring-and-runs.md).

## Primary first-run path

The automated entry point accepts UTF-8 text with one question per nonempty line.
It runs contract authoring, independent semantic admission and the existing clean
collection/judging/review/scoring/report pipeline. Checkpoint review is dispatched
automatically through the existing isolated agent executor; a negative verdict
stops continuation. No routine review files need to be copied by hand.

Start in a new private directory outside the checkout. This example is entirely
synthetic, needs no credentials and makes no live calls:

```sh
work="$HOME/openweb-news-simple-first-run"
openweb-news starter --simple --output "$work/inputs"
openweb-news prepare-date-trust --config "$work/inputs/config.json" --no-date-diagnostics --source "$work/inputs/source-notice.txt" --output "$work/inputs/trust"
```

The simple starter exports `simple-questions.txt` and settings, with no complete
question contracts. For your own run, supply your question text and honest origin
(`actual`, `expert` or `realistic_generated`). Optional `--context FILE` supplies
explicit interpretation context, such as a relative-date anchor. Unresolved
meaning blocks admission; no default period is invented. Original source bytes,
drafts, raw agent outputs, review decisions and failures remain private and replayable.

Initial owner choices remain explicit. Copy `inputs/trust/approval-template.json`
to `inputs/trust/approval.json`, preserve its hash, set `approved: true`,
`approved_by` and `approved_at`, and give each provider a fixture-only selection
`{"eligible_fields": [], "reason": "Synthetic exclusion; diagnostics unavailable"}`.
For real use, supply actual reviewed field choices and sources. Then prepare the
method:

```sh
openweb-news prepare-method --mode fixture --config "$work/inputs/config.json" --output "$work/inputs/method"
```

Copy that method's `approval-template.json` to `approval.json` in the same folder.
Preserve its hash and explicitly set `approved: true`, `accept_method: true`,
`approved_by` and `approved_at`. Method acceptance does not authorize spending.

```sh
openweb-news prepare-question-workflow --questions "$work/inputs/simple-questions.txt" --config "$work/inputs/config.json" --mode fixture --origin realistic_generated --stage-counts 2 2 --output "$work/run"
```

Review `run/APPROVAL-REVIEW.md` and `workflow.json`. They bind the original questions,
context, settings, method, exact child-derivation procedure, stages, spending
ceiling, subscription task bounds and private destinations. Copy the workflow's
`approval-template.json` to `$work/owner-execution.json`, preserve its hash and set
`decision: "approved"`, `accept_derived_execution: true`, `approved_by` and
`approved_at` only after accepting that exact scope. The program does not decide
or manufacture this approval. This approves the derivation and spending before
model-authored contracts exist; it is not approval of specific final contracts.

```sh
openweb-news question-workflow --package "$work/run/workflow.json" --approval "$work/owner-execution.json"
```

One command runs both stages through the final reviewed closure. Each checkpoint
uses capacity-checked review parts and verified unchanged-case coverage. Its output names
the HTML and JSON report; CSV files are beside them. Repeat the same command to
resume. Completed calls and decisions are reused; uncertain interrupted agent
attempts remain held without a silent resend. A new output directory is required
for a new workflow. A rejected or invalid completed author/reviewer response also
remains blocked on rerun: inspect its retained evidence and prepare a separately
approved workflow for any corrected input or settings. No historical question
bank, run or report is imported or replaced.

Fixture author/admission/checkpoint responses are labelled synthetic stubs, not
actual independent model decisions or evidence of semantic accuracy. Nonzero
fixture cost reservations simulate the existing accounting and are not charges.
For live use, select real provider/reviewer settings and credential references,
remove `fixture_accounting`, obtain real native-field and method acceptance in
`live` mode, and prepare/approve the workflow in that mode. Add an explicit external
`--env-file FILE` only when executing that approved live package. The existing
clean route requires Codex and Claude subscriptions; no fallback is selected.
Oversized, invalid or incomplete agent responses are retained and stop the
affected stage; evidence is never silently truncated to fit a model context.

### Reviewer capacity and complete checkpoint coverage

New question workflows require `review_capacity` in the input configuration.
The simple starter supplies **fixture-only declarations**, not real model limits:

```json
{
  "profile": "complete-checkpoint-capacity-v1",
  "kind": "fixture",
  "max_checkpoint_calls": 64,
  "reviewers": {
    "codex": {"model": "synthetic-model", "context_window_tokens": 500000, "max_output_tokens": 4096, "scaffolding_reserve_tokens": 8192, "source": "Synthetic fixture only"},
    "claude": {"model": "synthetic-model", "context_window_tokens": 500000, "max_output_tokens": 4096, "scaffolding_reserve_tokens": 8192, "source": "Synthetic fixture only"}
  }
}
```

For live use, choose `kind: "declared-live"`, bind each exact selected reviewer
model, and supply reviewed model/CLI/account capacity evidence in `source`. Set
the documented context window, maximum output and a justified native-scaffolding
reserve. `max_output_tokens` here reserves capacity; it does not change a CLI's
output limit. Use its supported maximum or an independently enforced limit, not
a desired answer length. Declare the checkpoint call bound explicitly. None of
these live values is guessed, and fixture declarations cannot authorize live use.
The workflow approval binds these settings and the maximum additional subscription
tasks: one author, one admission, plus the declared call bound per checkpoint.
The actual partition is known only after collection/judging; an insufficient bound
can stop after that spend, before checkpoint review sends. For scale context, the
earlier five-question/two-provider fixture had ten complete case/outcome/review
units averaging about 75.8 KB, with a maximum of 162.8 KB, before shared documents,
common context and native CLI overhead. These are measured fixture examples,
not live-size estimates or promises about the number of calls a real bank needs.

Before authentication or dispatch, the runner compares serialized packet UTF-8
bytes, including instructions/schema, with the declared context window after
output/scaffolding reserves. It also checks that the minimum coverage echo fits
the output allowance. This is conservative byte-based planning, not exact token
counting or proof of actual account capacity. A complete verdict may need more
output than its minimum echo; incomplete responses remain blocked.

Checkpoint parts keep every case with its final outcome and associated ordinary
and accepted review requests/responses. All report/audit rows or values remain
covered. A complete partition must rehydrate the original checkpoint source
exactly before any review is sent. Unchanged cases can reuse an already verified
continuing checkpoint through exact fingerprints and bound source/plan/receipt;
new reviews or changed outcomes require fresh case review. Current and previous
score overviews provide context without recalculating or changing scores.

Each independent part verdict must echo its exact unit IDs. Only complete
continuing coverage can derive a continuing clean gate. Negative, invalid or
uncertain parts stop; completed parts are reused without resend. An indivisible
case/row that cannot fit, or a complete partition exceeding the approved call
bound, blocks before any checkpoint review send. Inputs and prior work remain
retained. Select new settings only through a new reviewed workflow; there is no
silent sampling, truncation, context upgrade or extra-call fallback. Original
workflows retain their frozen runtime; do not edit their packages to adopt this.

## Credentials and configuration

Copy `.env.example` outside this checkout, preferably to
`~/.config/openweb-benchmark-news/.env`. Create its parent with mode 0700 and protect
the file with mode 0600. Populate only the selected services. Environment files are
literal assignments: no shell evaluation, variable expansion or automatic sourcing.
Only `--env-file` selects a file. Explicit file values take precedence over exported
variables with the same canonical name. Generic vendor API-key names are not used
automatically. An explicit credential-reference override changes the prepared package.

Keep run configuration, real contracts, execution packages, approvals and output
outside the checkout. Configuration contains credential variable names, never values.
Full permitted raw results and attempts stay private. Known credential echoes are
redacted from retained responses and marked as redacted, with original/retained
hashes; such a response needs inspection. HTTP session/auth response headers are
excluded from retained header metadata. Responses are not shortened or refetched.

## Subscription reviewers

Install the official [Codex CLI](https://developers.openai.com/codex/cli) and
[Claude Code](https://code.claude.com/docs/en/setup), then sign in to your own
subscriptions using their documented login commands. Do not copy authentication
files into this repository or an execution package. The runner preserves each CLI's
existing auth home, strips inherited API-key/gateway variables, checks auth mode,
and never falls back to API billing.

Codex uses one local stdio app-server with an empty HOME and auth-only CODEX_HOME.
Code dispatches each case to a fresh ephemeral judge thread with its own input and
output schema. Judges run in parallel without a parent model, forked history or
additional coordinator inference. One runtime owns the authentication lifecycle.
Only a symlink points to the existing local `auth.json`; credential bytes are never
read or copied by the wrapper. A native CLI auth refresh is moved atomically back
to its original private location; concurrent auth changes stop the wrapper and
retain the private refresh for inspection. Keyring-only Codex auth is unsupported
by this isolated route: use the CLI's documented file-auth login setup explicitly.
Global AGENTS files, user settings and skills stay outside the overlay. Memory,
plugins, MCP, shell/image/goal tools are disabled, and ChatGPT auth is forced.
The installed CLI still offers its native request-user-input helper; it has no
evidence access, and any attempted tool/interaction fails the output audit.
Claude uses safe mode (not bare mode, which excludes subscription
auth), an empty tool list and MCP config, disabled skills and no session persistence.
The wrapper forces `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
`DISABLE_TELEMETRY=1` and `DISABLE_ERROR_REPORTING=1` itself. It requires the native
init to report analytics and product feedback disabled; unexpected built-in or
user plugins still fail the audit. The native loopback test uses this same
environment builder, with only dummy auth and a local fake endpoint added.
Each gets its independent packet plus native CLI system scaffolding in a new
external working directory. Installed-CLI loopback tests with dummy auth and
planted global/project instruction, skill and memory sentinels checked the actual
wire requests: those sentinels were absent. Claude exposes its native schema-output
helper and two builtin plugin metadata entries; no user plugins or evidence-access
tools are allowed. Missing
isolation flags, wrong auth mode, or unexpected context/tool events fail closed.
Managed organization policy can still apply. See the
[Codex configuration reference](https://developers.openai.com/codex/config-reference)
and [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

Set both reviewers explicitly in private configuration:

```json
{
  "codex": {"backend": "codex-subscription", "transport": "app-server-stdio-v1", "model": "YOUR_EXACT_CODEX_MODEL", "reasoning_effort": "medium", "timeout_seconds": 600},
  "claude": {"backend": "claude-subscription", "model": "YOUR_EXACT_CLAUDE_MODEL", "reasoning_effort": "high", "timeout_seconds": 600}
}
```

These are the `reviewers` member of the full configuration, not a complete config.
Requested model/settings and emitted served-model/auth/usage metadata remain
separate. Some CLI event streams omit served-model identity; record it as unknown
rather than claiming it was verified. Fixture tests check construction/isolation
handling; each operator must verify actual access and execution for the selected
account and installed CLI. Completed approved runs do not verify a new setup.

## Explicit API alternatives

Either reviewer role can use `openai-api` (Responses) or `anthropic-api` (Messages).
Set `model`, `secret_ref`, `max_output_tokens`, `reasoning_effort`, `timeout_seconds`,
`price_input_per_million_usd` and `price_output_per_million_usd` explicitly. Model
access, structured-output support and rates must match that user's chosen account.
The implementations use official [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
and [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
API and subscription routes are distinct configurations and need not give identical
outputs. There is no automatic fallback between them.

## Resume and inspect a saved run

Repeat the same `question-workflow` command with its original package, execution
receipt and explicit external environment file, if applicable. Keep the run
directory and retained decisions together. Use `openweb-news status --help` and
`openweb-news verify --help` for read-only checks of a saved package.

The structured-bank and explicit checkpoint-review workflow remains available
for already prepared JSONL contracts. Start with `validate-bank --help` and the
[operator workflow](openweb_news/assets/operator-workflow-v1.md). Historical
recovery profiles serve existing bound runs. Select a profile only when it fits
the retained source evidence and its declared package requirements.

See [provider configuration](PROVIDERS.md) before changing the roster and
[score replay](REPRODUCE.md) for offline verification of saved figures.
