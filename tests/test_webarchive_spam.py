import unittest
from datetime import datetime, timezone
from itertools import permutations
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from webarchive_spam import (
    WaybackSnapshot,
    WebArchivePageContent,
    WebArchiveScriptObservation,
    WebArchiveSpamResult,
    archive_placeholder_reason,
    check_webarchive_spam,
    extract_archive_text,
    fetch_wayback_snapshots,
    scan_script_counts,
    scan_wayback_text,
    select_locale_samples,
    webarchive_script_spam_result,
)


class WebArchiveSpamTests(unittest.TestCase):
    def test_detects_obvious_spam_topics(self):
        matches = scan_wayback_text(
            "Best online casino bonus and 1xbet sportsbook. Buy cheap viagra online.",
            locale="DE",
        )
        categories = {match.category for match in matches}
        self.assertIn("casino/betting", categories)
        self.assertIn("pharma", categories)

    def test_detects_indonesian_gambling_archive_spam(self):
        matches = scan_wayback_text(
            "SBOBET WAP solusi taruhan online. DAFTAR LOGIN, judi online, "
            "slot gacor, togel online, live casino, agen sbobet resmi.",
            locale="ES",
        )
        categories = {match.category for match in matches}
        self.assertIn("multilingual gambling", categories)

    def test_detects_cakeptogel_deposit_pulsa_archive_spam(self):
        matches = scan_wayback_text(
            "CAKEPTOGEL >> Situs Slot Main Pakai Deposit Pulsa Indosat Terbaru Gampang Menang.",
            locale="US",
        )
        categories = {match.category for match in matches}
        self.assertIn("english spam words", categories)
        self.assertIn("multilingual gambling", categories)

    def test_detects_german_erotic_archive_reuse(self):
        matches = scan_wayback_text(
            "Light patterns projected on woman naked body. Belebung Ihrer sexuellen Verbindung. "
            "Wenn Sie sexuelle Fantasien haben, besuchen Sie erotikads.ch. "
            "Date-Nights mit einer unerwarteten erotischen Massage und Sexspielzeugen.",
            locale="DE",
        )
        categories = {match.category for match in matches}
        self.assertIn("adult/erotic content", categories)

    def test_detects_multilingual_gambling_and_pharma_phrases(self):
        gambling_categories = {
            match.category
            for match in scan_wayback_text(
                "Nhà cái cá cược bóng đá, casino en línea y zaklady bukmacherskie.",
                locale="DE",
            )
        }
        self.assertIn("multilingual gambling", gambling_categories)
        pharma_categories = {
            match.category
            for match in scan_wayback_text(
                "Farmacia online: comprar viagra sin receta. Apotheke rezeptfrei cialis kaufen.",
                locale="DE",
            )
        }
        self.assertIn("multilingual pharma", pharma_categories)

    def test_detects_japanese_sidejob_investment_archive_spam(self):
        matches = scan_wayback_text(
            "おすすめネット副業！最新投資法解説レビューまとめ。"
            "ドロップのエンジェルツールはどんな副業？その内容とは！最新レビュー。"
            "株式会社DROPの口コミ評判、ネット副業は詐欺に遭いやすいことがデメリット。"
            "ビットコインの価格が急騰した理由を解説します。",
            locale="CH",
        )
        categories = {match.category for match in matches}
        self.assertIn("japanese sidejob/investment spam", categories)

    def test_detects_german_krypto_signale_archive_spam(self):
        matches = scan_wayback_text(
            "Kategorie Krypto Signale. Bitcoin trading investment signals und aktuelle Krypto-Signale.",
            locale="NL",
        )
        categories = {match.category for match in matches}
        self.assertIn("crypto/nft", categories)

    def test_detects_branded_coin_rewards_archive_spam(self):
        matches = scan_wayback_text(
            "EthiCoin magyar startup. Az EthiCoin credittekkel vásárolható, "
            "az app használatáért rewards járnak és minden séta után EthiCoin kapsz.",
            locale="HU",
        )
        categories = {match.category for match in matches}
        self.assertIn("crypto/nft", categories)

    def test_cjk_reuse_is_ignored_for_cjk_locale_only(self):
        text = "日本語のテキストです。" * 80
        self.assertFalse(scan_wayback_text(text, locale="JP"))
        categories = {match.category for match in scan_wayback_text(text, locale="DE")}
        self.assertIn("chinese characters", categories)
        self.assertIn("japanese characters", categories)

    def test_script_filter_uses_absolute_20_char_threshold_without_ratio(self):
        text = ("safe latin content " * 400) + ("\u3042" * 20)
        categories = {match.category for match in scan_wayback_text(text, locale="DE")}
        self.assertIn("japanese characters", categories)

    def test_detects_bengali_thai_and_hindi_scripts_outside_matching_locale(self):
        bengali = "বাংলা টেক্সট " * 80
        thai = "ภาษาไทย " * 90
        hindi = "हिन्दी भाषा " * 90
        self.assertIn("bengali characters", {m.category for m in scan_wayback_text(bengali, locale="DE")})
        self.assertIn("thai characters", {m.category for m in scan_wayback_text(thai, locale="DE")})
        self.assertIn("hindi/devanagari", {m.category for m in scan_wayback_text(hindi, locale="DE")})
        self.assertFalse(scan_wayback_text(thai, locale="TH"))
        self.assertFalse(scan_wayback_text(hindi, locale="IN"))

    def test_detects_russian_spam_words_and_custom_words(self):
        categories = {
            match.category
            for match in scan_wayback_text("Лучшее онлайн казино и купить ссылки дешево", locale="DE")
        }
        self.assertIn("russian spam words", categories)
        with patch.dict("os.environ", {"WEBARCHIVE_CUSTOM_SPAM_WORDS": "bad custom phrase"}, clear=False):
            categories = {
                match.category
                for match in scan_wayback_text("This page has bad custom phrase inside.", locale="DE")
            }
        self.assertIn("custom spam words", categories)

    def test_extract_archive_text_skips_script_noise(self):
        title, text = extract_archive_text(
            "<html><head><title>Old Casino</title><script>viagra casino</script></head>"
            "<body><main>Normal text</main></body></html>"
        )
        self.assertEqual(title, "Old Casino")
        self.assertIn("Normal text", text)
        self.assertNotIn("viagra casino", text)

    def test_cdx_timeout_is_retried_with_cap(self):
        latest_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20260101000000","http://example.de/","text/html","200","abc"]]'
        )
        range_payload = latest_payload
        with (
            patch("webarchive_spam._domain_variants", return_value=["http://example.de/"]),
            patch("webarchive_spam._open_text", side_effect=[latest_payload, TimeoutError("slow"), range_payload]) as open_text,
            patch("webarchive_spam.time.sleep") as sleep,
        ):
            snapshots, errors = fetch_wayback_snapshots(
                "example.de",
                years=5,
                max_snapshots=3,
                timeout=1,
                retries=1,
            )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].timestamp, "20260101000000")
        self.assertEqual(errors, [])
        self.assertEqual(open_text.call_count, 3)
        sleep.assert_called_once()

    def test_archive_discovery_uses_two_life_windows_not_current_year(self):
        latest_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20181112000000","http://old.example/","warc/revisit","-","latest"]]'
        )
        range_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20181112000000","http://old.example/","warc/revisit","-","latest"],'
            '["20140801000000","http://old.example/","text/html","200","old"],'
            '["20110101000000","http://old.example/","text/html","200","too-old"]]'
        )
        urls = []

        def fake_open_text(url, timeout, max_bytes=1_000_000):
            urls.append(url)
            return latest_payload if len(urls) == 1 else range_payload

        with (
            patch("webarchive_spam._domain_variants", return_value=["http://old.example/"]),
            patch("webarchive_spam._open_text", side_effect=fake_open_text),
            patch("webarchive_spam.time.sleep"),
        ):
            snapshots, errors = fetch_wayback_snapshots(
                "old.example",
                years=5,
                max_snapshots=5,
                timeout=1,
                retries=0,
                now=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [snapshot.timestamp for snapshot in snapshots],
            ["20181112000000", "20140801000000", "20110101000000"],
        )
        self.assertEqual(len(urls), 2)
        query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(query.get("from"), ["20081112"])
        self.assertEqual(query.get("to"), ["20181112"])

    def test_cdx_collects_each_variant_with_its_own_domain_life_window(self):
        header_only = '[["timestamp","original","mimetype","statuscode","digest"]]'
        bare_latest = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","http://helliad.com/","text/html","200","parked-latest"]]'
        )
        bare_range = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","http://helliad.com/","text/html","200","parked-latest"],'
            '["20240425172539","http://helliad.com/","text/html","200","parked-old"]]'
        )
        www_latest = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20181112000000","http://www.helliad.com/","text/html","200","real-latest"]]'
        )
        www_range = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20181112000000","http://www.helliad.com/","text/html","200","real-latest"],'
            '["20140801000000","http://www.helliad.com/","text/html","200","real-old"]]'
        )
        urls = []

        def fake_open_text(url, timeout, max_bytes=1_000_000):
            urls.append(url)
            query = parse_qs(urlparse(url).query)
            original = query["url"][0]
            is_latest = query.get("sort") == ["reverse"]
            if original == "http://helliad.com/":
                return bare_latest if is_latest else bare_range
            if original == "http://www.helliad.com/":
                return www_latest if is_latest else www_range
            return header_only

        with (
            patch(
                "webarchive_spam._domain_variants",
                return_value=[
                    "http://helliad.com/",
                    "https://helliad.com/",
                    "http://www.helliad.com/",
                    "https://www.helliad.com/",
                ],
            ),
            patch("webarchive_spam._open_text", side_effect=fake_open_text),
            patch("webarchive_spam.time.sleep"),
        ):
            snapshots, errors = fetch_wayback_snapshots(
                "helliad.com",
                years=5,
                max_snapshots=8,
                timeout=1,
                retries=0,
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [snapshot.timestamp for snapshot in snapshots],
            ["20250908070304", "20240425172539", "20181112000000", "20140801000000"],
        )
        self.assertEqual(len(urls), 6)
        range_queries = [
            parse_qs(urlparse(url).query)
            for url in urls
            if parse_qs(urlparse(url).query).get("sort") != ["reverse"]
        ]
        windows = {
            query["url"][0]: (query.get("from", [""])[0], query.get("to", [""])[0])
            for query in range_queries
        }
        self.assertEqual(windows["http://helliad.com/"], ("20150908", "20250908"))
        self.assertEqual(windows["http://www.helliad.com/"], ("20081112", "20181112"))

    def test_same_variant_late_parking_does_not_hide_prior_real_site_era(self):
        latest_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","https://example.org/","text/html","200","parking"]]'
        )
        range_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","https://example.org/","text/html","200","parking"],'
            '["20181201000000","https://example.org/","text/html","200","real-3"],'
            '["20170601000000","https://example.org/","text/html","200","real-2"],'
            '["20160701000000","https://example.org/","text/html","200","real-1"],'
            '["20140101000000","https://example.org/","text/html","200","outside-guard"]]'
        )
        urls = []

        def fake_open_text(url, timeout, max_bytes=1_000_000):
            urls.append(url)
            query = parse_qs(urlparse(url).query)
            return latest_payload if query.get("sort") == ["reverse"] else range_payload

        with (
            patch("webarchive_spam._domain_variants", return_value=["https://example.org/"]),
            patch("webarchive_spam._open_text", side_effect=fake_open_text),
            patch("webarchive_spam.time.sleep"),
        ):
            snapshots, errors = fetch_wayback_snapshots(
                "example.org",
                years=5,
                max_snapshots=8,
                timeout=1,
                retries=0,
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [snapshot.timestamp for snapshot in snapshots],
            ["20250908070304", "20181201000000", "20170601000000", "20160701000000"],
        )
        range_query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(range_query.get("from"), ["20150908"])
        self.assertEqual(range_query.get("to"), ["20250908"])

        real_text = (
            "Example Foundation operates a community arts centre in Bristol, England. "
            "Contact the local organisation for workshops, events and membership. "
        ) * 8
        pages = [
            WebArchivePageContent(
                snapshot=snapshot,
                title=("Domain for sale" if snapshot.timestamp.startswith("2025") else "Example Foundation"),
                text=(
                    "This domain name is for sale. Buy this domain at Afternic. " * 10
                    if snapshot.timestamp.startswith("2025")
                    else real_text
                ),
            )
            for snapshot in snapshots
        ]
        samples = select_locale_samples(pages, "example.org")
        self.assertTrue(samples)
        self.assertEqual(samples[0].life_start, "2016")
        self.assertEqual(samples[0].life_end, "2018")
        self.assertEqual(samples[0].supporting_snapshots, 3)
        self.assertTrue(all(not sample.timestamp.startswith("2025") for sample in samples))

    def test_locale_sample_ignores_latest_parking_and_uses_stable_site_life(self):
        real_text = (
            "Project Zero Deaths is an English multiplayer action game for Steam, iOS and Android. "
            "Play worldwide with friends. Developer and publisher UAB Detis, Kaunas, Lithuania. "
            "Contact support@example.com. "
        ) * 5
        pages = [
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20190801000000", "https://projectzerodeaths.com/"),
                title="Project Zero Deaths | Multiplayer Game",
                text=real_text,
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20200801000000", "https://projectzerodeaths.com/"),
                title="Project Zero Deaths | Multiplayer Game",
                text=real_text + " Download the game today.",
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20250501000000", "https://projectzerodeaths.com/"),
                title="projectzerodeaths.com is for sale",
                text="This domain name is for sale. Buy this domain at Afternic. " * 20,
            ),
        ]

        samples = select_locale_samples(pages, "projectzerodeaths.com")

        self.assertTrue(samples)
        self.assertNotIn("for sale", samples[0].title.lower())
        self.assertEqual(samples[0].life_start, "2019")
        self.assertEqual(samples[0].life_end, "2020")
        self.assertEqual(samples[0].confidence, "MEDIUM")
        self.assertIn("UAB Detis", samples[0].excerpt)

    def test_locale_sample_is_empty_when_archive_contains_only_placeholders(self):
        pages = [
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20240101000000", "https://example.com/"),
                title="Coming soon",
                text="Our website is under construction. " * 30,
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20250101000000", "https://example.com/"),
                title="Domain for sale",
                text="This domain name is for sale. Buy this domain. " * 30,
            ),
        ]
        self.assertEqual(select_locale_samples(pages, "example.com"), [])

    def test_locale_sample_is_deterministic_and_generic_home_does_not_merge_eras(self):
        original_text = (
            "English community theatre in Bristol. Contact the organisers and visit our local venue. "
            "The company performs plays, workshops and public events for residents. "
        ) * 8
        pages = [
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20180501000000", "http://www.example.org/"),
                title="Home",
                text=original_text,
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20190501000000", "https://www.example.org/"),
                title="Home",
                text=original_text + "Tickets are available from the theatre office.",
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20240501000000", "https://example.org/"),
                title="Home",
                text=(
                    "Lietuvos verslo paslaugos. UAB Nauja Era, Vilnius, Lithuania. "
                    "Kontaktai, adresas ir telefono numeris pateikiami klientams. "
                ) * 12,
            ),
        ]

        selections = []
        for ordering in permutations(pages):
            samples = select_locale_samples(list(ordering), "example.org")
            selections.append(
                [(sample.timestamp, sample.life_start, sample.life_end) for sample in samples]
            )

        self.assertTrue(selections[0])
        self.assertTrue(all(selection == selections[0] for selection in selections))
        self.assertEqual(selections[0][0][1:], ("2018", "2019"))
        self.assertTrue(all(not timestamp.startswith("2024") for timestamp, _start, _end in selections[0]))

    def test_locale_sample_does_not_bridge_a_long_temporal_gap_by_title_alone(self):
        shared_text = (
            "Independent local arts association with workshops, events and member contacts. "
        ) * 10
        pages = [
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20100101000000", "http://example.org/"),
                title="Example Arts Association",
                text=shared_text,
            ),
            WebArchivePageContent(
                snapshot=WaybackSnapshot("20200101000000", "https://example.org/"),
                title="Example Arts Association",
                text=shared_text,
            ),
        ]

        samples = select_locale_samples(pages, "example.org")

        self.assertEqual(samples[0].supporting_snapshots, 1)
        self.assertEqual(samples[0].life_start, "2010")
        self.assertEqual(samples[0].life_end, "2010")

    def test_placeholder_detection_keeps_long_article_and_short_contact_card(self):
        article = (
            "This troubleshooting guide explains why a remote dependency can return "
            "service unavailable and access denied messages, with recovery steps. "
        ) * 20
        contact_card = (
            "UAB Tikras Projektas, Gedimino g. 10, Vilnius, Lithuania. "
            "+370 600 12345, info@tikras.lt"
        )

        self.assertEqual(archive_placeholder_reason("Incident response guide", article), "")
        self.assertEqual(archive_placeholder_reason("Contact", contact_card), "")
        self.assertEqual(
            archive_placeholder_reason("Domain for sale", "Buy this domain at Afternic."),
            "parking",
        )

    def test_title_is_not_duplicated_in_visible_text_or_script_counts(self):
        japanese_title = "\u3042" * 10
        title, text = extract_archive_text(
            f"<html><head><title>{japanese_title}</title></head>"
            "<body>Ordinary Latin body content for a legitimate site.</body></html>"
        )

        self.assertEqual(title, japanese_title)
        self.assertNotIn(japanese_title, text)
        counts = scan_script_counts(f"{title} {text}")
        self.assertEqual(counts["japanese characters"], 10)
        self.assertNotIn(
            "japanese characters",
            {match.category for match in scan_wayback_text(f"{title} {text}", locale="DE")},
        )

    def test_one_success_from_many_snapshots_is_not_a_clean_archive_check(self):
        snapshots = [
            WaybackSnapshot(f"202{i}0101000000", "http://example.org/")
            for i in range(1, 4)
        ]

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot == snapshots[0]:
                return snapshot, "Example", "Clean ordinary organisation content " * 20, ""
            return snapshot, "", "", f"{snapshot.display_month}: TimeoutError"

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(snapshots, [])),
            patch("webarchive_spam.fetch_wayback_snapshot_text", side_effect=fake_snapshot_text),
        ):
            result = check_webarchive_spam("example.org", max_workers=3)

        self.assertFalse(result.checked)
        self.assertFalse(result.spam)
        self.assertEqual(result.snapshots_checked, 1)
        self.assertTrue(any("incomplete usable snapshot coverage" in error for error in result.errors))

    def test_two_successes_from_large_snapshot_set_are_not_clean_coverage(self):
        snapshots = [
            WaybackSnapshot(f"202{i}0101000000", "http://example.org/")
            for i in range(1, 7)
        ]

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot in snapshots[:2]:
                return snapshot, "Example", "Clean organisation content " * 30, ""
            return snapshot, "", "", f"{snapshot.display_month}: TimeoutError"

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(snapshots, [])),
            patch("webarchive_spam.fetch_wayback_snapshot_text", side_effect=fake_snapshot_text),
        ):
            result = check_webarchive_spam("example.org", max_workers=4)

        self.assertFalse(result.checked)
        self.assertEqual(result.snapshots_checked, 2)
        self.assertTrue(any("incomplete usable snapshot coverage" in error for error in result.errors))

    def test_discovery_only_spam_outside_configured_life_window_is_not_scanned(self):
        snapshots = [
            WaybackSnapshot("20250101000000", "https://example.org/"),
            WaybackSnapshot("20240101000000", "https://example.org/"),
            WaybackSnapshot("20160101000000", "https://example.org/"),
        ]

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot.timestamp.startswith("2016"):
                return snapshot, "Casino takeover", "online casino viagra betting " * 30, ""
            return (
                snapshot,
                "Example Community",
                "Example Community runs local workshops, events and public services. " * 20,
                "",
            )

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(snapshots, [])),
            patch("webarchive_spam.fetch_wayback_snapshot_text", side_effect=fake_snapshot_text),
        ):
            result = check_webarchive_spam(
                "example.org",
                years=5,
                max_snapshots=6,
                max_workers=3,
                scan_scripts=False,
            )

        self.assertTrue(result.checked)
        self.assertFalse(result.spam)
        self.assertEqual(result.snapshots_checked, 2)
        self.assertTrue(result.locale_samples)
        self.assertEqual(result.locale_samples[0].life_start, "2024")
        self.assertEqual(result.locale_samples[0].life_end, "2025")

    def test_late_parking_triggers_exact_real_life_window_without_exceeding_html_budget(self):
        discovery = [
            WaybackSnapshot("20250908070304", "https://example.org/"),
            WaybackSnapshot("20181201000000", "https://example.org/"),
            WaybackSnapshot("20170601000000", "https://example.org/"),
            WaybackSnapshot("20160701000000", "https://example.org/"),
        ]
        exact_window = [
            WaybackSnapshot("20181201000000", "https://example.org/"),
            WaybackSnapshot("20170601000000", "https://example.org/"),
            WaybackSnapshot("20160701000000", "https://example.org/"),
            WaybackSnapshot("20140101000000", "https://example.org/"),
        ]
        real_text = (
            "Example Foundation operates a community centre in Bristol, England. "
            "Contact the organisation for workshops, events and membership. "
        ) * 15

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot.timestamp.startswith("2025"):
                return snapshot, "Domain for sale", "Buy this domain at Afternic. casino " * 30, ""
            if snapshot.timestamp.startswith("2014"):
                return snapshot, "Casino takeover", "online casino viagra betting " * 30, ""
            return snapshot, "Example Foundation", real_text, ""

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(discovery, [])),
            patch(
                "webarchive_spam._fetch_wayback_window_snapshots",
                return_value=(exact_window, []),
            ) as exact_fetch,
            patch(
                "webarchive_spam.fetch_wayback_snapshot_text",
                side_effect=fake_snapshot_text,
            ) as html_fetch,
        ):
            result = check_webarchive_spam(
                "example.org",
                years=5,
                max_snapshots=8,
                max_workers=4,
                scan_scripts=False,
            )

        self.assertTrue(result.checked)
        self.assertTrue(result.spam)
        self.assertEqual([hit.timestamp for hit in result.hits], ["20140101000000"])
        self.assertLessEqual(html_fetch.call_count, 8)
        exact_fetch.assert_called_once()
        kwargs = exact_fetch.call_args.kwargs
        self.assertEqual(kwargs["from_stamp"], "20131201")
        self.assertEqual(kwargs["to_stamp"], "20181201")

    def test_failed_required_exact_life_window_cannot_be_called_clean(self):
        discovery = [
            WaybackSnapshot("20250908070304", "https://example.org/"),
            WaybackSnapshot("20181201000000", "https://example.org/"),
            WaybackSnapshot("20170601000000", "https://example.org/"),
            WaybackSnapshot("20160701000000", "https://example.org/"),
        ]
        real_text = "Example Foundation community workshops and local events. " * 30

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot.timestamp.startswith("2025"):
                return snapshot, "Domain for sale", "Buy this domain at Afternic.", ""
            return snapshot, "Example Foundation", real_text, ""

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(discovery, [])),
            patch(
                "webarchive_spam._fetch_wayback_window_snapshots",
                return_value=([], ["CDX policy window https://example.org/: TimeoutError"]),
            ),
            patch("webarchive_spam.fetch_wayback_snapshot_text", side_effect=fake_snapshot_text),
        ):
            result = check_webarchive_spam(
                "example.org",
                years=5,
                max_snapshots=8,
                max_workers=4,
                scan_scripts=False,
            )

        self.assertFalse(result.checked)
        self.assertFalse(result.no_life_found)
        self.assertTrue(any("policy window" in error for error in result.errors))

    def test_all_successful_placeholders_are_no_life_not_clean(self):
        snapshots = [
            WaybackSnapshot(f"202{i}0101000000", "https://example.org/")
            for i in range(1, 4)
        ]
        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(snapshots, [])),
            patch(
                "webarchive_spam.fetch_wayback_snapshot_text",
                side_effect=lambda snapshot, *args: (
                    snapshot,
                    "Domain for sale",
                    "This domain name is for sale. Buy this domain at Afternic.",
                    "",
                ),
            ),
        ):
            result = check_webarchive_spam("example.org", max_workers=3)

        self.assertFalse(result.checked)
        self.assertFalse(result.spam)
        self.assertTrue(result.no_life_found)
        self.assertEqual(result.snapshots_checked, 0)
        self.assertEqual(result.errors, [])

    def test_placeholders_plus_failed_possible_life_are_not_no_life_or_clean(self):
        snapshots = [
            WaybackSnapshot("20230101000000", "https://example.org/"),
            WaybackSnapshot("20220101000000", "https://example.org/"),
        ]

        def fake_snapshot_text(snapshot, timeout=8, max_chars=8000, retries=1):
            if snapshot.timestamp.startswith("2023"):
                return snapshot, "Domain for sale", "Buy this domain at Afternic.", ""
            return snapshot, "", "", "2022-01: TimeoutError"

        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=(snapshots, [])),
            patch("webarchive_spam.fetch_wayback_snapshot_text", side_effect=fake_snapshot_text),
        ):
            result = check_webarchive_spam("example.org", max_snapshots=4, max_workers=2)

        self.assertFalse(result.checked)
        self.assertFalse(result.spam)
        self.assertFalse(result.no_life_found)
        self.assertIn("2022-01: TimeoutError", result.errors)

    def test_one_success_from_one_snapshot_is_a_valid_clean_archive_check(self):
        snapshot = WaybackSnapshot("20230101000000", "http://example.org/")
        with (
            patch("webarchive_spam.fetch_wayback_snapshots", return_value=([snapshot], [])),
            patch(
                "webarchive_spam.fetch_wayback_snapshot_text",
                return_value=(snapshot, "Example", "Clean ordinary organisation content " * 20, ""),
            ),
        ):
            result = check_webarchive_spam("example.org", max_workers=1)

        self.assertTrue(result.checked)
        self.assertFalse(result.spam)
        self.assertEqual(result.snapshots_checked, 1)

    def test_script_mismatch_is_deferred_until_final_locale(self):
        japanese_text = "\u3042" * 25
        result = WebArchiveSpamResult(
            checked=True,
            spam=False,
            snapshots_found=1,
            snapshots_checked=1,
            script_observations=[
                WebArchiveScriptObservation(
                    timestamp="20220101000000",
                    original="https://example.com/",
                    title="Example",
                    counts=scan_script_counts(japanese_text),
                )
            ],
        )

        self.assertIsNone(webarchive_script_spam_result(result, "JP"))
        rejected = webarchive_script_spam_result(result, "DE")
        self.assertIsNotNone(rejected)
        self.assertTrue(rejected.spam)
        self.assertIn("japanese characters", {m.category for m in rejected.hits[0].matches})


if __name__ == "__main__":
    unittest.main()
