import unittest

from codex_sync.redact import redact_obj, redact_text


class RedactTests(unittest.TestCase):
    def test_redacts_openai_key_like_value(self) -> None:
        text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", redact_text(text))

    def test_redacts_sensitive_dict_keys(self) -> None:
        data = redact_obj({"token": "abc", "nested": {"password": "pw", "ok": "yes"}})
        self.assertEqual(data["token"], "<redacted>")
        self.assertEqual(data["nested"]["password"], "<redacted>")
        self.assertEqual(data["nested"]["ok"], "yes")


if __name__ == "__main__":
    unittest.main()

