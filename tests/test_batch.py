from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import batch,checkpoint,fixtures,profile_authority,settlement,review_pipeline
from openweb_news.common import digest,encoded,load,write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.ledger import Ledger
from openweb_news.scheduling import Admission,AdmissionBlocked
from . import test_checkpoint as checkpoint_tests


class BatchTests(unittest.TestCase):
    def test_provider_cell_partition_rejects_missing_duplicate_and_changed_question(self):
        with TemporaryDirectory()as temp:
            root=Path(temp)
            _,_,native,_,_,_,_=self.setup_parent(root,n=1,total=1)
            component=checkpoint.completed_review(root/'parent-review/package.json')
            parts=[]
            for provider in native['providers']:
                cid=provider['configuration_id'];binding,p,r,cases=deepcopy(component)
                r['outcomes']=[o for o in r['outcomes']if o['configuration_id']==cid]
                ids={o['case_id']for o in r['outcomes']}
                p['source_collection']=[c for c in p['source_collection']if c['configuration_id']==cid]
                r['review_pipeline']['records']=[row for row in r['review_pipeline']['records']if row['case_id']in ids]
                parts.append((binding,p,r,{k:v for k,v in cases.items()if k in ids}))
            identity=dict(recollection_source={'synthetic':True})
            costs={k:'0'for k in checkpoint.COST_KEYS}
            result=checkpoint.combine(parts,identity,native,costs)
            self.assertEqual(len(result['questions']),1)
            self.assertEqual(len(result['collection']),len(native['providers']))
            with self.assertRaisesRegex(ValueError,'scope differs'):
                checkpoint.combine(parts[:-1],identity,native,costs)
            duplicate=deepcopy(parts)
            duplicate[-1][1]['source_collection']+=duplicate[0][1]['source_collection']
            with self.assertRaisesRegex(ValueError,'duplicate collection cell'):
                checkpoint.combine(duplicate,identity,native,costs)
            changed=deepcopy(parts)
            changed[-1][1]['questions'][0]['provider_query']='Different synthetic question'
            with self.assertRaisesRegex(ValueError,'overlapping report components'):
                checkpoint.combine(changed,identity,native,costs)

    def setup_parent(self,root,n=2,total=202):
        base=fixtures.config(root/'unused')['questions'][0]
        order=[dict(deepcopy(base),question_id='synthetic-batch-'+str(i))for i in range(total)]
        selection=root/'admitted/selection.json';bank=selection.parent/'bank.jsonl'
        write_private(bank,b''.join(encoded(q)for q in order))
        phase=selection.parent/'phase-1-questions.jsonl';write_private(phase,b''.join(encoded(q)for q in order))
        write_private(selection,dict(bank_sha256=digest(bank.read_bytes()),question_order=[q['question_id']for q in order],
            phases=[dict(phase=1,question_ids=[q['question_id']for q in order],questions_sha256=digest(order))]))
        write_private(selection.parent/'VERIFICATION-MANIFEST.json',dict(files=[dict(path=p.name,sha256=digest(p.read_bytes()))for p in(bank,phase,selection)]))
        cfg=checkpoint_tests.CheckpointTests().cfg(root/'parent-native/run');cfg['questions']=order[:n]
        native=prepare(cfg,mode='fixture');write_private(root/'parent-native/package.json',native);run(native)
        child=review_pipeline.prepare_review(root/'parent-native/package.json',[q['question_id']for q in order[:n]],root/'parent-review')
        approval=dict(approved=True,package_sha256=digest(child),approved_by='fixture',approved_at='fixture')
        write_private(root/'parent-review/approval.json',approval)
        review_pipeline.run_review(root/'parent-review/package.json',approval=approval)
        component=checkpoint.completed_review(root/'parent-review/package.json')
        costs={k:'0'for k in checkpoint.COST_KEYS};costs['components']=[]
        parent=checkpoint.combine([component],{},native,costs)
        authority=dict(package_path=str(root/'accepted/package.json'),approval_sha256='accepted-owner',profile_bindings=profile_authority.bindings(native['configuration']['reviewers']))
        parent['checker_acceptance']=dict(approval_sha256=authority['approval_sha256'])
        cid=parent['outcomes'][0]['case_id'];parent['outcomes'][0]['grade']=None
        component[3][cid]['compiled']['input']['publication_estimate_applicability']=dict(field_path='synthetic-date',source_bindings={})
        terminal=dict(case_id=cid,configuration_id=parent['outcomes'][0]['configuration_id'],accepted=False,
            grade=None,terminal=True,checker_profile=authority['profile_bindings']['checker_profile'],
            terminal_key=settlement.identity(component[3][cid]['compiled'],native['configuration']['reviewers'],authority=authority))
        parent['publication_review']=dict(profile='synthetic-publication-profile',records=[terminal])
        source=root/'parent/report/report.json';write_private(source,parent)
        return parent,component[3],native,source,selection,authority,order

    def test_legacy_profile_preserves_explicit_method_options(self):
        with TemporaryDirectory()as temp:
            root=Path(temp)
            cfg=checkpoint_tests.CheckpointTests().cfg(root/'unused')
            original=prepare(cfg,mode='fixture')
            provider=original['providers'][0]
            cfg['provider_overrides']={provider['configuration_id']:dict(secret_ref=provider['auth']['secret_ref'])}
            template=prepare(cfg,mode='fixture')
            authority=dict(profile=profile_authority.LIVE_PROFILE,package_path='synthetic-legacy',
                profile_bindings=profile_authority.live_bindings(cfg['reviewers']))
            with patch.object(profile_authority,'read',return_value=authority):
                selected,native=batch.native_plan(template,cfg['questions'][:1],root/'next','100','0',authority=authority,mode='fixture')
            self.assertEqual(selected['provider_overrides'],cfg['provider_overrides'])
            self.assertEqual(native['providers'],template['providers'])

    def test_declared_batch_sizes_and_frozen_parent_prefix(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);parent,cases,native,source,selection,authority,order=self.setup_parent(root)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),patch.object(profile_authority,'read',return_value=authority):
                for k in (10,40,130):
                    p=batch.prepare(source,selection,k,root/('next-'+str(k)),ceiling='600',accepted_profile=authority['package_path'])
                    self.assertEqual(p['new_questions'],order[2:2+k])
                    self.assertEqual(p['accepted_review_task_bound'],2*(1+k*2*10))
                    batch.verify(root/('next-'+str(k))/'checkpoint.json')
                    self.assertEqual(load(root/('next-'+str(k))/'approval-template.json')['decision'],'NOT_APPROVED')
                    self.assertTrue((root/('next-'+str(k))/'APPROVAL-REVIEW.md').read_text().startswith('# Proposed next batch:'))
                for bad in (0,-1,True,201):
                    output=root/('rejected-'+str(bad))
                    with self.assertRaises(ValueError):batch.prepare(source,selection,bad,output,ceiling='600',accepted_profile=authority['package_path'])
                    self.assertFalse(output.exists())
                gap=deepcopy(parent);gap['questions']=[order[0],order[2]]
                with patch.object(settlement,'load_parent',return_value=(gap,cases,native)):
                    with self.assertRaises(ValueError):batch.prepare(source,selection,40,root/'gap',ceiling='600',accepted_profile=authority['package_path'])
                # Historical parent display order is immutable; its membership is
                # exactly the prefix, while all NEW dispatch uses frozen order.
                reordered=deepcopy(parent);reordered['questions'].reverse()
                self.assertEqual(batch.next_questions(reordered,order,40),order[2:42])
                p=load(root/'next-40/checkpoint.json');p['new_questions'].reverse();write_private(root/'next-40/checkpoint.json',p,replace=True)
                with self.assertRaises(ValueError):batch.verify(root/'next-40/checkpoint.json')

    def test_pipeline_combines_parent_and_batch_preserving_authority_and_resume(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();parent,cases,native,source,selection,authority,order=self.setup_parent(root)
            original_loader=settlement.load_parent;original_plan=batch.native_plan
            def loader(path,**kwargs):return(parent,cases,native)if Path(path).resolve()==source else original_loader(path,**kwargs)
            def fixture_plan(*a,**kw):
                cfg,_=original_plan(*a,**kw);return cfg,prepare(cfg,mode='fixture')
            with patch.object(settlement,'load_parent',side_effect=loader),patch.object(profile_authority,'read',return_value=authority),patch.object(batch,'native_plan',side_effect=fixture_plan):
                p=batch.prepare(source,selection,10,root/'batch',ceiling='600',accepted_profile=authority['package_path'])
                child=load(root/'batch/native/package.json');run(child)
                approval=dict(decision='approved',package_sha256=digest(p),approved_by='fixture-owner',approved_at='fixture-time')
                before=source.read_bytes()
                def finish(destination,p,receipt):
                    self.assertTrue((destination/'report/report.html').exists())
                    return dict(state='completed',report=str(destination/'report/report.html'))
                with patch.object(checkpoint,'_finish_accepted',side_effect=finish),patch.object(checkpoint,'run',side_effect=AssertionError('native redispatch')):
                    checkpoint.run_checkpoint(root/'batch/checkpoint.json',approval)
                    report,_,_=settlement.load_parent(root/'batch/report/report.json')
                    self.assertEqual(len(report['questions']),12)
                    self.assertEqual(report['outcomes'][:len(parent['outcomes'])],parent['outcomes'])
                    self.assertEqual(report['checker_acceptance'],parent['checker_acceptance'])
                    self.assertEqual(report['active_review_profile'],authority)
                    with patch('openweb_news.publication.eligibility',return_value={'synthetic-route':True}):
                        self.assertEqual(settlement.selected(report,{**cases},native['configuration']['reviewers'],authority=authority),[])
                    self.assertEqual(report['phase_costs']['conservative_total_usd'],'0')
                    first=(root/'batch/report/report.json').read_bytes()
                    checkpoint.run_checkpoint(root/'batch/checkpoint.json',approval)
                    self.assertEqual((root/'batch/report/report.json').read_bytes(),first)
                    self.assertEqual(source.read_bytes(),before)

    def test_cost_carry_and_budget_rejection(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);parent,cases,native,source,selection,authority,_=self.setup_parent(root)
            parent['phase_costs'].update({k:'12.5'for k in checkpoint.COST_KEYS})
            write_private(source,parent,replace=True)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),patch.object(profile_authority,'read',return_value=authority):
                p=batch.prepare(source,selection,40,root/'batch',ceiling='600',accepted_profile=authority['package_path'])
                self.assertEqual(load(root/'batch/native/package.json')['configuration']['execution_policy']['spending_ceiling_usd'],'587.5')
                self.assertEqual(p['cost_forecast']['cumulative_observed_projection_usd'],'12.5')
                child_cost={k:'3.75'for k in checkpoint.COST_KEYS}
                with patch('openweb_news.accounting.report_costs',return_value=child_cost):
                    combined=batch.combined_costs(p,load(root/'batch/native/package.json'))
                self.assertTrue(all(combined[k]=='16.25'for k in checkpoint.COST_KEYS))
                self.assertEqual(len(combined['components']),1)
                planned=load(root/'batch/native/package.json');sent=[]
                with Ledger(root/'budget-ledger',{}).locked()as ledger:
                    admission=Admission(ledger,planned['configuration']['execution_policy'],package=planned)
                    ledger.execute('first',dict(kind='model',reservation_usd='587.4'),lambda _:sent.append('first')or dict(transport_success=True),before_start=admission.reserve)
                    with self.assertRaises(AdmissionBlocked):
                        ledger.execute('over-limit',dict(kind='model',reservation_usd='0.2'),lambda _:sent.append('over-limit'),before_start=admission.reserve)
                    self.assertEqual(sent,['first'])
                    resumed=Admission(ledger,planned['configuration']['execution_policy'],package=planned)
                    self.assertEqual(str(resumed.total),'587.4')
                cost_native=deepcopy(planned);cost_native['configuration']['run_directory']=str(root/'budget-ledger')
                actual=batch.combined_costs(p,cost_native)
                self.assertEqual(actual['conservative_total_usd'],'599.9')
                self.assertEqual(actual['other_conservative_holds_and_allowances_usd'],'599.9')
                self.assertEqual(actual['published_completed_pro_usd'],'12.5')
                self.assertEqual(len(actual['components']),1)
                with self.assertRaises(ValueError):batch.prepare(source,selection,40,root/'too-small',ceiling='12.5',accepted_profile=authority['package_path'])
                self.assertFalse((root/'too-small').exists())
