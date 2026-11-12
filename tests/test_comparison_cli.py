from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from openweb_news import fixtures
from openweb_news.cli import main
from openweb_news.common import encoded, load
from openweb_news.preparation import prepare
from openweb_news.runner import run


class ComparisonCliTests(unittest.TestCase):
    def test_offline_command_preserves_reports_and_identifies_diagnostic_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = prepare(fixtures.config(root / 'run'), mode='fixture')
            report = run(package)
            # Completed checkpoint reports carry their frozen question objects.
            report['questions'] = package['questions']
            archived, current = root / 'archived.json', root / 'current.json'
            archived.write_bytes(encoded(report))
            current.write_bytes(encoded(report))
            before = archived.read_bytes(), current.read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main(['compare-reports', '--archived-report', str(archived),
                    '--current-report', str(current), '--output', str(root / 'comparison')])
            self.assertEqual(status, 0)
            result = load(root / 'comparison/comparison.json')
            self.assertTrue(result['diagnostic_only'])
            self.assertEqual(result['source_files']['archived_sha256'],
                             result['source_files']['current_sha256'])
            self.assertEqual((archived.read_bytes(), current.read_bytes()), before)
            self.assertNotIn(report['questions'][0]['provider_query'], stdout.getvalue())

    def test_compact_summary_is_rejected_without_echoing_private_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'summary.json'
            source.write_bytes(encoded(dict(questions=['private-question-text'])))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(['compare-reports', '--archived-report', str(source),
                    '--current-report', str(source), '--output', str(root / 'comparison')])
            self.assertEqual(status, 1)
            self.assertNotIn('private-question-text', stderr.getvalue())
            self.assertFalse((root / 'comparison').exists())


if __name__ == '__main__':
    unittest.main()
