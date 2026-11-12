"""Reuse a sealed owner-accepted profile; calibration observations are not authority."""
from pathlib import Path

from . import adoption, publication
from .common import asset_bindings, code_bindings, digest, load


def bindings(reviewers):
    code=code_bindings();assets=asset_bindings()
    return dict(checker_profile=publication.CHECKER_PROFILE,
        semantic_code={name:code[name]for name in adoption.DECODERS},
        prompt_sha256=assets['publication-review-v1.md'],policies_sha256=assets['policies.json'],reviewers=reviewers)


def _read_legacy(package_path,reviewers):
    path=Path(package_path).resolve();root=path.parent;p=load(path)
    approval=load(root/'approval.json');closure=load(root/'closure.json')
    report_path=root/'report/report.json';report=load(report_path)
    if (p['profile']!=adoption.PROFILE or Path(p['output_directory']).resolve()!=root
            or p['profile_bindings']!=bindings(reviewers) or p['checker_profile']!=publication.CHECKER_PROFILE
            or approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)
            or not approval.get('approved_by')or not approval.get('approved_at')
            or(not p['calibration_pass']and approval.get('accept_failed_calibration')is not True)
            or closure!=adoption._closure(p,digest(report_path.read_bytes()))
            or report['package_sha256']!=digest(p)
            or report['checker_acceptance']['approval_sha256']!=digest(approval)):
        raise ValueError('accepted review profile authority differs')
    cp=Path(p['calibration_package_path'])
    if (digest(cp.read_bytes())!=p['calibration_package_sha256']
            or digest((cp.parent/'run/calibration.json').read_bytes())!=p['calibration_sha256']):
        raise ValueError('accepted calibration evidence changed')
    return dict(package_path=str(path),package_sha256=digest(p),approval_sha256=digest(approval),
        closure_sha256=digest(closure),report_sha256=closure['report_sha256'],
        calibration_sha256=p['calibration_sha256'],calibration_pass=p['calibration_pass'],
        one_time_owner_exception=not p['calibration_pass'],profile_bindings=p['profile_bindings'])


LIVE_PROFILE = 'accepted-live-profiles-v1'
POLICY_SELECTION = 'publication-policy-route-v1'
LIVE_DECODERS = (*adoption.DECODERS, 'date_reading.py', 'policy.py', 'review.py',
    'review_pipeline.py', 'runner.py', 'publication_reading.py', 'settlement.py',
    'subscription_health.py','completion_audit.py')


def live_bindings(reviewers, *, selection_profile=POLICY_SELECTION, date_profile=None):
    from .date_reading import LATEST_PROFILE, PROFILES
    date_profile = LATEST_PROFILE if date_profile is None else date_profile
    if date_profile not in PROFILES:raise ValueError("unknown date-reading profile")
    from .representation import SPACE_PROFILE
    if selection_profile not in (POLICY_SELECTION, publication.SELECTION_PROFILE):
        raise ValueError('unsupported live review selection profile')
    code=code_bindings()
    semantic_names=sorted(set(LIVE_DECODERS)|{name for name in code if name.startswith('_method/')})
    return dict(checker_profile=publication.READING_PROFILE,date_profile=date_profile,
        text_profile=SPACE_PROFILE,selection_profile=selection_profile,
        semantic_code={name:code[name]for name in semantic_names},
        assets=asset_bindings(),reviewers=reviewers)


def profiles(authority):
    """Legacy acceptance selects no new date, text or review route."""
    b=authority.get('profile_bindings',{})
    return {k:b[k]for k in ('checker_profile','date_profile','text_profile','selection_profile','judge_input_profile') if k in b} if 'date_profile'in b else {}


def prepare_live(source_report,directory,*,selection_profile=POLICY_SELECTION):
    from . import settlement
    from .common import now,write_private
    source=Path(source_report).resolve();root=Path(directory).resolve()
    if root.is_relative_to(source.parent.parent)or source.is_relative_to(root):
        raise ValueError('live authority must preserve its source report')
    report,_,native=settlement.load_parent(source)
    b=live_bindings(native['configuration']['reviewers'],selection_profile=selection_profile)
    if not source_profiles_match(report,b):
        raise ValueError('live authority requires the adopted source profiles')
    p=dict(profile=LIVE_PROFILE,created_at=now(),output_directory=str(root),
        source_report_path=str(source),source_report_sha256=digest(source.read_bytes()),profile_bindings=b,
        scope='Future batches may use these exact profiles only with separate exact batch approval. No dispatch authorized.')
    write_private(root/'package.json',p)
    write_private(root/'approval-template.json',dict(approved=False,package_sha256=digest(p),approved_by='',approved_at='',
        accept_live_profiles=False,scope=p['scope']))
    return p


def read(package_path,reviewers):
    path=Path(package_path).resolve();p=load(path)
    if p.get('profile') == METHOD_PROFILE:
        return read_method(path, reviewers)
    if p.get('profile')!=LIVE_PROFILE:return _read_legacy(path,reviewers)
    return read_live(path,reviewers)


def read_live(package_path,reviewers):
    path=Path(package_path).resolve();p=load(path)
    from . import settlement
    root=path.parent;approval=load(root/'approval.json');source=Path(p['source_report_path']).resolve()
    b=p['profile_bindings']
    if (p['profile']!=LIVE_PROFILE or Path(p['output_directory']).resolve()!=root
            or root.is_relative_to(source.parent.parent)or source.is_relative_to(root)
            or b!=live_bindings(reviewers,selection_profile=b.get('selection_profile'))
            or digest(source.read_bytes())!=p['source_report_sha256']
            or approval.get('approved')is not True or approval.get('accept_live_profiles')is not True
            or approval.get('package_sha256')!=digest(p)or not approval.get('approved_by')or not approval.get('approved_at')):
        raise ValueError('accepted live profile authority differs')
    report,_,native=settlement.load_parent(source)
    if (native['configuration']['reviewers']!=reviewers
            or not source_profiles_match(report,b)):
        raise ValueError('accepted live source profiles differ')
    return dict(package_path=str(path),package_sha256=digest(p),approval_sha256=digest(approval),
        source_report_sha256=p['source_report_sha256'],profile_bindings=b)


def verify_native(config, *, mode=None):
    authority=config.get('live_profile_authority')
    if authority is not None:
        if not profiles(authority)or read(authority['package_path'],config['reviewers'])!=authority:
            raise ValueError('native live profile authority differs')
        if authority.get('profile') == METHOD_PROFILE and method_options(config) != authority['method_options']:
            raise ValueError('native selected method options differ from owner acceptance')
        if authority.get('profile') == METHOD_PROFILE and mode is not None and authority.get('mode') != mode:
            raise ValueError('method owner acceptance mode differs from execution mode')
    elif config.get('judge_input_profile'):
        raise ValueError('new judge method requires exact independent owner acceptance')
    return profiles(authority)if authority is not None else {}


def source_profiles_match(report,binding):
    active=report.get('active_review_profile',{})
    active=active.get('profile_bindings',active)
    return all(active.get(k)==binding[k]for k in ('checker_profile','date_profile','text_profile'))


METHOD_PROFILE = 'accepted-independent-method-v1'
_METHOD_OPTIONS = ('judge_input_profile', 'date_trust_package', 'provider_request_profiles',
                   'provider_projection_profiles', 'provider_overrides', 'date_reading_profile',
                   'provider_date_profiles')


def method_options(config):
    """Method choices are distinct from question scope, budget and execution approval."""
    from copy import deepcopy
    return {key: deepcopy(config[key]) for key in _METHOD_OPTIONS if key in config}


def method_bindings(config, provider_ids, *, selection_profile=POLICY_SELECTION):
    from copy import deepcopy
    config = deepcopy(config)
    from .blind_input import LIVE_PROFILE as BLIND_PROFILE
    from .date_trust import read as read_trust
    from .preparation import selected_providers
    if config.get('judge_input_profile') != BLIND_PROFILE:
        raise ValueError('independent method acceptance requires the selected blind method')
    providers = selected_providers(config, provider_ids)
    from .request_profiles import validate_start_profiles
    validate_start_profiles(providers, config.get('provider_date_profiles', {}))
    trust = read_trust(config['date_trust_package'], providers)
    result = live_bindings(config['reviewers'], selection_profile=selection_profile,
        date_profile=config.get('date_reading_profile'))
    result.update(judge_input_profile=BLIND_PROFILE, semantic_code=code_bindings(),
        provider_ids=list(provider_ids), provider_configurations={p['configuration_id']: digest(p) for p in providers},
        date_trust=trust, method_options=method_options(config))
    return result


def prepare_method(config, directory, *, mode='live', selection_profile=POLICY_SELECTION):
    """Freeze a new method without an earlier benchmark or inherited approval."""
    from .common import now, write_private
    if mode not in {'live', 'fixture'}:
        raise ValueError('independent method mode differs')
    root = Path(directory).resolve()
    binding = method_bindings(config, config['provider_ids'], selection_profile=selection_profile)
    p = dict(profile=METHOD_PROFILE, mode=mode, created_at=now(), output_directory=str(root),
        profile_bindings=binding, method_options=method_options(config),
        scope='Exact new method acceptance only; no collection, model dispatch or spending approval.')
    write_private(root/'package.json', p)
    write_private(root/'approval-template.json', dict(approved=False, package_sha256=digest(p),
        accept_method=False, approved_by='', approved_at='', scope=p['scope']))
    return p


def read_method(package_path, reviewers):
    path = Path(package_path).resolve(); p = load(path)
    receipt = load(path.parent/'approval.json')
    binding = p['profile_bindings']
    config = dict(p['method_options'], reviewers=reviewers)
    expected = method_bindings(config, binding['provider_ids'], selection_profile=binding['selection_profile'])
    if (p.get('profile') != METHOD_PROFILE or p.get('mode') not in {'live', 'fixture'}
            or p.get('output_directory') != str(path.parent) or binding != expected
            or receipt.get('approved') is not True or receipt.get('accept_method') is not True
            or receipt.get('package_sha256') != digest(p)
            or not receipt.get('approved_by') or not receipt.get('approved_at')):
        raise ValueError('independent method owner acceptance or bindings differ')
    return dict(profile=METHOD_PROFILE, mode=p['mode'], package_path=str(path), package_sha256=digest(p),
        approval_sha256=digest(receipt), profile_bindings=binding, method_options=p['method_options'])


def verify_method_config(config, provider_ids, *, mode):
    authority = config.get('live_profile_authority')
    if not isinstance(authority, dict) or authority.get('profile') != METHOD_PROFILE:
        raise ValueError('new method requires explicit independent owner acceptance')
    if (read_method(authority['package_path'], config['reviewers']) != authority
            or authority['mode'] != mode or authority['method_options'] != method_options(config)
            or authority['profile_bindings']['provider_ids'] != list(provider_ids)):
        raise ValueError('selected method/configuration/mode differs from owner acceptance')
    return authority


def verify_case_method(authority, compiled, provider):
    """Bind every new-method case to its admitted provider and exact input profile.

    The enclosing package verifies the owner receipt once. These per-case checks
    do not reread the entire approval/evidence chain for every result.
    """
    if not authority or authority.get('profile') != METHOD_PROFILE:
        if compiled.get('judge_input_profile') or provider.get('judge_input_profile') or provider.get('date_trust'):
            raise ValueError('selected case method cannot downgrade to legacy or missing authority')
        return
    from .date_trust import configuration_sha256, eligible_fields
    binding = authority['profile_bindings']
    cid = provider['configuration_id']
    trust = provider.get('date_trust', {})
    accepted = binding['date_trust']
    if (binding['provider_configurations'].get(cid) != configuration_sha256(provider)
            or provider.get('judge_input_profile') != binding['judge_input_profile']
            or compiled.get('judge_input_profile') != binding['judge_input_profile']
            or trust.get('package_sha256') != accepted['package_sha256']
            or trust.get('receipt_sha256') != accepted['receipt_sha256']
            or eligible_fields(provider) != accepted['selections'].get(cid, {}).get('eligible_fields')):
        raise ValueError('case input method/provider/date trust differs from owner acceptance')
