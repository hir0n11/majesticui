import unittest
from urllib.parse import parse_qs, urlparse

from majestic_reports import (
    PAGES_JS,
    _backlinks_table_signature,
    _dofollow_is_active,
    _is_login_page,
    build_anchor_url,
    build_backlinks_url,
    build_pages_url,
    collect_backlinks,
)


class MajesticUrlTests(unittest.TestCase):
    def test_backlinks_url_has_required_filters(self):
        query = parse_qs(urlparse(build_backlinks_url("drop.lt", 50)).query)
        self.assertEqual(query["IndexDataSource"], ["F"])
        self.assertEqual(query["MaxSourceUrlsPerRefDomain"], ["1"])
        self.assertEqual(query["removeDeleted"], ["0"])
        self.assertEqual(query["s"], ["50"])

    def test_backlinks_collector_keeps_deleted_but_skips_nofollow(self):
        rows = [
            {
                "source_url": "https://donor.example/page",
                "target_url": "https://drop.lt/",
                "deleted": True,
                "nofollow": False,
            },
            {
                "source_url": "https://nofollow.example/page",
                "target_url": "https://drop.lt/",
                "deleted": False,
                "nofollow": True,
            },
        ]
        with unittest.mock.patch(
            "majestic_reports._collect_paginated",
            return_value={"rows": rows, "truncated": False, "pages": 1},
        ):
            result = collect_backlinks(object(), "drop.lt")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["source_domain"], "donor.example")
        self.assertEqual(result["filters"]["deleted"], "Included")

    def test_anchor_urls_select_both_indexes(self):
        fresh = parse_qs(urlparse(build_anchor_url("drop.lt", "F")).query)
        historic = parse_qs(urlparse(build_anchor_url("drop.lt", "H")).query)
        self.assertEqual(fresh["IndexDataSource"], ["F"])
        self.assertEqual(historic["IndexDataSource"], ["H"])

    def test_invalid_anchor_index_rejected(self):
        with self.assertRaises(ValueError):
            build_anchor_url("drop.lt", "X")

    def test_pages_url_selects_historic_index(self):
        query = parse_qs(urlparse(build_pages_url("drop.lt", "H", 50)).query)
        self.assertEqual(query["IndexDataSource"], ["H"])
        self.assertEqual(query["scope"], ["domain"])
        self.assertEqual(query["s"], ["50"])

    def test_backlink_signature_script_has_no_escaped_string_literal_bug(self):
        class Driver:
            script = ""

            def execute_script(self, script):
                self.script = script
                return "source|target"

        driver = Driver()
        self.assertEqual(_backlinks_table_signature(driver), "source|target")
        self.assertIn("String.fromCharCode(10)", driver.script)
        self.assertNotIn(".join('\n')", driver.script)

    def test_backlinks_parser_uses_last_numeric_outbound_value_for_external(self):
        from majestic_reports import BACKLINKS_JS

        self.assertIn("outboundNumbers[outboundNumbers.length - 1]", BACKLINKS_JS)

    def test_current_filter_tag_is_recognized_as_active_dofollow(self):
        class Element:
            text = "Follow (DoFollow)"

            def is_displayed(self):
                return True

        class Driver:
            def find_elements(self, by, selector):
                if "Follow (DoFollow)" in selector:
                    return [Element()]
                return []

        self.assertTrue(_dofollow_is_active(Driver()))

    def test_pages_parser_targets_top_pages_table(self):
        self.assertIn("#vue-pages-table", PAGES_JS)
        self.assertIn("page_title", PAGES_JS)
        self.assertIn('td[data-split="1"] .aDate', PAGES_JS)

    def test_silent_free_trial_page_is_treated_as_logged_out(self):
        class Driver:
            title = "Majestic.com Backlink Checker - Free Trial"
            page_source = "<html><body>Sign Up for FREE Login</body></html>"

            def find_elements(self, by, selector):
                return []

        self.assertTrue(_is_login_page(Driver()))


if __name__ == "__main__":
    unittest.main()
