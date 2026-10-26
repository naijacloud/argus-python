"""DSN parsing and the configuration order (spec section 1)."""

from __future__ import annotations

import os
import unittest

from _support import TEST_KEY, ArgusTestCase

import naijacloud_argus as argus
from naijacloud_argus._dsn import cron_path, parse_dsn


class ParseDsn(unittest.TestCase):
    def test_production_shape(self) -> None:
        d = parse_dsn("https://{}@argus-in.naijacloud.com/41".format(TEST_KEY))
        assert d is not None
        self.assertEqual(d.public_key, TEST_KEY)
        self.assertEqual(d.project_id, "41")
        self.assertEqual(d.origin, "https://argus-in.naijacloud.com")
        self.assertEqual(d.envelope_url(), "https://argus-in.naijacloud.com/api/41/envelope/")
        self.assertEqual(d.ingest_url("/v1/logs"), "https://argus-in.naijacloud.com/v1/logs")

    def test_port_and_path_prefix(self) -> None:
        d = parse_dsn("http://k@127.0.0.1:9000/proxy/argus/7")
        assert d is not None
        self.assertEqual(d.origin, "http://127.0.0.1:9000/proxy/argus")
        self.assertEqual(d.envelope_url(), "http://127.0.0.1:9000/proxy/argus/api/7/envelope/")

    def test_whitespace_is_trimmed(self) -> None:
        self.assertIsNotNone(parse_dsn("  https://k@h.example/1\n"))

    def test_invalid(self) -> None:
        for raw in (
            None,
            "",
            "not a url",
            "ftp://k@h/1",
            "https://h.example/1",  # no key
            "https://k@h.example/",  # no project
            "https://k@h.example/abc",  # project not numeric
            "https://k@h.example:99999/1",  # bad port
            123,
        ):
            self.assertIsNone(parse_dsn(raw), raw)  # type: ignore[arg-type]

    def test_repr_hides_the_key(self) -> None:
        d = parse_dsn("https://{}@h.example/1".format(TEST_KEY))
        self.assertNotIn(TEST_KEY, repr(d))

    def test_cron_slug_is_escaped(self) -> None:
        self.assertEqual(cron_path("a b/c", "/start"), "/v1/cron/a%20b%2Fc/start")


class ConfigOrder(ArgusTestCase):
    def test_explicit_dsn_wins(self) -> None:
        os.environ["ARGUS_DSN"] = "https://other@h.example/1"
        c = self.init()
        assert c.dsn is not None
        self.assertEqual(c.dsn.project_id, "42")

    def test_argus_dsn_then_sentry_dsn(self) -> None:
        os.environ["SENTRY_DSN"] = "https://s@h.example/2"
        self.assertEqual(argus.init().dsn.project_id, "2")  # type: ignore[union-attr]
        os.environ["ARGUS_DSN"] = "https://a@h.example/3"
        self.assertEqual(argus.init().dsn.project_id, "3")  # type: ignore[union-attr]

    def test_empty_explicit_dsn_turns_it_off(self) -> None:
        os.environ["ARGUS_DSN"] = "https://a@h.example/3"
        self.assertFalse(argus.init(dsn="").enabled)

    def test_release_and_environment(self) -> None:
        c = self.init()
        self.assertIsNone(c.options.release)
        self.assertEqual(c.options.environment, "production")
        os.environ["SENTRY_RELEASE"] = "s-rel"
        os.environ["SENTRY_ENVIRONMENT"] = "s-env"
        c = self.init()
        self.assertEqual((c.options.release, c.options.environment), ("s-rel", "s-env"))
        os.environ["ARGUS_RELEASE"] = "a-rel"
        os.environ["ARGUS_ENVIRONMENT"] = "a-env"
        c = self.init()
        self.assertEqual((c.options.release, c.options.environment), ("a-rel", "a-env"))
        c = self.init(release="o-rel", environment="o-env")
        self.assertEqual((c.options.release, c.options.environment), ("o-rel", "o-env"))

    def test_service_name(self) -> None:
        self.assertEqual(self.init().options.service_name, "app")
        os.environ["OTEL_SERVICE_NAME"] = "otel"
        self.assertEqual(self.init().options.service_name, "otel")
        os.environ["ARGUS_SERVICE_NAME"] = "argus"
        self.assertEqual(self.init().options.service_name, "argus")
        self.assertEqual(self.init(service_name="mine").options.service_name, "mine")

    def test_unknown_options_are_ignored(self) -> None:
        self.assertTrue(self.init(not_an_option=1).enabled)

    def test_reinit_replaces_the_client(self) -> None:
        first = self.init()
        second = self.init()
        self.assertIsNot(first, second)
        self.assertFalse(first.enabled)
        self.assertIs(argus.get_client(), second)


if __name__ == "__main__":
    unittest.main()
