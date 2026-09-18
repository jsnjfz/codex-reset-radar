"""状态报告与预测回归：临时数据库、模型替身，不联网。"""
import datetime as dt
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import load_test_config, make_post, utc
from x_watch.forecast import compute_forecast, recent_self_posts, _confirmed_resets
from x_watch.judge import Verdict
from x_watch.report import build_status_report, build_tick_report
from x_watch.storage import Storage


class ReportForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = load_test_config(self.tmp.name, handles=('alice', 'bob'))
        self.storage = Storage(self.config.database_path)
        self.addCleanup(self.storage.close)
        self.now = utc(2026, 9, 18, 12)
        clock = patch('x_watch.forecast.now_utc', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.model = Mock(backend='fake')
        self.model.complete_json.return_value = {'probability': .2}
        backend = patch('x_watch.forecast.build_judge', return_value=self.model)
        backend.start()
        self.addCleanup(backend.stop)

    def post(self, pid='1', handle='alice', text='hello', relation='self', author=None, age=0):
        post = make_post(pid, handle=handle, author=author, text=text, relation=relation,
                         published_at=self.now - dt.timedelta(hours=age))
        with self.storage.transaction():
            self.storage.upsert_post(post, self.now)
            self.storage.upsert_account_post(handle, pid, relation, self.now, 'test')
        return post

    def judge(self, post, reset=True, version=None, probability=.95):
        row = self.storage.get_post(post.post_id)
        verdict = Verdict(post.post_id, row['content_hash'], reset, probability,
                          'usage_limits', 'test', 'fake')
        if version:
            verdict.judge_version = version
        with self.storage.transaction():
            self.storage.save_judgment(verdict)

    def forecast(self, **kwargs):
        return compute_forecast(self.storage, self.config.judge, 'alice', 'UTC', **kwargs)

    def test_edited_post_does_not_keep_old_reset(self):
        self.judge(self.post(text='reset now'))
        self.post(text='not a reset')
        self.assertEqual(self.storage.recent_resets(), [])
        self.assertEqual(_confirmed_resets(self.storage, 'alice', .7), [])

    def test_old_judge_version_does_not_override_current_verdict(self):
        post = self.post()
        self.judge(post, version='reset-v1')
        self.judge(post, reset=False, probability=.01)
        self.assertEqual(self.storage.recent_resets(), [])
        self.assertEqual(_confirmed_resets(self.storage, 'alice', .7), [])
        self.assertEqual(recent_self_posts(self.storage, 'alice')[0]['is_reset'], 0)

    def test_multiple_judge_versions_do_not_duplicate_hit(self):
        post = self.post()
        self.judge(post, version='reset-v1')
        self.judge(post)
        self.assertEqual(len(self.storage.recent_resets()), 1)

    def test_other_authors_reset_is_not_own_reset(self):
        for relation in ('repost', 'context', 'quoted'):
            with self.subTest(relation=relation):
                self.judge(self.post(relation, relation=relation, author='carol'))
        self.assertEqual(_confirmed_resets(self.storage, 'alice', .7), [])
        self.assertIsNone(self.forecast(allow_model_calls=False))
        self.model.complete_json.assert_not_called()

    def test_report_shows_already_reset_without_forecast_cache(self):
        self.judge(self.post())
        report = build_status_report(self.config, self.storage)
        self.assertEqual(len(report['forecasts']), 1)
        self.assertTrue(report['forecasts'][0]['already_reset_today'])
        self.model.complete_json.assert_not_called()

    def test_report_replaces_old_probability_after_reset(self):
        post = self.post()
        self.forecast()
        self.judge(post)
        report = build_status_report(self.config, self.storage)
        self.assertTrue(report['forecasts'][0]['already_reset_today'])
        self.assertEqual(self.model.complete_json.call_count, 1)

    def test_report_uses_configured_reset_threshold(self):
        self.config.judge['threshold'] = .99
        self.judge(self.post())
        self.assertEqual(build_status_report(self.config, self.storage)['forecasts'], [])
        self.model.complete_json.assert_not_called()

    def test_editing_older_post_invalidates_forecast(self):
        self.post('1', age=1)
        self.post('2')
        self.forecast()
        self.post('1', age=1, text='limits might reset soon')
        self.forecast()
        self.assertEqual(self.model.complete_json.call_count, 2)

    def test_unchanged_input_keeps_forecast_cache(self):
        self.post()
        self.forecast()
        self.assertTrue(self.forecast().cached)
        self.assertEqual(self.model.complete_json.call_count, 1)

    def test_read_only_report_marks_edited_input_stale(self):
        self.post()
        self.forecast()
        self.post(text='edited')
        report = build_status_report(self.config, self.storage)
        self.assertIn('输入已变化', report['forecasts'][0]['reasoning'])
        self.assertEqual(self.model.complete_json.call_count, 1)

    def test_tick_report_scopes_accounts_before_reset_limit(self):
        self.judge(self.post('1', age=1))
        self.judge(self.post('2', handle='bob'))
        with self.storage.transaction():
            for handle in ('alice', 'bob'):
                self.storage.ensure_account_state(handle, 'test')
        report = build_tick_report(self.config, self.storage, reset_limit=1, handles=['alice'])
        self.assertEqual(report['configured_accounts'], ['alice'])
        self.assertEqual([r['handle'] for r in report['accounts']], ['alice'])
        self.assertEqual([r['post_id'] for r in report['resets']], ['1'])
        self.assertEqual([r['post_id'] for r in report['recent_posts']], ['1'])
        self.assertEqual([r['handle'] for r in report['forecasts']], ['alice'])

    def test_run_once_cli_passes_handle_to_report_without_model_calls(self):
        import contextlib
        import io
        import json
        from support import FakeProvider, make_client, make_page
        from x_watch.__main__ import build_parser, cmd_run_once
        from x_watch.collector import Collector

        self.judge(self.post('1'))
        self.judge(self.post('2', handle='bob'))
        client = make_client()
        provider = FakeProvider(client, {'alice': [make_page([])]})
        args = build_parser().parse_args([
            'run-once', '--handle', 'alice', '--json', '--no-judge', '--no-export',
        ])
        output = io.StringIO()
        with patch('x_watch.__main__.Collector', side_effect=lambda config, storage:
                   Collector(config, storage, client=client, provider=provider)), \
             patch('x_watch.__main__.setup_logging', return_value=Mock()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cmd_run_once(self.config, args), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['configured_accounts'], ['alice'])
        self.assertEqual([p['post_id'] for p in report['resets']], ['1'])
        self.assertTrue(report['forecasts'][0]['already_reset_today'])
        self.assertFalse(report['judge']['enabled_effective'])
        self.assertEqual([c['handle'] for c in provider.calls], ['alice'])
        self.model.complete_json.assert_not_called()

    def test_scoped_recent_runs_filter_before_limit(self):
        with self.storage.transaction():
            self.storage.start_run('alice-run', 'alice', self.now, 'fake', 'incremental')
            for n in range(11):
                self.storage.start_run(str(n), 'bob', self.now, 'fake', 'incremental')
        report = build_tick_report(self.config, self.storage, handles=['alice'])
        self.assertEqual([r['handle'] for r in report['recent_runs']], ['alice'])

    def test_translation_candidates_stay_in_selected_accounts(self):
        from x_watch.__main__ import _run_translate
        self.judge(self.post('1'))
        self.judge(self.post('2', handle='bob'))
        with patch('x_watch.translate.translate_posts') as translate:
            _run_translate(self.config, self.storage, Mock(), handles=['alice'])
        self.assertEqual([p['post_id'] for p in translate.call_args.args[2]], ['1'])
