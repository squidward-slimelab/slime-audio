import unittest

from spotify_brain.redact import redact, redact_text


class RedactTests(unittest.TestCase):
    def test_redacts_secret_keys_recursively(self):
        self.assertEqual(
            redact({"nested": {"access_token": "abc"}, "ok": "yes"}),
            {
                "nested": {"access_token": "[REDACTED]"},
                "ok": "yes",
            },
        )

    def test_redacts_spotify_cookie_text(self):
        text = "cookie: sp_dc=secret; sp_key=alsosecret"

        redacted = redact_text(text)
        self.assertNotIn("secret", redacted)
        self.assertIn("[REDACTED]", redacted)


if __name__ == "__main__":
    unittest.main()
