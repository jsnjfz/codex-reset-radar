"""配置校验与 HTTP 细节测试。对应方案 §8、§10、§11.3。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from support import CONFIG_TEMPLATE, load_test_config, write_config  # noqa: E402

from x_watch.config import ConfigError, load_config, normalize_handle  # noqa: E402
from x_watch.httpclient import (  # noqa: E402
    BudgetExhausted,
    HttpClient,
    parse_retry_after,
)
from x_watch.toml_compat import TomlError, load_toml  # noqa: E402
from x_watch.util import safe_url  # noqa: E402


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="x-watch-cfg-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, body: str) -> str:
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def base_body(self) -> str:
        return CONFIG_TEMPLATE.format(
            max_pages=3,
            bootstrap_hours=72,
            bootstrap_max_pages=3,
            overlap_hours=6,
            max_requests=30,
            include_reposts="false",
        )


class HandleTests(unittest.TestCase):
    def test_normalization(self) -> None:
        self.assertEqual(normalize_handle("@Alice"), "alice")
        self.assertEqual(normalize_handle("  BCherny "), "bcherny")
        self.assertEqual(normalize_handle("https://x.com/simonw"), "simonw")
        self.assertEqual(normalize_handle("https://x.com/simonw/"), "simonw")

    def test_non_string_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            normalize_handle(123)


class ConfigValidationTests(TempDirCase):
    def test_valid_config_loads(self) -> None:
        config = load_test_config(self.tmp, handles=("alice", "bob"))
        self.assertEqual([a.handle for a in config.accounts], ["alice", "bob"])
        self.assertEqual(config.source["provider"], "fxtwitter_json")

    def test_relative_paths_resolve_against_config_dir(self) -> None:
        """相对路径不能受当前工作目录影响（方案 §8）。"""
        config = load_test_config(self.tmp)
        self.assertTrue(config.database_path.startswith(os.path.realpath(self.tmp))
                        or config.database_path.startswith(self.tmp))
        self.assertTrue(config.database_path.endswith(os.path.join("data", "test.sqlite3")))

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(os.path.join(self.tmp, "nope.toml"))

    def test_no_accounts_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(self.base_body()))
        self.assertIn("没有任何 [[accounts]]", str(ctx.exception))

    def test_duplicate_account_rejected(self) -> None:
        body = self.base_body() + '\n[[accounts]]\nhandle = "alice"\n[[accounts]]\nhandle = "@Alice"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("重复出现", str(ctx.exception))

    def test_invalid_handle_rejected(self) -> None:
        body = self.base_body() + '\n[[accounts]]\nhandle = "not a handle!"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("不符合 X 用户名格式", str(ctx.exception))

    def test_all_accounts_disabled_rejected(self) -> None:
        body = self.base_body() + '\n[[accounts]]\nhandle = "alice"\nenabled = false\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("没有可采集的对象", str(ctx.exception))

    def test_unknown_key_rejected(self) -> None:
        body = self.base_body().replace(
            'page_size = 50', 'page_size = 50\nmystery_option = 1'
        ) + '\n[[accounts]]\nhandle = "alice"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("无法识别的键", str(ctx.exception))

    def test_out_of_range_value_rejected(self) -> None:
        body = self.base_body().replace(
            "max_pages_per_account = 3", "max_pages_per_account = 999"
        ) + '\n[[accounts]]\nhandle = "alice"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("必须在 1..50 之间", str(ctx.exception))

    def test_budget_smaller_than_page_count_rejected(self) -> None:
        """预算不够跑完一个账号的分页会让账号结构性饿死（方案 §8）。"""
        body = self.base_body().replace(
            "max_total_requests_per_run = 30", "max_total_requests_per_run = 2"
        ) + '\n[[accounts]]\nhandle = "alice"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("无法跑完一轮分页", str(ctx.exception))

    def test_unknown_provider_rejected(self) -> None:
        body = self.base_body().replace(
            'provider = "fxtwitter_json"', 'provider = "scrape_with_login"'
        ) + '\n[[accounts]]\nhandle = "alice"\n'
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(body))
        self.assertIn("provider 必须是", str(ctx.exception))

    def test_rss_with_auto_fallback_rejected(self) -> None:
        body = self.base_body().replace(
            'provider = "fxtwitter_json"', 'provider = "fxtwitter_rss"'
        ).replace("auto_fallback = false", "auto_fallback = true") + '\n[[accounts]]\nhandle = "alice"\n'
        with self.assertRaises(ConfigError):
            load_config(self.write(body))

    def test_scope_hash_changes_with_include_replies(self) -> None:
        """采集范围变化必须体现在 scope_hash 上（方案 §7.3）。"""
        a = load_test_config(self.tmp)
        tmp2 = tempfile.mkdtemp(prefix="x-watch-cfg2-")
        try:
            path = write_config(tmp2)
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body.replace("include_replies = true", "include_replies = false"))
            b = load_config(path)
            self.assertNotEqual(a.accounts[0].scope_hash, b.accounts[0].scope_hash)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


class TomlSubsetTests(TempDirCase):
    def test_comment_inside_string_preserved(self) -> None:
        path = self.write('[app]\nuser_agent = "x-watch/0.1 (#1 tool)"  # 真注释\n')
        data = load_toml(path)
        self.assertEqual(data["app"]["user_agent"], "x-watch/0.1 (#1 tool)")

    def test_types(self) -> None:
        path = self.write('[a]\ni = 10\nf = 1.5\nt = true\nf2 = false\ns = "x"\n')
        data = load_toml(path)["a"]
        self.assertEqual(data["i"], 10)
        self.assertEqual(data["f"], 1.5)
        self.assertIs(data["t"], True)
        self.assertIs(data["f2"], False)
        self.assertEqual(data["s"], "x")

    def test_array_of_tables(self) -> None:
        path = self.write('[[accounts]]\nhandle = "a"\n[[accounts]]\nhandle = "b"\n')
        data = load_toml(path)
        self.assertEqual([x["handle"] for x in data["accounts"]], ["a", "b"])

    def test_duplicate_key_rejected(self) -> None:
        path = self.write('[a]\nx = 1\nx = 2\n')
        with self.assertRaises(TomlError):
            load_toml(path)

    def test_garbage_line_rejected(self) -> None:
        path = self.write("[a]\nthis is not toml\n")
        with self.assertRaises(TomlError):
            load_toml(path)


class RetryAfterTests(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120)

    def test_http_date(self) -> None:
        value = parse_retry_after("Thu, 01 Jan 2099 00:00:00 GMT")
        self.assertIsNotNone(value)
        self.assertLessEqual(value, 24 * 3600)  # 上限保护

    def test_past_date_is_zero(self) -> None:
        self.assertEqual(parse_retry_after("Thu, 01 Jan 2000 00:00:00 GMT"), 0)

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))

    def test_absurd_value_capped(self) -> None:
        self.assertEqual(parse_retry_after("99999999"), 24 * 3600)


class BudgetTests(unittest.TestCase):
    def test_request_budget_enforced(self) -> None:
        client = HttpClient("t", 1, 1, max_total_requests=2, sleeper=lambda s: None)
        client.requests_used = 2
        with self.assertRaises(BudgetExhausted):
            client.check_budget(1)

    def test_run_seconds_budget_enforced(self) -> None:
        client = HttpClient("t", 1, 1, max_run_seconds=0, sleeper=lambda s: None)
        with self.assertRaises(BudgetExhausted):
            client.check_budget(1)

    def test_sleep_refused_when_no_time_left(self) -> None:
        client = HttpClient("t", 1, 1, max_run_seconds=5, sleeper=lambda s: None)
        self.assertFalse(client.sleep_within_budget(100))
        self.assertTrue(client.sleep_within_budget(0))

    def test_non_https_refused(self) -> None:
        client = HttpClient("t", 1, 1, sleeper=lambda s: None)
        result = client.get("http://example.invalid/x")
        self.assertEqual(result.error_kind, "blocked_content")
        self.assertIn("只允许 https", result.error_detail)


class SafeUrlTests(unittest.TestCase):
    def test_accepts_https(self) -> None:
        self.assertEqual(safe_url("https://x.com/a"), "https://x.com/a")

    def test_rejects_dangerous_schemes(self) -> None:
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x"):
            self.assertIsNone(safe_url(bad))

    def test_rejects_embedded_newlines(self) -> None:
        self.assertIsNone(safe_url("https://x.com/a\nhttps://evil"))

    def test_rejects_non_string(self) -> None:
        self.assertIsNone(safe_url(None))
        self.assertIsNone(safe_url(42))


if __name__ == "__main__":
    unittest.main()
