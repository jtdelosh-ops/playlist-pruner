import argparse
import unittest
from datetime import datetime, timezone

from playlist_pruner import PlaylistEntry, classify_entries, parse_cutoff, parse_playlist_id


def entry(uri, added_at, *, local=False):
    return PlaylistEntry("p", "Playlist", 0, added_at, uri, "track", "Song", "Artist", "Album", "", local)


class PlaylistPrunerTests(unittest.TestCase):
    def test_playlist_url(self):
        self.assertEqual(
            parse_playlist_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x"),
            "37i9dQZF1DXcBWIGoYBM5M",
        )

    def test_playlist_uri(self):
        self.assertEqual(parse_playlist_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")

    def test_invalid_date(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_cutoff("01/02/2020")

    def test_classification_protects_mixed_age_duplicates(self):
        cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
        entries = [
            entry("spotify:track:a", "2019-01-01T00:00:00Z"),
            entry("spotify:track:a", "2021-01-01T00:00:00Z"),
            entry("spotify:track:b", "2018-01-01T00:00:00Z"),
            entry("spotify:track:c", None),
        ]
        removable, ambiguous, unknown = classify_entries(entries, cutoff)
        self.assertEqual([item.uri for item in removable], ["spotify:track:b"])
        self.assertEqual([item.uri for item in ambiguous], ["spotify:track:a"])
        self.assertEqual([item.uri for item in unknown], ["spotify:track:c"])


if __name__ == "__main__":
    unittest.main()
