from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from openweb_news import fixtures, judging, returned_fields as rf
from openweb_news.common import digest, load
from openweb_news.preparation import prepare
from openweb_news.runner import run


def observation(rank,text,grade=None):
    row=dict(rank=rank,**rf.measure_packet(dict(allowed_text=[dict(field='snippet',text=text)],native_metadata=[],date_observations=[])))
    return row


def report_with(rows,grades):
    return dict(collection=[dict(configuration_id='provider',question_id='q',status='success',returned_field_profile=rf.CONFIG['profile'],returned_field_observations=rows)],
                outcomes=[dict(configuration_id='provider',question_id='q',rank=i,grade=g) for i,g in grades])


class ReturnedFieldTests(unittest.TestCase):
    def test_native_measurements_match_packet_and_preserve_all_snippets(self):
        cfg=fixtures.config(Path('synthetic'))
        from openweb_news._method.providers import configuration_catalog
        provider=configuration_catalog()[0];question=cfg['questions'][0]
        native=dict(rank=1,status='native_result',url=dict(state='present',value='https://example.test/item'),
                    title=dict(state='present',value='A title'),content=dict(state='present',value=['אב ג','two two']),
                    additional_content={'extra':dict(state='present',value=['more','text'])},
                    metadata={'rank':dict(present=True,value=1),'section':dict(present=True,value='business'),'empty':dict(present=True,value=None)},
                    native_dates=dict(observations=[]))
        packet=judging.packet_for(question,provider,native,'synthetic-case')
        before=deepcopy(native)
        expected=rf.measure_position(native)
        self.assertEqual(expected,rf.measure_packet(packet));self.assertEqual(native,before)
        self.assertEqual(expected['snippet_chars'],len('אב ג')+len('two two')+len('more')+len('text'))
        self.assertEqual(expected['snippet_words'],6)
        self.assertEqual(expected['primary_snippet_chars'],len('אב ג')+len('two two'))
        self.assertEqual(expected['additional_snippet_chars'],8)
        self.assertEqual(expected['metadata_entries'],1)
        self.assertEqual(expected['title_chars'],7)

    def test_empty_missing_invalid_and_whitespace_remain_distinct(self):
        self.assertEqual(rf.measure_position({})['snippet_state'],'missing')
        for value in ['',[],[''], '   ']:
            row=rf.measure_position(dict(content=dict(state='present',value=value)))
            self.assertEqual(row['snippet_state'],'empty');self.assertEqual(rf.band(row),'empty')
        row=rf.measure_position(dict(content=dict(state='non_string',value=42)))
        self.assertEqual(row['snippet_state'],'non_string');self.assertEqual(rf.band(row),'unusable')
        self.assertEqual(rf.measure_position(dict(content=dict(state='present',value='   ')))['snippet_chars'],3)

    def test_metadata_does_not_count_duplicate_date_aliases_or_mechanical_fields(self):
        packet=dict(allowed_text=[],native_metadata=[dict(field='native:position',text='1'),dict(field='native:score',text='0'),
              dict(field='native:published',text='"unverified date"'),dict(field='date:0',text='"unverified date"'),dict(field='native:empty',text='null')],
              date_observations=[dict(raw_value='unverified date')])
        result=rf.measure_packet(packet)
        self.assertEqual(result['metadata_entries'],2);self.assertTrue(result['date_metadata_present'])
        self.assertFalse(result['title_present'])

    def test_percentiles_and_band_boundaries(self):
        self.assertEqual(rf.distribution([])['median'],None)
        self.assertEqual(rf.distribution([1,2,3,4])['median'],2)
        self.assertEqual(rf.distribution(range(1,101))['p95'],95)
        for n,label in [(1,'1–199'),(199,'1–199'),(200,'200–499'),(499,'200–499'),(500,'500–999'),(999,'500–999'),(1000,'1000–1999'),(1999,'1000–1999'),(2000,'2000+')]:
            self.assertEqual(rf.band(observation(1,'x'*n)),label)

    def test_unknowns_and_ungraded_positions_are_not_resolved_failures(self):
        report=report_with([observation(i,'small') for i in (1,2,3,4)],[(1,2),(2,0),(3,None)])
        before=deepcopy(report);a=rf.analyze(report);q=a['providers'][0]['quality']
        self.assertEqual((q['results'],q['resolved'],q['unknown'],q['ungraded']),(4,2,1,1))
        self.assertEqual(q['answer_rate_resolved'],.5);self.assertEqual(report,before)
        self.assertEqual(a['unmeasured_outcomes'],0)
        self.assertEqual(sum(r['results'] for r in a['length_bands']),4)
        self.assertEqual(a['providers'][0]['measured_results'],4) # not 10 hypothetical slots

    def test_legacy_measurements_unavailable_and_duplicate_joins_rejected(self):
        report=report_with([],[(1,2)]);report['collection'][0].pop('returned_field_observations')
        a=rf.analyze(report);p=a['providers'][0]
        self.assertEqual(a['status'],'partial_or_unavailable');self.assertEqual(a['unmeasured_outcomes'],1)
        self.assertEqual(p['snippet_chars']['count'],0);self.assertEqual(p['snippet_states'],{})
        report['collection'].append(deepcopy(report['collection'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate.*collection'):rf.analyze(report)
        report=report_with([observation(1,'a'),observation(1,'b')],[(1,2)])
        with self.assertRaisesRegex(ValueError,'duplicate.*measurement'):rf.analyze(report)

    def test_exports_escape_labels_and_do_not_copy_returned_text(self):
        report=report_with([observation(1,'PRIVATE SYNTHETIC SNIPPET')],[(1,2)])
        for row in report['collection']+report['outcomes']:row['configuration_id']='a<script>'
        a=rf.analyze(report);rendered=rf.render_html(a)
        self.assertIn('a&lt;script&gt;',rendered);self.assertNotIn('a<script>',rendered)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);rf.write(root,a)
            for path in root.iterdir():self.assertNotIn('PRIVATE SYNTHETIC SNIPPET',path.read_text())
            with (root/'returned-fields.csv').open() as h:self.assertEqual(len(list(csv.DictReader(h))),1)

    def test_chart_uses_each_median_directly_and_distinguishes_unavailable(self):
        from html.parser import HTMLParser
        rows=[]
        for rank,(primary,extra) in enumerate([(0,300),(100,0),(1000,150)],1):
            position=dict(content=dict(state='present',value='x'*primary),
                          additional_content={'extra':dict(state='present',value='x'*extra)})
            rows.append(dict(rank=rank,**rf.measure_position(position)))
        a=rf.analyze(report_with(rows,[(1,2),(2,None),(3,0)]))
        p=a['providers'][0]
        self.assertEqual(p['snippet_chars']['median'],300)
        self.assertEqual(p['primary_snippet_chars']['median'],100)
        self.assertEqual(p['additional_snippet_chars']['median'],150)
        # Adding separately computed medians would give the wrong combined length.
        class Chart(HTMLParser):
            def __init__(self):super().__init__();self.view=None;self.rows={}
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if 'data-snippet-view' in attrs:self.view=attrs['data-snippet-view']
                if 'data-length-chars' in attrs:self.rows.setdefault(self.view,[]).append(attrs)
        parsed=Chart();parsed.feed(rf.snippet_chart(a))
        self.assertEqual(parsed.rows['all'][0]['data-length-chars'],'300')
        self.assertEqual(parsed.rows['main'][0]['data-length-chars'],'100')
        self.assertIn('median 300 characters',parsed.rows['all'][0]['aria-label'])
        missing=rf.analyze(dict(collection=[dict(configuration_id='legacy',question_id='q',status='success')],outcomes=[]))
        parsed=Chart();parsed.feed(rf.snippet_chart(missing))
        self.assertEqual(parsed.rows['all'][0]['data-length-chars'],'unavailable')
        self.assertIn('unavailable',parsed.rows['all'][0]['aria-label'])

    def test_pipeline_reports_and_resume_keep_measurements_and_final_grade_join(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cfg=fixtures.config(root/'run');package=prepare(cfg,mode='fixture')
            report=run(package);a=report['returned_fields_analysis'];n=len(report['outcomes'])
            self.assertEqual(a['status'],'complete');self.assertEqual(a['measured_results'],n)
            self.assertEqual(a['unmeasured_outcomes'],0)
            self.assertTrue(all('returned_field_observations' in row for row in report['collection']))
            before=(root/'run/attempts.jsonl').read_bytes();scores=deepcopy(report['scores'])
            again=run(package)
            self.assertEqual((root/'run/attempts.jsonl').read_bytes(),before)
            self.assertEqual(again['returned_fields_analysis'],a);self.assertEqual(again['scores'],scores)
            self.assertEqual(load(root/'run/returned-fields.json'),a)
            self.assertIn('Titles, metadata and snippet length',(root/'run/report.html').read_text())
            from openweb_news.phase_report import write_readable
            report['questions']=cfg['questions'];write_readable(root/'phase',report)
            self.assertIn('id="returned-fields"',(root/'phase/report.html').read_text())
            # A later review changes grade summaries, never the raw length measurements.
            changed=deepcopy(report);changed['outcomes'][0]['grade']=None
            b=rf.analyze(changed)
            self.assertEqual([p['snippet_chars'] for p in a['providers']],[p['snippet_chars'] for p in b['providers']])
            self.assertEqual(b['providers'][0]['quality']['unknown'],a['providers'][0]['quality']['unknown']+1)
