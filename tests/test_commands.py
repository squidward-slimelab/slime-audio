import unittest

from spotify_brain.commands import CommandError, plan_command


class CommandTests(unittest.TestCase):
    def test_status_plans_read_only_command(self):
        planned = plan_command("status")

        self.assertEqual(planned.argv, ["status"])
        self.assertFalse(planned.mutates)
        self.assertFalse(planned.destructive)

    def test_playlist_remove_requires_confirm_without_dry_run(self):
        with self.assertRaises(CommandError):
            plan_command(
                "playlist-remove",
                {"playlist_id": "abc", "uris": ["spotify:track:123"]},
            )

    def test_playlist_remove_allows_dry_run(self):
        planned = plan_command(
            "playlist-remove",
            {"playlist_id": "abc", "uris": ["spotify:track:123"]},
            dry_run=True,
        )

        self.assertEqual(planned.argv, ["playlist", "remove", "abc", "spotify:track:123"])
        self.assertTrue(planned.dry_run)

    def test_playlist_add_accepts_single_uri_string(self):
        planned = plan_command(
            "playlist-add",
            {"playlist_id": "abc", "uris": "spotify:track:123"},
            confirm=True,
        )

        self.assertEqual(planned.argv, ["playlist", "add", "abc", "spotify:track:123"])


if __name__ == "__main__":
    unittest.main()
