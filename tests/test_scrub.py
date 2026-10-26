"""Privacy defaults (spec section 7)."""

from __future__ import annotations

import datetime
import json
import unittest

import _support  # noqa: F401  (sys.path)

from naijacloud_argus._scrub import FILTERED, scrub, scrub_string, sensitive_key, strip_query


class SensitiveKeys(unittest.TestCase):
    def test_contains(self) -> None:
        for k in (
            "password",
            "user_password",
            "PassWD",
            "client-secret",
            "access_token",
            "X-Api-Key",
            "apiKey",
            "AccessKeyId",
            "Authorization",
            "aws_credentials",
            "Set-Cookie",
            "private_key",
            "card_number",
            "creditCard",
            "customer_bvn",
            "cvv2",
            "CVC",
            "session_id",
            "sessionToken",
            "webhook signature",
        ):
            self.assertTrue(sensitive_key(k), k)

    def test_equals(self) -> None:
        for k in ("auth", "PWD", "pin", "otp", "session", "card", "sid", "S-I-D"):
            self.assertTrue(sensitive_key(k), k)

    def test_not_sensitive(self) -> None:
        for k in ("author", "pinned", "cardinality", "sidebar", "user_id", "email", "authority"):
            self.assertFalse(sensitive_key(k), k)


class Strings(unittest.TestCase):
    def test_card_numbers_that_pass_luhn(self) -> None:
        self.assertEqual(scrub_string("card 4111 1111 1111 1111 ok"), "card {} ok".format(FILTERED))
        self.assertEqual(scrub_string("5500-0000-0000-0004"), FILTERED)
        # 16 digits failing Luhn is an order number, not a card.
        self.assertEqual(scrub_string("order 1234567812345678"), "order 1234567812345678")
        self.assertEqual(scrub_string("short 411111111111"), "short 411111111111")

    def test_credentials(self) -> None:
        self.assertEqual(
            scrub_string("Authorization: Bearer abcdefgh12345678"),
            "Authorization: Bearer " + FILTERED,
        )
        self.assertEqual(scrub_string("Basic dXNlcjpwYXNzd29yZA=="), "Basic " + FILTERED)
        self.assertEqual(scrub_string("token abcdefgh1234"), "token " + FILTERED)

    def test_jwt(self) -> None:
        # A made-up token, joined here so secret scanners don't flag the fixture.
        jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"])
        self.assertEqual(scrub_string("got " + jwt), "got " + FILTERED)

    def test_key_value_pairs(self) -> None:
        self.assertEqual(
            scrub_string("login password=hunter2 ok"), "login password={} ok".format(FILTERED)
        )
        self.assertEqual(
            scrub_string('{"api_key": "abc123"}'), '{{"api_key": "{}"}}'.format(FILTERED)
        )
        self.assertEqual(
            scrub_string("?client_secret=s3cr3t&x=1"), "?client_secret={}&x=1".format(FILTERED)
        )
        self.assertEqual(scrub_string("pin=1234"), "pin=" + FILTERED)
        # `equals` keys only count with `=`, so prose survives.
        self.assertEqual(scrub_string("card: declined"), "card: declined")
        self.assertEqual(scrub_string("spin=3"), "spin=3")

    def test_plain_text_untouched(self) -> None:
        s = "order ord_81 placed for 18500 kobo at 2026-10-25"
        self.assertEqual(scrub_string(s), s)


class Structures(unittest.TestCase):
    def test_sensitive_keys_at_any_depth(self) -> None:
        out = scrub(
            {
                "user": {"name": "Ada", "password": "hunter2", "nested": [{"token": "t"}]},
                "headers": {"Authorization": "Bearer x", "Accept": "*/*"},
                "empty_password": "",
                "none_token": None,
            }
        )
        self.assertEqual(out["user"]["password"], FILTERED)
        self.assertEqual(out["user"]["nested"][0]["token"], FILTERED)
        self.assertEqual(out["user"]["name"], "Ada")
        self.assertEqual(out["headers"]["Authorization"], FILTERED)
        self.assertEqual(out["headers"]["Accept"], "*/*")
        self.assertEqual(out["empty_password"], "")
        self.assertIsNone(out["none_token"])

    def test_json_safe(self) -> None:
        class Thing:
            def __repr__(self) -> str:
                return "<Thing password=hunter2>"

        class Broken:
            def __repr__(self) -> str:
                raise RuntimeError("no")

        cyclic: dict = {"a": 1}
        cyclic["self"] = cyclic
        out = scrub(
            {
                "when": datetime.datetime(2026, 10, 25),
                "nan": float("nan"),
                "inf": float("inf"),
                "set": {1},
                "tuple": (1, "a"),
                "bytes": b"\x00\x01",
                "thing": Thing(),
                "broken": Broken(),
                3: "int key",
                "cyclic": cyclic,
            }
        )
        json.dumps(out, allow_nan=False)  # must not raise
        self.assertEqual(out["nan"], "nan")
        self.assertEqual(out["set"], [1])
        self.assertEqual(out["thing"], "<Thing password={}>".format(FILTERED))
        self.assertEqual(out["broken"], "<Broken object>")
        self.assertEqual(out["3"], "int key")
        self.assertEqual(out["cyclic"]["self"], "[…]")

    def test_depth_is_cut(self) -> None:
        deep: dict = {}
        cur = deep
        for _ in range(20):
            cur["x"] = {}
            cur = cur["x"]
        text = json.dumps(scrub(deep), ensure_ascii=False)
        self.assertIn("[…]", text)

    def test_strip_query(self) -> None:
        self.assertEqual(strip_query("https://a.ng/p?token=1#f"), "https://a.ng/p")
        self.assertEqual(strip_query("/p#frag"), "/p")
        self.assertEqual(strip_query("/p"), "/p")


if __name__ == "__main__":
    unittest.main()
