from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import checkpoint,fixtures,review_pipeline
from openweb_news.common import digest,encoded,load,write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.recovery import sibling_rule


class CheckpointTests(unittest.TestCase):
    def cfg(self,path):
        cfg=fixtures.config(path)
        cfg['execution_policy']['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in cfg['execution_policy']['scheduling']['accounts'].items()}
        return cfg

    def test_saved_invalid_pro_envelopes_and_missing_votes_replay(self):
        original=fixtures.model
        for kind in ('malformed','scalar','usage','model','blocked','extra_candidates','parts','incomplete'):
            with self.subTest(kind=kind),TemporaryDirectory()as temp:
                root=Path(temp);cfg=self.cfg(root/'native');cfg['questions']=cfg['questions'][:1]
                def model(role,compiled,folder):
                    raw=original(role,compiled,folder)
                    if role!='pro':return raw
                    if kind=='malformed':raw['raw_text']='broken'
                    elif kind=='scalar':raw['raw_text']='42'
                    else:
                        import json
                        data=json.loads(raw['raw_text'])
                        if kind=='usage':data['usageMetadata']=42
                        if kind=='model':data['modelVersion']='wrong'
                        if kind=='blocked':data['promptFeedback']={'blockReason':'SAFETY'}
                        if kind=='extra_candidates':data['candidates']*=2
                        if kind=='parts':data['candidates'][0]['content']['parts']=[{'functionCall':{}}]
                        if kind=='incomplete':data['candidates'][0]['finishReason']='MAX_TOKENS'
                        raw['raw_text']=encoded(data).decode()
                    return raw
                source=prepare(cfg,mode='fixture');write_private(root/'source.json',source)
                with patch('openweb_news.fixtures.model',side_effect=model):native=run(source)
                review=review_pipeline.prepare_review(root/'source.json',[q['question_id']for q in source['questions']],root/'review')
                cases=review_pipeline.verify(review,root/'review')
                for cid,case in cases.items():
                    _,outcome=review_pipeline.decode(case,review['text_profile'])
                    old=next(o for o in native['outcomes']if o['case_id']==cid)
                    self.assertEqual(outcome['grade'],old['grade']);self.assertEqual(outcome['states'],old['states'])

    def test_zero_job_review_closes_without_a_ledger_and_pools_exact_scores(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);cfg=self.cfg(root/'native');cfg['questions']=cfg['questions'][:1]
            source=prepare(cfg,mode='fixture');write_private(root/'source.json',source);native=run(source)
            p=review_pipeline.prepare_review(root/'source.json',[q['question_id']for q in source['questions']],root/'review')
            self.assertEqual(p['jobs'],[])
            p['mode']='fixture';write_private(root/'review/package.json',p,replace=True)
            write_private(root/'review/approval.json',dict(approved=True,package_sha256=digest(p)))
            r=review_pipeline.run_review(root/'review/package.json',call=lambda *_:self.fail('zero-job review dispatched'))
            self.assertFalse((root/'review/run/attempts.jsonl').exists())
            component=checkpoint.completed_review(root/'review/package.json')
            combined=checkpoint.combine([component],{'fixture':True},source,{})
            import json
            self.assertEqual(json.loads(json.dumps(combined['scores'])),json.loads(json.dumps(r['scores'])))
            self.assertEqual(combined['outcome_counts'],r['outcome_counts'])
            from openweb_news.returned_fields import analyze
            fields=analyze(combined)
            self.assertEqual(fields['status'],'complete')
            self.assertEqual(fields['measured_results'],len(combined['outcomes']))
            self.assertEqual(fields['unmeasured_outcomes'],0)
            self.assertEqual(combined['collection'],r['collection'])

            with self.assertRaises(ValueError):checkpoint.combine([component,component],{},source,{})

    def test_completed_native_is_not_rewritten_when_review_resumes(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);cfg=self.cfg(root/'native/run');native=prepare(cfg,mode='fixture');report=run(native)
            p=dict(new_questions=native['questions'],old_costs={},profile=checkpoint.PROFILE)
            write_private(root/'checkpoint.json',p)
            child=dict(created_at='then',run_directory=str(root/'review/run'),fixture='same')
            write_private(root/'review/package.json',child)
            def prepare_child(source,ids,destination):
                c=dict(child,created_at='now',run_directory=str(Path(destination)/'run'));write_private(Path(destination)/'package.json',c);return c
            held=dict(execution=dict(state='completed_with_holds',held_tasks=['review-task']))
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='fixture-owner',approved_at='fixture-time')
            before=(root/'native/run/report.json').read_bytes()
            with patch.object(checkpoint,'verify_checkpoint',return_value=(p,native,[])),patch.object(checkpoint,'run',side_effect=AssertionError('completed native stage rerun')),patch.object(checkpoint,'inspect_ledger',return_value=dict(unclassified=[])),patch.object(checkpoint,'prepare_review',side_effect=prepare_child),patch.object(checkpoint,'run_review',return_value=held):
                result=checkpoint._run_checkpoint(root/'checkpoint.json',approval)
                again=checkpoint._run_checkpoint(root/'checkpoint.json',approval)
                self.assertEqual(result,again)
            self.assertEqual(result['stage'],'review');self.assertEqual((root/'native/run/report.json').read_bytes(),before)


if __name__=='__main__':unittest.main()
