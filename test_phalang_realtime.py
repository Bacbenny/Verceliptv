import unittest
from unittest.mock import patch

import main


class PhaLangRealtimeTests(unittest.TestCase):
    def setUp(self):
        with main._upcoming_cache_lock:
            main._upcoming_cache.clear()
            main._upcoming_inflight.clear()

    def tearDown(self):
        with main._upcoming_cache_lock:
            main._upcoming_cache.clear()
            main._upcoming_inflight.clear()

    def test_playlist_uses_stable_resolver_without_fetching_streams(self):
        matches = [
            {
                "id": "match/123",
                "team_1": "Home",
                "team_2": "Away",
                "league": "League",
                "blv": "Commentator",
                "start_date": "2099-01-01T12:00:00+00:00",
                "is_live": False,
            }
        ]

        with patch.object(main, "_get_server_base_url", return_value="https://iptv.example"), \
                patch.object(main, "_fetch_phalang_stream") as fetch_stream:
            lines = main._build_phalang_lines(matches)

        fetch_stream.assert_not_called()
        self.assertEqual(
            lines[1],
            'https://iptv.example/phalang/live/match%2F123'
            '|Referer=https://phalang.live/&User-Agent=Mozilla/5.0',
        )

    def test_successful_stream_resolution_is_cached(self):
        with patch.object(
            main,
            "_fetch_phalang_stream",
            return_value="https://cdn.example/live.m3u8",
        ) as fetch_stream:
            first = main._cached_phalang_stream("match-1")
            second = main._cached_phalang_stream("match-1")

        self.assertEqual(first, "https://cdn.example/live.m3u8")
        self.assertEqual(second, first)
        fetch_stream.assert_called_once_with("match-1")

    def test_missing_stream_has_short_negative_cache(self):
        with patch.object(main, "_fetch_phalang_stream", return_value="") as fetch_stream:
            self.assertEqual(main._cached_phalang_stream("match-2"), "")
            self.assertEqual(main._cached_phalang_stream("match-2"), "")

        fetch_stream.assert_called_once_with("match-2")

    def test_live_resolver_redirect_is_not_cached_by_clients(self):
        with patch.object(
            main,
            "_fetch_phalang_stream",
            return_value="https://cdn.example/live.m3u8",
        ):
            response = main.app.test_client().get(
                "/phalang/live/match-3",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "https://cdn.example/live.m3u8",
        )
        self.assertEqual(
            response.headers["Cache-Control"],
            "no-store, no-cache, max-age=0, private",
        )


if __name__ == "__main__":
    unittest.main()