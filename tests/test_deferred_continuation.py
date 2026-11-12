from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openweb_news import checkpoint,clean_run,fixtures,profile_authority
from openweb_news.common import digest,load,write_private
from . import test_lean_checkpoints


class DeferredContinuationTests(unittest.TestCase):
    def test_only_declared_cells_may_be_deferred(self):
        questions=fixtures.config(Path('/unused'))['questions'][:2]
        q0,q1=[q['question_id'] for q in questions]
        providers=[dict(configuration_id=c) for c in ('first','later')]
        native=dict(providers=providers,questions=[questions[1]],configuration=dict(
            search_provider_ids=['first'],coverage_target=.9,tie_order=['first','later']))
        rows=[dict(configuration_id=c,question_id=q,status='success_empty') for c,q in [('first',q0),('later',q0),('first',q1)]]
        part=dict(questions=questions,source_collection=rows,source_method='synthetic')
        report=dict(outcomes=[],review_pipeline=dict(records=[]))
        component=({},part,report,{})
        result=checkpoint.combine([component],{},native,{})
        self.assertEqual(result['deferred_collection'],[dict(configuration_id='later',question_id=q1)])
        self.assertEqual(result['scores']['availability']['table']['later'][q1],'not_collected')
        bad=deepcopy(component);bad[1]['source_collection'].pop(0)
        with self.assertRaisesRegex(ValueError,'declared deferred'):
            checkpoint.combine([bad],{},native,{})
        unexpected=deepcopy(component);unexpected[1]['source_collection'].append(dict(configuration_id='later',question_id=q1,status='success_empty'))
        with self.assertRaisesRegex(ValueError,'declared deferred'):
            checkpoint.combine([unexpected],{},native,{})

    def test_lean_subset_preserves_parent_and_resume_does_not_resend(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,old,_,_=test_lean_checkpoints.LeanCheckpointTests().setup_prefix(root)
            subset=[old['accepted_profile']['profile_bindings']['provider_ids'][0]]
            out=root/'deferred'
            p=clean_run.prepare(root/'template.json',old['selection_path'],out,ceiling='100',
                accepted_profile=old['accepted_profile']['package_path'],archived_reference=old['archived_reference'],
                stage_counts=(1,),mode='fixture',verified_prefix=old['verified_seed'],search_provider_ids=subset)
            self.assertEqual(p['stages'][0]['native_prototype']['configuration']['search_provider_ids'],subset)
            self.assertEqual({r['configuration_id'] for r in p['stages'][0]['native_prototype']['search_requests']},set(subset))
            receipt=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
            result=clean_run.run(out/'clean-run.json',receipt)
            self.assertEqual(result['state'],'awaiting_stage_review')
            before=load(load(old['verified_seed']['path'])['gate']['path'])
            original=load(before['report']['path']);final=load(result['evidence']['report']['path'])
            by_id={o['case_id']:o for o in final['outcomes']}
            self.assertTrue(all(by_id[o['case_id']]==o for o in original['outcomes']))
            self.assertEqual(len(final['deferred_collection']),1)
            attempts=out/'stage-1/native/run/attempts.jsonl';saved=attempts.read_bytes()
            again=clean_run.run(out/'clean-run.json',receipt)
            self.assertEqual(again['state'],'awaiting_stage_review');self.assertEqual(attempts.read_bytes(),saved)

    def test_reporting_transition_is_explicit_seed_bound_and_science_unchanged(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();_,old,_,_=test_lean_checkpoints.LeanCheckpointTests().setup_prefix(root)
            ref=old['verified_seed'];prefix=load(ref['path']);saved=Path(ref['path']).read_bytes()
            original=old['code'];changed=dict(original,**{'phase_report.py':'synthetic-new-report','checkpoint.py':'synthetic-deferred-cells'})
            cfg=load(root/'old/stage-1/native/config.json')
            with patch('openweb_news.profile_authority.code_bindings',return_value=changed), \
                    patch('openweb_news.clean_run.code_bindings',return_value=changed), \
                    patch('openweb_news.settlement.code_bindings',return_value=changed):
                method=profile_authority.prepare_method(cfg,root/'new-method',mode='fixture')
                write_private(root/'new-method/approval.json',dict(approved=True,accept_method=True,package_sha256=digest(method),approved_by='synthetic owner',approved_at='fixture'))
                authority=profile_authority.read(root/'new-method/package.json',cfg['reviewers'])
                with self.assertRaises(ValueError):clean_run._verified_prefix(ref,authority)
                transition=clean_run._seed_transition(ref,authority)
                _,evidence=clean_run._verified_prefix(ref,authority,transition=transition)
                self.assertEqual(evidence[2]['configuration']['live_profile_authority'],authority)
                self.assertEqual(Path(ref['path']).read_bytes(),saved)
                bad=deepcopy(transition);bad['changed_code_files'].append('judging.py')
                with self.assertRaises(ValueError):clean_run._verified_prefix(ref,authority,transition=bad)
                write_private(root/'other-prefix.json',prefix)
                with self.assertRaises(ValueError):clean_run._verified_prefix(clean_run._bound_file(root/'other-prefix.json'),authority,transition=transition)
                altered=deepcopy(authority);altered['profile_bindings']['semantic_code']['judging.py']='forbidden'
                with self.assertRaises(ValueError):clean_run._seed_transition(ref,altered)
                # Even the explicit transition cannot relax the existing quota-rebind allowlist.
                with self.assertRaises(ValueError):clean_run._same_choices(old['accepted_profile'],authority)
                source_receipt=Path(old['accepted_profile']['package_path']).parent/'approval.json';raw=source_receipt.read_bytes();source_receipt.write_bytes(raw+b' ')
                with self.assertRaises(ValueError):clean_run._verified_prefix(ref,authority,transition=transition)
                source_receipt.write_bytes(raw)

    def test_archive_may_be_shorter_but_must_match_frozen_prefix(self):
        order=fixtures.config(Path('/unused'))['questions']
        self.assertEqual(set(clean_run._archive_prefix(dict(questions=order[:2]),order)),{q['question_id'] for q in order[:2]})
        for bad in [order[1:3],order[:1]*2,[dict(order[0],provider_query='changed')]]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                clean_run._archive_prefix(dict(questions=bad),order)
