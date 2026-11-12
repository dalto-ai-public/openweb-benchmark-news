"""Public command-line interface. Live execution always requires an exact approval."""
import argparse
import json
from pathlib import Path
import sys

from . import fixtures
from .common import digest, load, write_private
from .preparation import prepare
from .runner import run
from ._method.providers import configuration_catalog


def main(argv=None):
    parser = argparse.ArgumentParser(prog="openweb-news", description="Standalone own-evidence news benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="show carried configurations; no account or network access")
    demo = commands.add_parser("example", help="run the bundled synthetic example offline")
    demo.add_argument("--output", type=Path, required=True)
    starter = commands.add_parser("starter", help="export editable synthetic first-run inputs, with no approvals or calls")
    starter.add_argument("--output", type=Path, required=True)
    starter.add_argument("--simple", action="store_true", help="export plain question text for the automated question-to-report workflow")
    workflow_prep = commands.add_parser("prepare-question-workflow", help="freeze simple questions and the exact automated derivation for owner approval; no calls")
    workflow_prep.add_argument("--questions", type=Path, required=True, help="UTF-8 text, one question per nonempty line")
    workflow_prep.add_argument("--config", type=Path, required=True)
    workflow_prep.add_argument("--mode", choices=["fixture", "live"], required=True)
    workflow_prep.add_argument("--origin", choices=["actual", "expert", "realistic_generated"], required=True)
    workflow_prep.add_argument("--context", type=Path, help="explicit interpretation context; no automatic date anchoring")
    workflow_prep.add_argument("--stage-counts", type=int, nargs="+")
    workflow_prep.add_argument("--output", type=Path, required=True)
    workflow_run = commands.add_parser("question-workflow", help="run/resume approved question authoring, semantic admission and the existing clean pipeline")
    workflow_run.add_argument("--package", type=Path, required=True)
    workflow_run.add_argument("--approval", type=Path, required=True)
    workflow_run.add_argument("--env-file", type=Path)
    prep = commands.add_parser("prepare", help="freeze a private execution package without credentials or calls")
    prep.add_argument("--mode", choices=["live", "fixture"], default="live", help="fixture uses only synthetic transports; receipts cannot authorize live mode")
    prep.add_argument("--config", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    source_mode = prep.add_mutually_exclusive_group()
    source_mode.add_argument("--reuse-from", type=Path, help="freeze completed/uncertain history from this prior package for continuation of identical work")
    source_mode.add_argument("--fresh-from", type=Path, help="freeze saved collection for a separately approved new judge epoch; no source votes or provider calls")
    execute = commands.add_parser("run", help="run/resume an explicitly approved package")
    execute.add_argument("--package", type=Path, required=True)
    execute.add_argument("--approval", type=Path, required=True)
    execute.add_argument("--env-file", type=Path)
    execute.add_argument("--retry-failed", action="store_true", help="use only the package's covered completed HTTP-failure retry branch")
    replay = commands.add_parser("replay", help="optional explicit external saved-label scoring comparison")
    replay.add_argument("--inputs", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare-reports", help="compare provider rankings on the same question scope offline; diagnostic only")
    compare.add_argument("--archived-report", type=Path, required=True)
    compare.add_argument("--current-report", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    capabilities = commands.add_parser("prepare-date-capabilities", help="review exact provider date controls and omissions offline")
    capabilities.add_argument("--config", type=Path, required=True)
    capabilities.add_argument("--output", type=Path, required=True)
    diagnostics = commands.add_parser("date-diagnostics", help="measure saved native dates against URL dates and labeled own datelines; suggestion only, no calls")
    diagnostics.add_argument("--compiled-directory", type=Path, required=True)
    diagnostics.add_argument("--source-package", type=Path, required=True)
    diagnostics.add_argument("--source-report", type=Path, required=True)
    diagnostics.add_argument("--thresholds", type=Path, help="explicit recommendation-only thresholds JSON; omission produces no threshold suggestion")
    from .date_diagnostics import LEGACY_SCREENING_PROFILE, CALENDAR_SCREENING_PROFILE
    diagnostics.add_argument("--screening-profile", choices=[LEGACY_SCREENING_PROFILE, CALENDAR_SCREENING_PROFILE],
        default=LEGACY_SCREENING_PROFILE, help="explicit screening definitions; onboarding uses the within-one-day supporting-datelines profile")
    diagnostics.add_argument("--output", type=Path, required=True)
    trust = commands.add_parser("prepare-date-trust", help="pin date diagnostics and prepare undecided owner field selections; no calls")
    trust.add_argument("--config", type=Path, required=True)
    diagnostic_choice = trust.add_mutually_exclusive_group(required=True)
    diagnostic_choice.add_argument("--diagnostic", type=Path, action="append")
    diagnostic_choice.add_argument("--no-date-diagnostics", action="store_true", help="declare first-run diagnostics unavailable; exact source and owner selections remain required")
    trust.add_argument("--source", type=Path, action="append", required=True)
    trust.add_argument("--output", type=Path, required=True)
    bank_validate = commands.add_parser("validate-bank", help="validate private question structure without semantic or execution approval")
    bank_validate.add_argument("--questions", type=Path, required=True)
    bank_freeze = commands.add_parser("freeze-bank", help="freeze all source questions in their original order without rewriting them")
    bank_freeze.add_argument("--questions", type=Path, required=True)
    bank_freeze.add_argument("--phase-counts", type=int, nargs="+")
    bank_freeze.add_argument("--output", type=Path, required=True)
    method = commands.add_parser("prepare-method", help="freeze a new method for explicit owner acceptance without an earlier run")
    method.add_argument("--mode", choices=["live", "fixture"], default="live")
    method.add_argument("--config", type=Path, required=True)
    method.add_argument("--output", type=Path, required=True)
    clean = commands.add_parser("prepare-clean-run", help="freeze a clean staged lifecycle, optionally with an authenticated archive; no calls")
    clean.add_argument("--template-package", type=Path, required=True)
    clean.add_argument("--selection", type=Path, required=True)
    clean.add_argument("--accepted-method", type=Path, required=True)
    clean.add_argument("--archive-reference", type=Path, help="closed archived benchmark reference; omission declares a first run with unavailable comparison")
    clean.add_argument("--mode", choices=["live", "fixture"], default="live")
    clean.add_argument("--ceiling", required=True)
    clean.add_argument("--output", type=Path, required=True)
    clean.add_argument('--rebind-seed',action='store_true',help='declare one compatible reviewed-seed method transition')
    clean.add_argument('--search-provider-ids',nargs='+',help='explicit ordered search subset; other configurations are deferred')
    clean.add_argument("--stage-counts",type=int,nargs='+',help="new questions per remaining stage; use 20 30 40 100 after the repaired first10")
    clean.add_argument("--seed-report",type=Path,help="accepted checker-only repaired prefix")
    clean.add_argument("--seed-review",type=Path,help="independent review bound to that exact repaired prefix")
    clean.add_argument("--recollection-source",type=Path,help="bound accepted repair evidence for first-stage provider-cell replacement")
    clean.add_argument("--verified-prefix",type=Path,help="prepared hash-bound prefix.json for lean continuation; no historical replay")
    clean_run = commands.add_parser("clean-run", help="run/resume an exact approved clean lifecycle, stopping at checkpoint review")
    clean_run.add_argument("--package", type=Path, required=True)
    clean_run.add_argument("--approval", type=Path, required=True)
    clean_run.add_argument("--env-file", type=Path)
    clean_run.add_argument("--fixture-response", type=Path, help="explicit synthetic review response for fixture mode only; never live")
    stage_review = commands.add_parser("record-stage-review", help="record an existing exact checkpoint review; no dispatch")
    stage_review.add_argument("--package", type=Path, required=True)
    stage_review.add_argument("--receipt", type=Path, required=True)
    for name, help_text in (("status", "read current stage, holds, stops, costs and report paths without dispatch"),
                            ("verify", "verify a native or clean package offline"),
                            ("check-credentials", "check required credential presence only; no authentication calls"),
                            ("record-approval", "record an already explicit exact owner receipt; no dispatch")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--package", type=Path, required=True)
        if name == "check-credentials":
            command.add_argument("--env-file", type=Path)
        if name == "record-approval":
            command.add_argument("--receipt", type=Path, required=True)
    repair = commands.add_parser("redecode", help="use the package's single offline saved-response repair branch")
    repair.add_argument("--package", type=Path, required=True)
    repair.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("audit-sample", help="prepare all failures and twenty varied saved judgments for operational inspection")
    audit.add_argument("--run-directory", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    review_prep = commands.add_parser("prepare-review", help="prepare standalone saved-evidence replay and blind policy review")
    review_prep.add_argument("--source-package", type=Path, required=True)
    review_prep.add_argument("--question-ids", nargs="+", required=True)
    review_prep.add_argument("--output", type=Path, required=True)
    review_run = commands.add_parser("review", help="run/resume an approved saved-evidence review pipeline")
    review_run.add_argument("--package", type=Path, required=True)
    review_run.add_argument("--approval", type=Path, required=True)
    bundle_prep=commands.add_parser("prepare-review-bundle",help="prepare Brave replacement and automatic review/fix pipeline")
    bundle_prep.add_argument("--source-package",type=Path,required=True)
    bundle_prep.add_argument("--question-ids",nargs="+",required=True)
    bundle_prep.add_argument("--output",type=Path,required=True)
    bundle_run=commands.add_parser("review-bundle",help="run/resume the complete approved replacement/review bundle")
    bundle_run.add_argument("--package",type=Path,required=True)
    bundle_run.add_argument("--approval",type=Path,required=True)
    bundle_run.add_argument("--env-file",type=Path)
    bundle_report=commands.add_parser("finalize-review-bundle",help="verify completed stages and repair a bundle report offline; no model calls")
    bundle_report.add_argument("--package",type=Path,required=True)
    bundle_report.add_argument("--output",type=Path,required=True)
    phase_prep=commands.add_parser("prepare-phase-review",help="prepare only remaining phase work, reusing disjoint completed review bundles")
    phase_prep.add_argument("--source-package",type=Path,required=True)
    phase_prep.add_argument("--completed-bundle",type=Path,action="append",required=True)
    phase_prep.add_argument("--output",type=Path,required=True)
    phase_run=commands.add_parser("phase-review",help="finish the approved phase and automatically generate its readable report")
    phase_run.add_argument("--package",type=Path,required=True)
    phase_run.add_argument("--approval",type=Path,required=True)
    phase_run.add_argument("--env-file",type=Path)
    phase_report=commands.add_parser("phase-report",help="verify and regenerate a completed phase report offline; no calls")
    phase_report.add_argument("--package",type=Path,required=True)
    phase_report.add_argument("--output",type=Path,required=True)
    phase_repair=commands.add_parser("finalize-phase-report",help="audit completed phase stages with a reporting-only repair; no calls")
    phase_repair.add_argument("--package",type=Path,required=True)
    phase_repair.add_argument("--output",type=Path,required=True)
    completions=commands.add_parser("audit-completions",help="audit saved native completions offline; no credentials or calls")
    completions.add_argument("--run-directory",type=Path,required=True)
    completions.add_argument("--output",type=Path,required=True)
    checkpoint_prep=commands.add_parser("prepare-checkpoint",help="prepare next10 and a combined30 report, preserving completed20")
    checkpoint_prep.add_argument("--source-package",type=Path,required=True)
    checkpoint_prep.add_argument("--completed-bundle",type=Path,action="append",required=True)
    checkpoint_prep.add_argument("--old-report",type=Path,required=True)
    checkpoint_prep.add_argument("--question-file",type=Path,required=True)
    checkpoint_prep.add_argument("--output",type=Path,required=True)
    checkpoint_prep.add_argument("--build-identity",type=Path)
    checkpoint_prep.add_argument("--accepted-profile",type=Path)
    checkpoint_run=commands.add_parser("checkpoint",help="run/resume the approved10-question checkpoint and report all30")
    checkpoint_run.add_argument("--package",type=Path,required=True)
    checkpoint_run.add_argument("--approval",type=Path,required=True)
    checkpoint_run.add_argument("--env-file",type=Path)
    batch_prep=commands.add_parser("prepare-batch",help="prepare the next declared frozen questions after an audited parent report")
    batch_prep.add_argument("--parent-report",type=Path,required=True)
    batch_prep.add_argument("--selection",type=Path,required=True)
    batch_prep.add_argument("--count",type=int,required=True)
    batch_prep.add_argument("--ceiling",required=True)
    batch_prep.add_argument("--accepted-profile",type=Path,required=True)
    batch_prep.add_argument("--output",type=Path,required=True)
    batch_prep.add_argument("--build-identity",type=Path)
    recovery_prep=commands.add_parser("prepare-checkpoint-recovery",help="prepare covered saved-evidence stages after completed native grading; no calls")
    recovery_prep.add_argument("--checkpoint",type=Path,required=True)
    recovery_prep.add_argument("--output",type=Path,required=True)
    recovery_run=commands.add_parser("finish-checkpoint-recovery",help="complete originally approved saved-evidence reviews and pooled report; no native rejudging")
    recovery_run.add_argument("--package",type=Path,required=True)
    settlement_prep=commands.add_parser("prepare-settlement",help="prepare publication review of unresolved saved results; no calls")
    settlement_prep.add_argument("--parent-report",type=Path,required=True)
    settlement_prep.add_argument("--output",type=Path,required=True)
    settlement_prep.add_argument("--build-identity",type=Path)
    settlement_prep.add_argument("--accepted-profile",type=Path)
    settlement_prep.add_argument('--experimental-one-per-provider',action='store_true')
    settlement_prep.add_argument('--maximum-tasks',type=int)
    settlement_run=commands.add_parser("settlement",help="run/resume exact approved publication reviews and write a derived report")
    settlement_run.add_argument("--package",type=Path,required=True)
    settlement_run.add_argument("--approval",type=Path,required=True)
    checker_replay=commands.add_parser("replay-publication-checker",help="offline candidate checker diagnostics; no calls or adopted grades")
    checker_replay.add_argument("--source-report",type=Path,required=True)
    checker_replay.add_argument("--output",type=Path,required=True)
    calibration_prep=commands.add_parser("prepare-calibration",help="prepare five saved negative controls; no calls")
    calibration_prep.add_argument("--source-report",type=Path,required=True)
    calibration_prep.add_argument("--selection",type=Path,required=True)
    calibration_prep.add_argument("--output",type=Path,required=True)
    calibration_prep.add_argument("--build-identity",type=Path)
    calibration_run=commands.add_parser("calibration",help="run/resume exact approved diagnostic-only controls")
    calibration_run.add_argument("--package",type=Path,required=True)
    calibration_run.add_argument("--approval",type=Path,required=True)
    adoption_prep=commands.add_parser("prepare-checker-adoption",help="bind saved checker candidates and calibration; no calls")
    adoption_prep.add_argument("--diagnostic",type=Path,required=True)
    adoption_prep.add_argument("--calibration-package",type=Path,required=True)
    adoption_prep.add_argument("--output",type=Path,required=True)
    adoption_run=commands.add_parser("adopt-checker",help="apply exact owner-approved offline candidates")
    adoption_run.add_argument("--package",type=Path,required=True)
    adoption_run.add_argument("--approval",type=Path,required=True)
    review_prep=commands.add_parser("prepare-review-checkpoint",help="bind accepted-profile reviews after a completed checkpoint")
    review_prep.add_argument("--parent-report",type=Path,required=True)
    review_prep.add_argument("--accepted-profile",type=Path,required=True)
    review_prep.add_argument("--output",type=Path,required=True)
    review_prep.add_argument("--allow-inference",action="store_true")
    review_prep.add_argument("--maximum-review-tasks",type=int)
    review_run=commands.add_parser("review-checkpoint",help="run/resume declared accepted-profile reviews and report")
    review_run.add_argument("--package",type=Path,required=True)
    review_run.add_argument("--approval",type=Path,required=True)
    dates=commands.add_parser("replay-date-reading",help="compare opt-in date and review profiles against saved evidence; no calls or adoption")
    from .date_reading import PROFILES as DATE_PROFILES, LATEST_PROFILE as DATE_PROFILE
    dates.add_argument("--date-profile",choices=DATE_PROFILES,default=DATE_PROFILE)
    dates.add_argument("--baseline-date-profile",choices=DATE_PROFILES)
    dates.add_argument("--source-report",type=Path,required=True)
    dates.add_argument("--expected-source-sha256",required=True)
    dates.add_argument("--review-package",type=Path,action="append",required=True)
    dates.add_argument("--publication-package",type=Path,action="append",default=[])
    dates.add_argument("--output",type=Path,required=True)
    date_prep=commands.add_parser("prepare-date-report",help="bind a derived saved-evidence report and explicitly bounded blind third judgments")
    date_prep.add_argument("--native-package",type=Path,action="append",required=True)
    date_prep.add_argument("--source-report",type=Path,required=True)
    date_prep.add_argument("--expected-source-sha256",required=True)
    date_prep.add_argument("--review-package",type=Path,action="append",required=True)
    date_prep.add_argument("--publication-package",type=Path,action="append",default=[])
    date_prep.add_argument("--maximum-new-third",type=int,required=True)
    date_prep.add_argument("--date-profile",choices=DATE_PROFILES,default=DATE_PROFILE)
    date_prep.add_argument("--baseline-date-profile",choices=DATE_PROFILES)
    date_prep.add_argument("--accepted-method",type=Path)
    date_prep.add_argument("--repair-disposition",type=Path)
    date_prep.add_argument("--output",type=Path,required=True)
    date_prep.add_argument('--text-profile',choices=['unicode-spaces-v1'])
    date_prep.add_argument('--reuse-third-package',type=Path)
    date_prep.add_argument('--comparison-report',type=Path)
    date_prep.add_argument('--expected-comparison-sha256')
    date_run=commands.add_parser("date-report",help="run/resume an exactly approved date-profile report rebuild")
    date_run.add_argument("--package",type=Path,required=True)
    date_run.add_argument("--approval",type=Path,required=True)
    quote_replay=commands.add_parser('replay-quote-repair',help='offline all-provider quote repair and broader-review census; no calls or adoption')
    quote_replay.add_argument('--rebuild-package',type=Path,required=True)
    quote_replay.add_argument('--source-report',type=Path,required=True)
    quote_replay.add_argument('--expected-source-sha256',required=True)
    quote_replay.add_argument('--output',type=Path,required=True)
    live_prep=commands.add_parser('prepare-live-profiles',help='prepare exact future batch profile bindings and an unapproved acceptance template; no calls')
    live_prep.add_argument('--source-report',type=Path,required=True)
    live_prep.add_argument('--output',type=Path,required=True)
    live_prep.add_argument('--selection-profile',choices=['publication-policy-route-v1','invalid-own-source-review-v1'],default='publication-policy-route-v1')
    cache_prep=commands.add_parser('prepare-cache-recovery',help='prepare exact saved-work continuation after a proved cache-expiry stop; no calls')
    cache_prep.add_argument('--checkpoint',type=Path,required=True)
    cache_prep.add_argument('--snapshot-reference',type=Path,required=True)
    cache_prep.add_argument('--accepted-profile',type=Path,required=True)
    cache_prep.add_argument('--output',type=Path,required=True)
    cache_prep.add_argument('--build-identity',type=Path)
    quota_prep=commands.add_parser('prepare-quota-repair',help='prepare proven missing Claude thirds from a held checkpoint; no calls')
    quota_prep.add_argument('--source-gate',type=Path,required=True)
    quota_prep.add_argument('--checklist',type=Path,required=True)
    quota_prep.add_argument('--accepted-profile',type=Path,required=True)
    quota_prep.add_argument('--output',type=Path,required=True)
    quota_prep.add_argument('--maximum-tasks',type=int,required=True)
    quota_run=commands.add_parser('quota-repair',help='run exact approved missing thirds and bounded follow-up; await independent verdict')
    quota_run.add_argument('--package',type=Path,required=True)
    quota_run.add_argument('--approval',type=Path,required=True)
    quota_review=commands.add_parser('record-quota-repair-review',help='record an independent exact repaired-gate verdict; no calls')
    quota_review.add_argument('--package',type=Path,required=True)
    quota_review.add_argument('--receipt',type=Path,required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {'status', 'verify', 'check-credentials', 'record-approval'}:
            from . import operator
            if args.command == 'status':
                value = operator.status(args.package)
            elif args.command == 'verify':
                value = operator.verify(args.package)
            elif args.command == 'check-credentials':
                value = operator.credential_presence(args.package, env_file=args.env_file)
            else:
                value = operator.record_approval(args.package, load(args.receipt))
            if (args.command == 'verify' and value.get('verified') is not True
                    or args.command == 'check-credentials' and value.get('all_present') is not True):
                print(json.dumps(value, indent=2, ensure_ascii=False))
                return 1
        elif args.command in {'validate-bank', 'freeze-bank'}:
            from . import question_bank
            value = (question_bank.validate(args.questions) if args.command == 'validate-bank' else
                     question_bank.freeze(args.questions, args.output, phase_counts=args.phase_counts))
        elif args.command in {'prepare-date-capabilities', 'prepare-date-trust'}:
            from .preparation import selected_providers
            cfg = load(args.config)
            providers = selected_providers(cfg, cfg['provider_ids'])
            if args.command == 'prepare-date-capabilities':
                from .provider_dates import prepare as prepare_capabilities
                from .common import records
                questions = cfg.get('questions')
                if questions is None:
                    questions = list(records(cfg['questions_path']))
                result = prepare_capabilities(providers, questions)
                write_private(args.output / 'date-capabilities.json', result)
                value = dict(profile=result['profile'], supported_omissions=result['supported_omissions'], calls=0)
            else:
                from .date_trust import prepare as prepare_trust
                result = prepare_trust(providers, args.output, diagnostic_paths=args.diagnostic or [],
                    source_bindings={str(i): p for i, p in enumerate(args.source)})
                value = dict(profile=result['profile'], package_sha256=digest(result), approved=False, calls=0)
        elif args.command == 'date-diagnostics':
            from .date_diagnostics import inspect as inspect_dates
            if args.output.resolve().is_relative_to(args.compiled_directory.resolve()):
                raise ValueError('diagnostic output would alter the compiled source tree')
            result = inspect_dates(args.compiled_directory, source_package_path=args.source_package,
                source_report_path=args.source_report, thresholds=load(args.thresholds) if args.thresholds else None,
                screening_profile=args.screening_profile)
            write_private(args.output / 'date-diagnostics.json', result)
            value = dict(profile=result['profile'], report=str(args.output / 'date-diagnostics.json'),
                diagnostic_complete=result['diagnostic_complete'], screening_profile=result['screening_profile'], suggestion_only=True, calls=0)
            if not result['diagnostic_complete']:
                print(json.dumps(value, indent=2, ensure_ascii=False))
                return 1
        elif args.command == 'prepare-method':
            from .profile_authority import prepare_method
            result = prepare_method(load(args.config), args.output, mode=args.mode)
            value = dict(profile=result['profile'], package_sha256=digest(result), approved=False, calls=0)
        elif args.command == 'prepare-clean-run':
            from .clean_run import prepare as prepare_clean, _bound_file
            result = prepare_clean(args.template_package, args.selection, args.output, ceiling=args.ceiling,
                accepted_profile=args.accepted_method, archived_reference=load(args.archive_reference) if args.archive_reference is not None else None, mode=args.mode,
                seed_report=args.seed_report,seed_review=args.seed_review,search_provider_ids=args.search_provider_ids,rebind_seed=args.rebind_seed,
                recollection_source=load(args.recollection_source)if args.recollection_source else None,
                verified_prefix=_bound_file(args.verified_prefix)if args.verified_prefix else None,
                **(dict(stage_counts=args.stage_counts)if args.stage_counts is not None else {}))
            value = dict(profile=result['profile'], package_sha256=digest(result), approved=False,
                stage_counts=[s['count'] for s in result['stages']], calls=0)
        elif args.command == 'clean-run':
            from .clean_run import run as run_clean
            fixture_call = None
            if args.fixture_response is not None:
                if load(args.package)['mode'] != 'fixture':
                    raise ValueError('fixture responses cannot be used in live mode')
                response = load(args.fixture_response)
                if response.get('fixture') is not True:
                    raise ValueError('explicit fixture-only response required')
                from copy import deepcopy
                fixture_call = lambda job, folder: deepcopy(response)
            value = run_clean(args.package, load(args.approval), env_file=args.env_file, fixture_call=fixture_call)
        elif args.command == 'record-stage-review':
            from .clean_run import record_stage_review
            value = record_stage_review(args.package, load(args.receipt))
        elif args.command == 'compare-reports':
            from .comparison import compare_reports
            archived_raw = args.archived_report.read_bytes()
            current_raw = args.current_report.read_bytes()
            result = compare_reports(json.loads(archived_raw), json.loads(current_raw))
            result['source_files'] = dict(archived_sha256=digest(archived_raw), current_sha256=digest(current_raw),
                authority='File hashes identify these diagnostic inputs; package authenticity is not established by this command.')
            output = args.output / 'comparison.json'
            write_private(output, result)
            value = dict(profile=result['profile'], scope=result['scope'], comparison=str(output), diagnostic_only=True)
        elif args.command=='prepare-cache-recovery':
            from .cache_recovery import prepare as prepare_cache_recovery
            p=prepare_cache_recovery(args.checkpoint,load(args.snapshot_reference),args.accepted_profile,args.output,
                build_identity=load(args.build_identity)if args.build_identity else None)
            value=dict(package_sha256=digest(p),new_questions=len(p['new_questions']),shared_ceiling_usd=p['shared_ceiling_usd'],new_calls=0)
        elif args.command == 'prepare-live-profiles':
            from .profile_authority import prepare_live
            p=prepare_live(args.source_report,args.output,selection_profile=args.selection_profile)
            value=dict(package_sha256=digest(p),profile=p['profile'],selection_profile=p['profile_bindings']['selection_profile'],approved=False,new_calls=0)
        elif args.command == 'replay-quote-repair':
            from .representation_replay import replay as replay_quotes
            r=replay_quotes(args.rebuild_package,args.source_report,args.output,expected_source_sha256=args.expected_source_sha256)
            value={k:r[k]for k in ('profile','cases','model_calls','adopted','before_counts','after_counts','known_changes','proposed_review_cases','proposed_subscription_tasks')}
        elif args.command == "prepare-date-report":
            from .date_report import prepare as prepare_date_report
            p=prepare_date_report(args.source_report,args.review_package,args.publication_package,args.output,
                expected_source_sha256=args.expected_source_sha256,maximum_new_third=args.maximum_new_third,native_packages=args.native_package,
                text_profile=args.text_profile,reused_third_package=args.reuse_third_package,
                comparison_report=args.comparison_report,expected_comparison_sha256=args.expected_comparison_sha256,
                date_profile=args.date_profile,baseline_date_profile=args.baseline_date_profile,
                accepted_profile=args.accepted_method,repair_disposition=args.repair_disposition)
            value=dict(package_sha256=digest(p),new_third_tasks=len(p['jobs']),date_profile=p['date_profile'],new_pro_calls=0,new_search_calls=0)
        elif args.command == "date-report":
            from .date_report import run as run_date_report
            r=run_date_report(args.package,load(args.approval))
            value=dict(outcome_counts=r['outcome_counts'],report=str(args.package.parent/'report/report.html'))
        elif args.command == "replay-date-reading":
            from .date_replay import replay as replay_dates
            summary=replay_dates(args.source_report,args.review_package,args.publication_package,args.output,
                expected_source_sha256=args.expected_source_sha256,date_profile=args.date_profile,baseline_date_profile=args.baseline_date_profile)
            value={k:summary[k]for k in ('profile','cases','position_totals','changed_positions','outcome_counts','model_calls','adopted')}
        elif args.command == "catalog":
            value = [{"configuration_id": row["configuration_id"], "provider": row["provider"],
                      "optional": row["optional"], "live_tested": False,
                      "fixture_tested": row["verification"]["fixture_tested"],
                      "credential_reference": row["auth"]["secret_ref"]}
                     for row in configuration_catalog(include_optional=True)]
        elif args.command == "prepare":
            config = load(args.config)
            if args.reuse_from:
                if "reuse_snapshot" in config:
                    raise ValueError("choose either an existing reuse snapshot or --reuse-from")
                # Freeze no auth state; the private bundle contains task evidence only.
                from .reuse import freeze
                config["reuse_snapshot"] = freeze(args.reuse_from, args.output.absolute().parent / ".reuse" / args.output.stem)
            if args.fresh_from:
                if "fresh_judging" in config or "reuse_snapshot" in config:
                    raise ValueError("choose one source snapshot or --fresh-from")
                from .fresh import freeze
                config["fresh_judging"] = freeze(args.fresh_from, args.output.absolute().parent / ".fresh" / args.output.stem)
            package = prepare(config, mode=args.mode)
            write_private(args.output, package)
            value = dict(package_sha256=digest(package), scope=package["scope"],
                         reservation_forecast=package["reservation_forecast"], approval="required; no live calls made")
        elif args.command == "starter":
            value = fixtures.starter(args.output, simple=args.simple)
        elif args.command == "prepare-question-workflow":
            from .question_workflow import prepare as prepare_workflow
            p = prepare_workflow(args.questions, load(args.config), args.output,
                mode=args.mode, origin=args.origin, stage_counts=args.stage_counts,
                context=args.context.read_text() if args.context else '')
            value = dict(package_sha256=digest(p), questions=len(p['inputs']),
                mode=p['mode'], approval='required; no calls made',
                additional_subscription_tasks=p['maximum_additional_subscription_tasks'])
        elif args.command == "question-workflow":
            from .question_workflow import run as run_workflow
            value = run_workflow(args.package, load(args.approval), env_file=args.env_file)
        elif args.command == "example":
            package = prepare(fixtures.config(args.output.absolute()), mode="fixture")
            result = run(package)
            value = dict(mode="fixture", outcome_counts=result["outcome_counts"], report=str(args.output.absolute() / "report.html"))
        elif args.command == "run":
            result = run(load(args.package), approval=load(args.approval), env_file=args.env_file, retry_failed=args.retry_failed)
            value = dict(mode=result["mode"], outcome_counts=result["outcome_counts"], package_sha256=result["package_sha256"])
        elif args.command == "replay":
            from .replay import replay as compare
            value = compare(load(args.inputs))
            write_private(args.output, value)
        elif args.command == "redecode":
            from .repair import redecode
            result = redecode(load(args.package), args.output)
            value = dict(mode=result["mode"], outcome_counts=result["outcome_counts"], new_live_calls=0)
        elif args.command == "audit-sample":
            from .audit import sample
            value = sample(args.run_directory, args.output)
        elif args.command == "prepare-review":
            from .review_pipeline import prepare_review
            value = prepare_review(args.source_package, args.question_ids, args.output)
            value = dict(package_sha256=digest(value), question_count=len(value["questions"]),
                         maximum_new_subscription_tasks=value["maximum_new_subscription_tasks"], incremental_api_calls=0)
        elif args.command == "review":
            from .review_pipeline import run_review
            report = run_review(args.package, approval=load(args.approval))
            value = dict(outcome_counts=report["outcome_counts"], review_pipeline=report["review_pipeline"],execution=report["execution"])
        elif args.command=="prepare-review-bundle":
            from .review_bundle import prepare_bundle
            p=prepare_bundle(args.source_package,args.question_ids,args.output)
            value=dict(bundle_sha256=digest(p),question_ids=p['question_ids'],maximum_initial_searches=p['maximum_initial_searches'],maximum_new_pro_positions=p['maximum_new_pro_positions'])
        elif args.command=="review-bundle":
            from .review_bundle import run_bundle
            value=run_bundle(args.package,load(args.approval),env_file=args.env_file)
        elif args.command=="finalize-review-bundle":
            from .review_bundle import finalize_bundle_report
            value=finalize_bundle_report(args.package,args.output)
        elif args.command=="prepare-phase-review":
            from .phase_review import prepare_phase
            p=prepare_phase(args.source_package,args.completed_bundle,args.output)
            value=dict(package_sha256=digest(p),question_ids=p['question_ids'],remaining_question_ids=p['remaining_question_ids'],
                       prior_committed_and_held_usd=p['prior_committed_and_held_usd'],shared_ceiling_usd=p['shared_ceiling_usd'])
        elif args.command=="phase-review":
            from .phase_review import run_phase
            value=run_phase(args.package,load(args.approval),env_file=args.env_file)
        elif args.command=="phase-report":
            from .phase_review import report_phase
            value=report_phase(args.package,args.output)
        elif args.command=="finalize-phase-report":
            from .phase_review import report_phase
            value=report_phase(args.package,args.output,reporting_only=True)
        elif args.command=="audit-completions":
            from .completion_audit import audit_ledger
            value=audit_ledger(args.run_directory,args.output)
            print(json.dumps(value,indent=2))
            return 1 if value['unclassified'] else 0
        elif args.command=="prepare-checkpoint":
            from .checkpoint import prepare_checkpoint
            p=prepare_checkpoint(args.source_package,args.completed_bundle,args.old_report,args.question_file,10,args.output,build_identity=load(args.build_identity)if args.build_identity else None,accepted_profile=args.accepted_profile)
            value=dict(package_sha256=digest(p),question_ids=[q['question_id']for q in p['new_questions']],prior_committed_and_held_usd=p['prior_committed_and_held_usd'],ceiling=p['shared_ceiling_usd'])
        elif args.command=="prepare-batch":
            from .batch import prepare as prepare_batch
            p=prepare_batch(args.parent_report,args.selection,args.count,args.output,ceiling=args.ceiling,
                accepted_profile=args.accepted_profile,build_identity=load(args.build_identity)if args.build_identity else None)
            value=dict(package_sha256=digest(p),parent_questions=len(p['old_questions']),new_questions=len(p['new_questions']),
                total_questions=len(p['old_questions'])+len(p['new_questions']),shared_ceiling_usd=p['shared_ceiling_usd'],
                prior_committed_and_held_usd=p['prior_committed_and_held_usd'],approval_state='requires_separate_owner_receipt')
        elif args.command=="prepare-checkpoint-recovery":
            from .checkpoint_recovery import prepare as prepare_checkpoint_recovery
            p=prepare_checkpoint_recovery(args.checkpoint,args.output)
            value=dict(package_sha256=digest(p),review_tasks=p['review_tasks'],new_search_calls=0,new_pro_calls=0,
                authority='Remaining stages of original approved checkpoint only')
        elif args.command=="finish-checkpoint-recovery":
            from .checkpoint_recovery import run as finish_checkpoint_recovery
            value=finish_checkpoint_recovery(args.package)
        elif args.command=="checkpoint":
            from .checkpoint import run_checkpoint
            value=run_checkpoint(args.package,load(args.approval),env_file=args.env_file)
        elif args.command=="prepare-settlement":
            from .settlement import prepare_settlement
            p=prepare_settlement(args.parent_report,args.output,build_identity=load(args.build_identity)if args.build_identity else None,accepted_profile=args.accepted_profile,
                experimental=args.experimental_one_per_provider,maximum_tasks=args.maximum_tasks)
            value=dict(package_sha256=digest(p),cases=len(p['roster']),subscription_tasks=len(p['jobs']),new_api_calls=0)
        elif args.command=="settlement":
            from .settlement import run_settlement
            r=run_settlement(args.package,load(args.approval))
            value=dict(execution=r['execution'],outcome_counts=r['outcome_counts'],report=str(Path(args.package).parent/'run/report.html'))
        elif args.command=="replay-publication-checker":
            from .checker_replay import replay
            summary=replay(args.source_report,args.output)
            value={k:summary[k] for k in ('candidate_not_adopted','model_calls','cases','candidate_pair_dispositions','additional_candidate_grades')}
        elif args.command=="prepare-calibration":
            from .calibration import prepare as prepare_calibration
            p=prepare_calibration(args.source_report,args.selection,args.output,
                build_identity=load(args.build_identity)if args.build_identity else None)
            value=dict(package_sha256=digest(p),cases=5,subscription_tasks=10,new_api_calls=0)
        elif args.command=="calibration":
            from .calibration import run as run_calibration
            r=run_calibration(args.package,load(args.approval))
            value=dict(execution=r['execution'],calibration_pass=r['calibration_pass'],diagnostic_only=True)
        elif args.command=="prepare-checker-adoption":
            from .adoption import prepare as prepare_adoption
            p=prepare_adoption(args.diagnostic,args.calibration_package,args.output)
            value=dict(package_sha256=digest(p),promoted_results=len(p['promoted_case_ids']),new_model_calls=0)
        elif args.command=="adopt-checker":
            from .adoption import run as run_adoption
            r=run_adoption(args.package,load(args.approval))
            value=dict(outcome_counts=r['outcome_counts'],report=str(args.package.parent/'report/report.html'),new_model_calls=0)
        elif args.command=="prepare-review-checkpoint":
            from .accepted_review import prepare as prepare_review_checkpoint
            p=prepare_review_checkpoint(args.parent_report,args.accepted_profile,args.output,
                allow_inference=args.allow_inference,maximum_tasks=args.maximum_review_tasks)
            value=dict(package_sha256=digest(p),review_tasks=p['review_tasks'],calibration_repeated=False)
        elif args.command=="review-checkpoint":
            from .accepted_review import run as run_review_checkpoint
            r=run_review_checkpoint(args.package,load(args.approval))
            value=dict(outcome_counts=r['outcome_counts'],report=str(args.package.parent/'report/report.html'))
        elif args.command=='prepare-quota-repair':
            from .clean_run import prepare_quota_repair
            p=prepare_quota_repair(args.source_gate,args.checklist,args.output,
                accepted_profile=args.accepted_profile,maximum_tasks=args.maximum_tasks)
            value=dict(package_sha256=digest(p),maximum_followup_tasks=p['maximum_followup_tasks'],new_api_calls=0)
        elif args.command=='quota-repair':
            from .clean_run import run_quota_repair
            value=run_quota_repair(args.package,load(args.approval))
        elif args.command=='record-quota-repair-review':
            from .clean_run import record_quota_repair_review
            value=record_quota_repair_review(args.package,load(args.receipt))
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        # Generic CLI failures must not echo credentials or private response bodies.
        print(f"{type(error).__name__}: operation failed; inspect the private inputs and retained run records", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
