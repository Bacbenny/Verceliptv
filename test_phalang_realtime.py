import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

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

    def test_fetch_matches_uses_separate_live_and_featured_queries(self):
        live_match = {"id": "live-1", "team_1": "Live", "team_2": "Match"}
        featured_match = {"id": "upcoming-1", "team_1": "Home", "team_2": "Away"}
        duplicate_live = {"id": "shared-1", "blv": "Live version"}
        duplicate_featured = {"id": "shared-1", "blv": "Featured version"}
        requests = []

        def post(_url, json, **_kwargs):
            requests.append(json)
            if json["queries"][0]["field"] == "is_live":
                rows = [live_match, duplicate_live]
            else:
                rows = [featured_match, duplicate_featured]
            response = Mock()
            response.json.return_value = {"data": rows, "total": len(rows)}
            return response

        with patch.object(main._http_session, "post", side_effect=post):
            matches = main._fetch_phalang_matches()

        self.assertEqual({match["id"] for match in matches}, {"live-1", "upcoming-1", "shared-1"})
        self.assertEqual(len(requests), 2)
        live_query = next(payload for payload in requests if payload["queries"][0]["field"] == "is_live")
        featured_query = next(payload for payload in requests if payload["queries"][0]["field"] == "is_hot")
        self.assertNotIn("query_or", live_query)
        self.assertTrue(featured_query["query_or"])
        self.assertEqual(
            next(match for match in matches if match["id"] == "shared-1")["blv"],
            "Featured version",
        )

    def test_fetch_matches_paginates_until_upstream_total_is_collected(self):
        live_pages = []

        def post(_url, json, **_kwargs):
            if json["queries"][0]["field"] == "is_live":
                page = json["page"]
                live_pages.append(page)
                rows = (
                    [{"id": f"live-{index}"} for index in range(50)]
                    if page == 1
                    else [{"id": "live-50"}]
                )
                total = 51
            else:
                rows = []
                total = 0
            response = Mock()
            response.json.return_value = {"data": rows, "total": total}
            return response

        with patch.object(main._http_session, "post", side_effect=post):
            matches = main._fetch_phalang_matches()

        self.assertEqual(live_pages, [1, 2])
        self.assertEqual(len(matches), 51)

    def test_live_match_older_than_max_age_is_filtered_even_if_api_marks_live(self):
        now = datetime.now(timezone.utc)
        old_match = {
            "id": "stale-live",
            "blv": "Commentator",
            "start_date": (now - timedelta(hours=3)).isoformat(),
            "is_live": True,
        }
        current_match = {
            "id": "current-live",
            "blv": "Commentator",
            "start_date": (now - timedelta(minutes=30)).isoformat(),
            "is_live": True,
        }

        with patch.object(main.time, "time", return_value=now.timestamp()):
            self.assertFalse(main._phalang_is_active(old_match))
            self.assertTrue(main._phalang_is_active(current_match))

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

    def test_live_playlist_uses_prewarmed_hls_with_client_headers(self):
        matches = [
            {
                "id": "live-match",
                "team_1": "Home",
                "team_2": "Away",
                "league": "League",
                "blv": "Commentator",
                "start_date": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "is_live": True,
            }
        ]

        with patch.object(
            main,
            "_get_cached_phalang_stream",
            return_value="https://cdn.example/live.m3u8",
        ), patch.object(main, "_get_server_base_url", return_value="https://iptv.example"):
            lines = main._build_phalang_lines(matches, use_prewarmed_live=True)

        self.assertEqual(
            lines[1],
            "https://cdn.example/live.m3u8"
            "|Referer=https://phalang.live/&User-Agent=Mozilla/5.0",
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

    def test_resolver_uses_public_request_host_when_no_app_url_is_set(self):
        empty_urls = {
            "RENDER_EXTERNAL_URL": "",
            "REPLIT_DOMAINS": "",
            "APP_URL": "",
            "VERCEL_PROJECT_PRODUCTION_URL": "",
            "VERCEL_URL": "",
        }
        with patch.dict(main.os.environ, empty_urls):
            with main.app.test_request_context(
                "/phalang.m3u",
                base_url="https://verceliptv.vercel.app",
            ):
                self.assertEqual(
                    main._get_server_base_url(),
                    "https://verceliptv.vercel.app",
                )

    def test_migrates_old_localhost_resolver_urls(self):
        old_playlist = (
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"PhaLang TV\",Match\n"
            "http://localhost:5000/upcoming/match-1\n"
        )
        empty_urls = {
            "RENDER_EXTERNAL_URL": "",
            "REPLIT_DOMAINS": "",
            "APP_URL": "",
            "VERCEL_PROJECT_PRODUCTION_URL": "",
            "VERCEL_URL": "",
        }
        with patch.dict(main.os.environ, empty_urls):
            with main.app.test_request_context(
                "/phalang.m3u",
                base_url="https://verceliptv.vercel.app",
            ):
                repaired = main._repair_phalang_playlist_urls(old_playlist)

        self.assertIn(
            "https://verceliptv.vercel.app/phalang/live/match-1"
            "|Referer=https://phalang.live/&User-Agent=Mozilla/5.0",
            repaired,
        )
        self.assertNotIn("localhost:5000", repaired)

    def test_prewarm_only_selects_live_and_near_kickoff_matches(self):
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        matches = [
            {
                "id": "live-match",
                "blv": "Commentator",
                "is_live": True,
                "start_date": (now - timedelta(minutes=30)).isoformat(),
            },
            {
                "id": "near-match",
                "blv": "Commentator",
                "is_live": False,
                "start_date": (now + timedelta(seconds=60)).isoformat(),
            },
            {
                "id": "later-match",
                "blv": "Commentator",
                "is_live": False,
                "start_date": (now + timedelta(minutes=10)).isoformat(),
            },
        ]

        with patch.object(main.time, "time", return_value=now_ts):
            candidates = main._phalang_prewarm_candidates(matches)

        self.assertEqual(candidates, ["live-match", "near-match"])


if __name__ == "__main__":
    unittest.main()