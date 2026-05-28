import unittest

from spotify_brain.app import SpotifyBrain


class FakeRunner:
    def run(self, planned):
        return {"ok": True, "argv": planned.argv, "dry_run": planned.dry_run}


class AppTests(unittest.TestCase):
    def test_app_returns_runner_result(self):
        brain = SpotifyBrain(FakeRunner())

        result = brain.execute({"action": "devices"})

        self.assertEqual(result, {"ok": True, "argv": ["device", "list"], "dry_run": False})

    def test_app_returns_policy_error(self):
        brain = SpotifyBrain(FakeRunner())

        result = brain.execute(
            {
                "action": "library-remove",
                "args": {"uris": ["spotify:track:123"]},
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("destructive", result["error"])


if __name__ == "__main__":
    unittest.main()
