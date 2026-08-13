import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from webarchive_spam import extract_archive_text, fetch_wayback_snapshots, scan_wayback_text


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

    def test_archive_window_uses_last_domain_life_years_not_current_year(self):
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
        self.assertEqual([snapshot.timestamp for snapshot in snapshots], ["20181112000000", "20140801000000"])
        self.assertEqual(len(urls), 2)
        query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(query.get("from"), ["20131112"])
        self.assertEqual(query.get("to"), ["20181112"])

    def test_cdx_stops_after_first_working_domain_variant(self):
        latest_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","https://helliad.com/","text/html","200","latest"]]'
        )
        range_payload = (
            '[["timestamp","original","mimetype","statuscode","digest"],'
            '["20250908070304","https://helliad.com/","text/html","200","latest"],'
            '["20250425172539","https://helliad.com/","text/html","200","old"]]'
        )
        urls = []

        def fake_open_text(url, timeout, max_bytes=1_000_000):
            urls.append(url)
            self.assertIn("http%3A%2F%2Fhelliad.com%2F", url)
            return latest_payload if len(urls) == 1 else range_payload

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
                max_snapshots=5,
                timeout=1,
                retries=0,
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(len(urls), 2)


if __name__ == "__main__":
    unittest.main()
