import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import main
from PIL import Image


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

    def test_phalang_football_logo_composes_both_teams_and_league(self):
        match = {
            "id": "logo-match",
            "team_1": "Home FC",
            "team_2": "Away FC",
            "league": "Premier League",
            "team_1_logo_url": "https://assets.example/home.png",
            "team_2_logo_url": "https://assets.example/away.png",
            "league_logo_url": "https://assets.example/league.png",
            "blv": "Commentator",
            "start_date": "2099-01-01T12:00:00+00:00",
            "is_live": False,
        }
        with patch.object(main, "_get_server_base_url", return_value="https://iptv.example"):
            line = main._build_phalang_lines([match])[0]

        logo_url = line.split('tvg-logo="', 1)[1].split('"', 1)[0]
        query = parse_qs(urlsplit(logo_url).query)
        self.assertEqual(urlsplit(logo_url).path, "/match-logo.png")
        self.assertEqual(query["home"], ["https://assets.example/home.png"])
        self.assertEqual(query["league"], ["https://assets.example/league.png"])
        self.assertEqual(query["away"], ["https://assets.example/away.png"])
        self.assertEqual(query["v"], [main.MATCH_LOGO_RENDER_VERSION])

    def test_phalang_basketball_logo_composes_teams_and_sport_mark(self):
        match = {
            "id": "basketball-logo-match",
            "team_1": {"name": "Cleveland Cavaliers", "logo": "https://assets.example/cavaliers.png"},
            "team_2": {"name": "Minnesota Timberwolves", "logo": "https://assets.example/timberwolves.png"},
            "league": {"title": "NBA"},
            "desc": "Basketball",
            "blv": "Commentator",
            "start_date": "2099-01-01T12:00:00+00:00",
            "is_live": False,
        }
        with patch.object(main, "_get_server_base_url", return_value="https://iptv.example"):
            line = main._build_phalang_lines([match])[0]

        logo_url = line.split('tvg-logo="', 1)[1].split('"', 1)[0]
        query = parse_qs(urlsplit(logo_url).query)
        self.assertEqual(query["home"], ["https://assets.example/cavaliers.png"])
        self.assertEqual(query["league"], [main.SPORT_LOGOS["basketball"]])
        self.assertEqual(query["away"], ["https://assets.example/timberwolves.png"])

    def test_giovang_football_logo_reads_nested_provider_logos(self):
        match = {
            "id": "gio-logo-match",
            "teams": {
                "home": {"name": "Home FC", "logo": "https://assets.example/home.png"},
                "away": {"name": "Away FC", "badge": "https://assets.example/away.png"},
            },
            "league": {"title": "MLS", "image_url": "https://assets.example/league.png"},
            "time_start": 4102444800,
            "blv": [{
                "blv_name": "Commentator",
                "mobile_stream_url": "https://stream.example/live.m3u8",
            }],
        }
        with patch.object(main, "_get_server_base_url", return_value="https://iptv.example"):
            line = main._build_giovang_lines([match])[0]

        logo_url = line.split('tvg-logo="', 1)[1].split('"', 1)[0]
        query = parse_qs(urlsplit(logo_url).query)
        self.assertEqual(query["home"], ["https://assets.example/home.png"])
        self.assertEqual(query["league"], ["https://assets.example/league.png"])
        self.assertEqual(query["away"], ["https://assets.example/away.png"])

    def test_giovang_volleyball_logo_composes_nested_participant_logos(self):
        match = {
            "id": "gio-volleyball-logo-match",
            "type": "Volleyball",
            "teams": {
                "home": {"name": "Japan", "logo": "https://assets.example/japan.png"},
                "away": {"name": "Thailand", "logo": "https://assets.example/thailand.png"},
            },
            "league": {"title": "Nations League"},
            "time_start": 4102444800,
            "blv": [{
                "blv_name": "Commentator",
                "mobile_stream_url": "https://stream.example/live.m3u8",
            }],
        }
        with patch.object(main, "_get_server_base_url", return_value="https://iptv.example"):
            line = main._build_giovang_lines([match])[0]

        logo_url = line.split('tvg-logo="', 1)[1].split('"', 1)[0]
        query = parse_qs(urlsplit(logo_url).query)
        self.assertEqual(query["home"], ["https://assets.example/japan.png"])
        self.assertEqual(query["league"], [main.SPORT_LOGOS["volleyball"]])
        self.assertEqual(query["away"], ["https://assets.example/thailand.png"])

    def test_match_logo_png_renders_raster_and_embeds_all_three_assets(self):
        asset = BytesIO()
        Image.new("RGBA", (32, 32), (220, 30, 60, 255)).save(asset, format="PNG")
        png_bytes = asset.getvalue()
        path = (
            "/match-logo.png?home=https%3A%2F%2Fimgts.sportpulseapiz.com%2Fhome.png"
            "&league=https%3A%2F%2Fimages.keovip88.net%2Fleague.png"
            "&away=https%3A%2F%2Fstatic.flashscore.com%2Faway.png"
            "&home_name=Home+FC&league_name=Premier+League&away_name=Away+FC"
        )
        with patch.object(main, "_download_logo_asset", return_value=png_bytes) as fetch:
            response = main.app.test_client().get(path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("max-age=60", response.headers["Cache-Control"])
        self.assertIn("s-maxage=60", response.headers["Cache-Control"])
        result = Image.open(BytesIO(response.data))
        self.assertEqual(result.size, (1920, 1080))
        self.assertEqual(fetch.call_count, 3)

    def test_match_logo_png_is_full_hd_with_larger_team_badges(self):
        with patch.object(main, "_draw_match_logo_badge") as draw_badge:
            response = main.app.test_client().get("/match-logo.png")

        result = Image.open(BytesIO(response.data)).convert("RGB")
        self.assertEqual(result.size, (1920, 1080))
        self.assertEqual(
            [call.args[2] for call in draw_badge.call_args_list],
            [560, 360, 560],
        )
        self.assertEqual(
            [call.args[1] for call in draw_badge.call_args_list],
            [(400, 540), (960, 540), (1520, 540)],
        )
        self.assertEqual(result.getpixel((10, 10)), (11, 11, 17))
        self.assertEqual(result.getpixel((960, 160)), (255, 255, 255))
        self.assertEqual(result.getpixel((200, 60)), (5, 128, 245))
        self.assertEqual(main.MATCH_LOGO_RENDER_VERSION, "7")

    def test_live_phalang_logo_hosts_use_larger_source_images(self):
        self.assertIn("thumb.wikimedia.org", main._LOGO_IMAGE_ALLOWED_HOSTS)
        self.assertIn("img0.aiscore.com", main._LOGO_IMAGE_ALLOWED_HOSTS)
        low_resolution = (
            "https://img0.aiscore.com/volleyball/team/"
            "b037a1bf40c26a9e0bad50963dbcb8c4.png!w60"
        )
        self.assertEqual(
            main._logo_asset_candidates(low_resolution),
            (
                "https://img0.aiscore.com/volleyball/team/"
                "b037a1bf40c26a9e0bad50963dbcb8c4.png!w300",
                low_resolution,
            ),
        )
        already_large = low_resolution.replace("!w60", "!w300")
        self.assertEqual(main._logo_asset_candidates(already_large), (already_large,))

        wikimedia_logo = (
            "https://thumb.wikimedia.org/wikipedia/commons/thumb/2/21/"
            "Flag_of_Vietnam.svg/330px-Flag_of_Vietnam.svg.png"
        )
        self.assertEqual(
            main._logo_asset_candidates(wikimedia_logo),
            (
                "https://thumb.wikimedia.org/wikipedia/commons/thumb/2/21/"
                "Flag_of_Vietnam.svg/960px-Flag_of_Vietnam.svg.png",
                wikimedia_logo,
            ),
        )

    def test_match_logo_png_uses_initials_when_source_logos_are_missing(self):
        with patch.object(main, "_download_logo_asset") as fetch:
            response = main.app.test_client().get(
                "/match-logo.png?home_name=Home+FC&league_name=MLS&away_name=Galaxy"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        fetch.assert_not_called()

    def test_transient_logo_failure_is_retried_after_short_negative_cache(self):
        url = (
            "https://imgts.sportpulseapiz.com/football/team/transient/image/small"
        )
        now = [100.0]
        with main._logo_asset_cache_lock:
            main._logo_asset_cache.clear()
        try:
            with (
                patch.object(
                    main,
                    "_fetch_logo_asset",
                    side_effect=[b"", b"recovered-logo"],
                ) as fetch,
                patch.object(main.time, "monotonic", side_effect=lambda: now[0]),
            ):
                self.assertEqual(main._download_logo_asset(url), b"")
                self.assertEqual(main._download_logo_asset(url), b"")
                fetch.assert_called_once()
                self.assertEqual(fetch.call_args.args[0], url)

                now[0] += main._LOGO_IMAGE_NEGATIVE_CACHE_TTL_SECONDS + 1
                self.assertEqual(
                    main._download_logo_asset(url),
                    b"recovered-logo",
                )
                self.assertEqual(fetch.call_count, 2)
        finally:
            with main._logo_asset_cache_lock:
                main._logo_asset_cache.clear()

    def test_logo_fetch_uses_shared_session_and_provider_referer(self):
        response = MagicMock()
        response.status_code = 200
        response.headers = {
            "Content-Type": "image/png",
            "Content-Length": "4",
        }
        response.iter_content.return_value = [b"logo"]
        response.__enter__.return_value = response
        url = (
            "https://imgts.sportpulseapiz.com/football/team/logo/image/small"
        )

        with patch.object(
            main._http_session,
            "get",
            return_value=response,
        ) as get:
            self.assertEqual(main._fetch_logo_asset(url), b"logo")

        headers = get.call_args.kwargs["headers"]
        self.assertEqual(
            headers["Referer"],
            f"{main.PHALANG_FRONTEND_URL.rstrip('/')}/",
        )
        self.assertIn("Chrome", headers["User-Agent"])

    def test_match_logo_png_rejects_unapproved_asset_hosts(self):
        self.assertEqual(
            main._download_logo_asset("https://not-approved.example/logo.png"),
            b"",
        )

    def test_cached_match_logo_urls_migrate_and_invalidate_old_images(self):
        cached = '#EXTINF:-1 tvg-logo="https://iptv.example/match-logo.svg?home=x",Match\n'
        repaired = main._repair_match_logo_urls(cached)
        self.assertIn("/match-logo.png?", repaired)
        self.assertNotIn("/match-logo.svg?", repaired)
        self.assertIn("home=x&v=7", repaired)

        old_png = (
            '#EXTINF:-1 tvg-logo="https://iptv.example/match-logo.png'
            '?home=x&v=4",Match\n'
        )
        repaired_png = main._repair_match_logo_urls(old_png)
        self.assertIn("home=x&v=7", repaired_png)
        self.assertNotIn("v=4", repaired_png)

    def test_match_logo_svg_embeds_the_three_logos_in_order(self):
        response = main.app.test_client().get(
            "/match-logo.svg?home=https%3A%2F%2Fassets.example%2Fhome.png"
            "&league=https%3A%2F%2Fassets.example%2Fleague.png"
            "&away=https%3A%2F%2Fassets.example%2Faway.png"
            "&home_name=Home&league_name=MLS&away_name=Away"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        svg = response.get_data(as_text=True)
        self.assertEqual(svg.count("<image "), 3)
        self.assertIn("https://assets.example/home.png", svg)
        self.assertIn("https://assets.example/league.png", svg)
        self.assertIn("https://assets.example/away.png", svg)
        self.assertNotIn('stroke="#ffffff"', svg)
        self.assertNotIn('stroke-opacity=".12"', svg)
        self.assertIn('<rect width="960" height="320" fill="#0b0b10"/>', svg)
        self.assertIn('<rect x="18" y="14" width="924" height="292" rx="36" fill="#ffffff"/>', svg)
        self.assertIn('fill="#05cbc1"', svg)

    def test_legacy_khan_dai_cache_entries_and_streams_are_removed(self):
        legacy = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="Khán Đài TV",Match\n'
            "#EXTVLCOPT:http-user-agent=Example\n"
            "https://old.example/stream.m3u8\n"
            '#EXTINF:-1 group-title="Giờ Vàng TV",Keep this\n'
            "https://new.example/stream.m3u8\n"
        )
        cleaned = main._strip_removed_groups(legacy)
        self.assertNotIn("Khán Đài", cleaned)
        self.assertNotIn("old.example", cleaned)
        self.assertIn("Keep this", cleaned)
        self.assertIn("new.example", cleaned)

    def test_legacy_combined_cache_is_scrubbed_before_hydration(self):
        legacy = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="Khán Đài TV",Old event\n'
            "https://old.example/stream.m3u8\n"
            '#EXTINF:-1 tvg-logo="https://iptv.example/match-logo.svg?home=x" group-title="Giờ Vàng TV",Keep this\n'
            "https://new.example/stream.m3u8\n"
        )
        main._kv_hydrated_keys.discard("combined")
        try:
            with patch.object(main, "_cloudflare_kv_enabled", return_value=True), \
                    patch.object(main, "_cloudflare_kv_get", return_value=(legacy, 123.0)), \
                    patch.object(main, "_cloudflare_kv_put") as put:
                self.assertTrue(main._hydrate_from_cloudflare_kv("combined"))

            cached = main._get_entry("combined")["content"].decode("utf-8")
            self.assertNotIn("Khán Đài", cached)
            self.assertNotIn("old.example", cached)
            self.assertIn("Keep this", cached)
            self.assertIn("/match-logo.png?", cached)
            self.assertNotIn("/match-logo.svg?", cached)
            put.assert_called_once_with("combined", cached, 123.0)
        finally:
            main._kv_hydrated_keys.discard("combined")


if __name__ == "__main__":
    unittest.main()