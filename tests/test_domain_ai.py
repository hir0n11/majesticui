import unittest
from unittest.mock import patch

from domain_ai import (
    AnchorScreenAssessment,
    ArticleFallbackAssessment,
    CriticalScreenAssessment,
    FirstBatchAssessment,
    LinkBatchAssessment,
    OpenAIDomainChecker,
    aggregate_assessment,
    compact_anchor_payload,
    compact_backlink_batch,
    compact_critical_payload,
    combine_staged_assessments,
    compact_historic_pages_payload,
    collect_article_page_evidence,
    clustered_article_metric,
    extract_article_page_preview,
    extract_explicit_years,
    is_seo_noise_only_reason,
    is_exact_homepage,
    local_backlink_precheck,
    local_domain_name_precheck,
    local_historic_pages_precheck,
    local_source_age_precheck,
    prepare_evidence,
    near_thresholds_for_locale,
    resolve_locale_with_source,
    scan_anchor_hard_stops,
    sanitize_seo_only_batch,
    sanitize_seo_only_anchor,
    sort_backlinks_for_critical,
    thresholds_for_locale,
    unique_backlinks,
)


def make_rows(count: int, domain: str = "drop.lt"):
    return [
        {
            "record_id": f"M{i}",
            "source_domain": f"donor{i}.example",
            "source_url": f"https://donor{i}.example/article-{i}",
            "target_url": f"https://{domain}/" if i % 2 else f"https://{domain}/inner-{i}",
        }
        for i in range(1, count + 1)
    ]


def assessment_for(rows, articles: int = 0, **overrides):
    value = {
        "locale": "LT",
        "language": "lt",
        "topic": "local project",
        "pbn_risk": "CLEAN",
        "pbn_reasons": [],
        "anchor_risk": "CLEAN",
        "anchor_reasons": [],
        "hard_stop_reasons": [],
        "warnings": [],
        "summary": "clean",
        "link_assessments": [
            {
                "record_id": row["record_id"],
                "quality": "QUALITY",
                "link_type": "ARTICLE" if index <= articles else "OTHER",
                "count_quality": True,
                "count_article": index <= articles,
                "prohibited_topic": "NONE",
                "age_signal": "UNKNOWN",
                "reason": "ok",
            }
            for index, row in enumerate(rows, start=1)
        ],
    }
    value.update(overrides)
    return value


class DomainAggregationTests(unittest.TestCase):
    def test_parse_retries_opaque_third_party_bad_request(self):
        class OpaqueBadRequest(Exception):
            status_code = 400
            body = {
                "message": "The request is invalid (request id: test)",
                "type": "invalid_request_error",
                "param": "",
                "code": "bad_request",
            }

        parsed = AnchorScreenAssessment(
            locale="LV",
            language="lv",
            topic="radio",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="clean",
            warnings=[],
        )
        class Responses:
            calls = 0

            @classmethod
            def parse(cls, **kwargs):
                cls.calls += 1
                raise OpaqueBadRequest()

        class ChatCompletions:
            parse_calls = 0
            create_calls = 0

            @classmethod
            def parse(cls, **kwargs):
                cls.parse_calls += 1
                raise OpaqueBadRequest()

            @classmethod
            def create(cls, **kwargs):
                cls.create_calls += 1
                if cls.create_calls < 3:
                    raise OpaqueBadRequest()
                message = type(
                    "Message",
                    (),
                    {"content": parsed.model_dump_json()},
                )()
                choice = type("Choice", (), {"message": message})()
                usage = type("Usage", (), {"prompt_tokens": 12, "completion_tokens": 3})()
                return type("ChatResponse", (), {"choices": [choice], "usage": usage})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type(
            "Client",
            (),
            {
                "responses": Responses(),
                "chat": type("Chat", (), {"completions": ChatCompletions()})(),
            },
        )()
        checker.base_url = "https://arionhub.pro/v1"
        checker.model = "gpt-5.6-terra"
        checker.reasoning_effort = ""
        checker.opaque_bad_request_retries = 2
        checker._model_access_checked = True

        with patch("domain_ai.time.sleep") as sleep_mock:
            value, input_tokens, output_tokens = checker._parse(
                "prompt",
                {"domain": "xradio.lv"},
                AnchorScreenAssessment,
                700,
            )

        self.assertEqual(value, parsed)
        self.assertEqual((input_tokens, output_tokens), (12, 3))
        self.assertEqual(Responses.calls, 1)
        self.assertEqual(ChatCompletions.parse_calls, 1)
        self.assertEqual(ChatCompletions.create_calls, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_parse_does_not_retry_actionable_bad_request(self):
        class ActionableBadRequest(Exception):
            status_code = 400
            body = {
                "message": "Unsupported schema",
                "type": "invalid_request_error",
                "param": "text.format",
                "code": "bad_request",
            }

        class Responses:
            calls = 0

            @classmethod
            def parse(cls, **kwargs):
                cls.calls += 1
                raise ActionableBadRequest()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type("Client", (), {"responses": Responses()})()
        checker.base_url = "https://arionhub.pro/v1"
        checker.model = "gpt-5.6-terra"
        checker.reasoning_effort = ""
        checker.opaque_bad_request_retries = 2
        checker._model_access_checked = True

        with self.assertRaises(ActionableBadRequest), patch("domain_ai.time.sleep") as sleep_mock:
            checker._parse(
                "prompt",
                {"domain": "xradio.lv"},
                AnchorScreenAssessment,
                700,
            )

        self.assertEqual(Responses.calls, 1)
        sleep_mock.assert_not_called()

    def test_parse_falls_back_to_chat_parse_for_opaque_gateway_bad_request(self):
        class OpaqueBadRequest(Exception):
            status_code = 400
            body = {
                "message": "The request is invalid (request id: test)",
                "type": "invalid_request_error",
                "param": "",
                "code": "bad_request",
            }

        parsed = AnchorScreenAssessment(
            locale="LV",
            language="lv",
            topic="radio",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="clean",
            warnings=[],
        )

        class Responses:
            calls = 0

            @classmethod
            def parse(cls, **kwargs):
                cls.calls += 1
                raise OpaqueBadRequest()

        class ChatCompletions:
            calls = 0

            @classmethod
            def parse(cls, **kwargs):
                cls.calls += 1
                message = type("Message", (), {"parsed": parsed})()
                choice = type("Choice", (), {"message": message})()
                usage = type("Usage", (), {"prompt_tokens": 21, "completion_tokens": 5})()
                return type("ChatResponse", (), {"choices": [choice], "usage": usage})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type(
            "Client",
            (),
            {
                "responses": Responses(),
                "chat": type("Chat", (), {"completions": ChatCompletions()})(),
            },
        )()
        checker.base_url = "https://arionhub.pro/v1"
        checker.model = "gpt-5.6-terra"
        checker.reasoning_effort = ""
        checker.opaque_bad_request_retries = 0
        checker._model_access_checked = True

        value, input_tokens, output_tokens = checker._parse(
            "prompt",
            {"domain": "xradio.lv"},
            AnchorScreenAssessment,
            700,
        )

        self.assertIs(value, parsed)
        self.assertEqual((input_tokens, output_tokens), (21, 5))
        self.assertEqual(Responses.calls, 1)
        self.assertEqual(ChatCompletions.calls, 1)

    def test_gateway_does_not_silently_use_luna_for_quality(self):
        class Models:
            @staticmethod
            def list():
                return type("Page", (), {"data": [type("Model", (), {"id": "gpt-5.6-luna"})()]})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type("Client", (), {"models": Models()})()
        checker.base_url = "https://arionhub.pro/v1"
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.5"
        checker.model_notice = ""
        checker._model_access_checked = False
        with self.assertRaises(PermissionError):
            checker._ensure_model_access()
        self.assertEqual(checker.model, "gpt-5.5")
        self.assertFalse(checker._model_access_checked)

    def test_gateway_selects_an_available_sol_variant(self):
        class Models:
            @staticmethod
            def list():
                ids = ["gpt-5.6-luna", "vendor-gpt-5.6-sol"]
                return type("Page", (), {"data": [type("Model", (), {"id": item})() for item in ids]})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type("Client", (), {"models": Models()})()
        checker.base_url = "https://arionhub.pro/v1"
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.6-sol"
        checker.model_notice = ""
        checker._model_access_checked = False
        checker._ensure_model_access()
        self.assertEqual(checker.model, "vendor-gpt-5.6-sol")
        self.assertTrue(checker._model_access_checked)

    def test_gateway_does_not_replace_terra_with_sol(self):
        class Models:
            @staticmethod
            def list():
                ids = ["gpt-5.6-luna", "gpt-5.6-sol"]
                return type("Page", (), {"data": [type("Model", (), {"id": item})() for item in ids]})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type("Client", (), {"models": Models()})()
        checker.base_url = "https://arionhub.pro/v1"
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.6-terra"
        checker.model_notice = ""
        checker._model_access_checked = False
        with self.assertRaises(PermissionError):
            checker._ensure_model_access()
        self.assertEqual(checker.model, "gpt-5.6-terra")
        self.assertFalse(checker._model_access_checked)

    def test_gateway_selects_an_available_terra_variant(self):
        class Models:
            @staticmethod
            def list():
                ids = ["gpt-5.6-luna", "vendor-gpt-5.6-terra"]
                return type("Page", (), {"data": [type("Model", (), {"id": item})() for item in ids]})()

        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = type("Client", (), {"models": Models()})()
        checker.base_url = "https://arionhub.pro/v1"
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.6-terra"
        checker.model_notice = ""
        checker._model_access_checked = False
        checker._ensure_model_access()
        self.assertEqual(checker.model, "vendor-gpt-5.6-terra")
        self.assertTrue(checker._model_access_checked)

    def test_exact_homepage_is_strict(self):
        self.assertTrue(is_exact_homepage("https://www.drop.lt/", "drop.lt"))
        self.assertTrue(is_exact_homepage("/", "drop.lt"))
        self.assertFalse(is_exact_homepage("https://drop.lt/?utm_source=x", "drop.lt"))
        self.assertFalse(is_exact_homepage("https://drop.lt/index", "drop.lt"))
        self.assertFalse(is_exact_homepage("https://drop.lt/index.php", "drop.lt"))
        self.assertFalse(is_exact_homepage("https://drop.lt/lt", "drop.lt"))

    def test_threshold_groups(self):
        self.assertEqual(thresholds_for_locale("EE"), (7, 0))
        self.assertEqual(thresholds_for_locale("PL"), (7, 3))
        self.assertEqual(thresholds_for_locale("DE"), (9, 5))

    def test_locale_resolution_prefers_explicit_override_then_ai_then_tld(self):
        self.assertEqual(resolve_locale_with_source("!PL drops", "example.de", "HU"), ("PL", "OVERRIDE"))
        self.assertEqual(resolve_locale_with_source("locale:PL drops", "example.de", "HU"), ("PL", "OVERRIDE"))
        self.assertEqual(resolve_locale_with_source("PL drops", "example.de", "HU"), ("HU", "AI"))
        self.assertEqual(resolve_locale_with_source("TEST", "example.de", "HU"), ("HU", "AI"))
        self.assertEqual(resolve_locale_with_source("TEST", "example.de", ""), ("DE", "TLD"))
        self.assertEqual(resolve_locale_with_source("TEST", "example.com", ""), ("OTHER", "FALLBACK"))

    def test_domain_name_precheck_rejects_prohibited_topic_before_majestic(self):
        result = local_domain_name_precheck("cialisbuyonlinegf.com", "AT")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:DOMAIN_NAME")
        self.assertEqual(result.early_stop_stage, "local_domain_name")
        self.assertEqual(result.model, "LOCAL_RULES")
        self.assertEqual(local_domain_name_precheck("best-viagra.co.uk", "UK").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("cheapjerseys-nfl.net", "AT").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("cheapoakleyglasses.co.uk", "UK").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("nft-coin-market.net", "AT").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("bitcoin-wallet-example.com", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("buyessayonlinecheap.info", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("2015michaelkorsbags.net", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("20bet-pl.pl", "PL").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("custom-dissertation-writing.com", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("cakeptogel-login.org", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("situs-slot-gacor.net", "US").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("erotikads-example.com", "DE").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("ethicoin.hu", "HU").status, "BAD:DOMAIN_NAME")
        self.assertEqual(local_domain_name_precheck("green-rewardcoin.org", "HU").status, "BAD:DOMAIN_NAME")

    def test_domain_name_precheck_allows_normal_domain(self):
        self.assertIsNone(local_domain_name_precheck("civicalliance.org", "US"))
        self.assertIsNone(local_domain_name_precheck("zukunftskonferenz.live", "DE"))
        self.assertIsNone(local_domain_name_precheck("lecoincreatif.ch", "CH"))
        self.assertIsNone(local_domain_name_precheck("coincreatif.ch", "CH"))
        self.assertIsNone(local_domain_name_precheck("newjerseyarts.org", "US"))
        self.assertIsNone(local_domain_name_precheck("cheapflightsmuseum.org", "US"))
        self.assertIsNone(local_domain_name_precheck("prudentialpropertyspecialists.com", "US"))
        self.assertIsNone(local_domain_name_precheck("definition-lab.org", "US"))
        self.assertIsNone(local_domain_name_precheck("watchtowerhistory.org", "US"))
        self.assertIsNone(local_domain_name_precheck("coachmuseum.org", "US"))
        self.assertIsNone(local_domain_name_precheck("slotcarclub.org", "US"))
        self.assertIsNone(local_domain_name_precheck("sexualhealthresearch.org", "US"))

    def test_strict_pass(self):
        rows = make_rows(7)
        result = aggregate_assessment("drop.lt", "LT", rows, assessment_for(rows))
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.status, "GOOD")
        self.assertEqual(result.unique_quality, 7)
        self.assertEqual(result.locale_source, "AI")

    def test_aggregate_locale_can_be_selected_by_ai_over_tld(self):
        rows = make_rows(7, domain="drop.de")
        data = assessment_for(rows, locale="LT")
        result = aggregate_assessment("drop.de", "TEST", rows, data)
        self.assertEqual(result.locale, "LT")
        self.assertEqual(result.locale_source, "AI")
        self.assertEqual(result.status, "GOOD")

    def test_generic_domain_locale_falls_back_to_austrian_backlink_signals(self):
        rows = make_rows(9, domain="boersianer-gruen.com")
        rows[0]["source_url"] = "https://www.rfu.at/category/unkategorisiert/page/7/"
        rows[1]["source_title"] = "Alexandra Rotter, Wien | Torial"
        data = assessment_for(rows, articles=5, locale="")
        result = aggregate_assessment("boersianer-gruen.com", "GNAME", rows, data)
        self.assertEqual(result.locale, "AT")
        self.assertEqual(result.locale_source, "BACKLINKS")

    def test_generic_domain_locale_falls_back_to_international_english(self):
        rows = make_rows(9, domain="global-review.com")
        for row in rows:
            row["language"] = "EN"
            row["source_domain"] = f"publisher-{row['record_id'].lower()}.com"
            row["source_title"] = "Independent review and international project news"
        data = assessment_for(rows, articles=5, locale="")
        result = aggregate_assessment("global-review.com", "GNAME", rows, data)
        self.assertEqual(result.locale, "EN")
        self.assertEqual(result.locale_source, "LANGUAGE")

    def test_near_threshold_allows_three_unique_and_two_articles_deficit(self):
        rows = make_rows(6, domain="drop.de")
        data = assessment_for(rows, articles=3, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.verdict, "PASS_NEAR_THRESHOLD")
        self.assertEqual(result.unique_deficit, 3)
        self.assertEqual(result.article_deficit, 2)

    def test_strict_mode_near_allows_only_one_unique_and_one_article_deficit(self):
        rows = make_rows(8, domain="drop.de")
        data = assessment_for(rows, articles=4, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data, strict_mode=True)
        self.assertEqual(result.verdict, "PASS_NEAR_THRESHOLD")
        self.assertEqual(result.unique_deficit, 1)
        self.assertEqual(result.article_deficit, 1)

    def test_strict_mode_rejects_old_near_margin(self):
        rows = make_rows(6, domain="drop.de")
        data = assessment_for(rows, articles=3, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data, strict_mode=True)
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(result.status, "BAD:LOW_PROFILE")

    def test_strict_mode_custom_near_deficits_are_used(self):
        rows = make_rows(7, domain="drop.de")
        data = assessment_for(rows, articles=4, locale="DE")
        default_strict = aggregate_assessment("drop.de", "DE", rows, data, strict_mode=True)
        custom_strict = aggregate_assessment(
            "drop.de",
            "DE",
            rows,
            data,
            strict_mode=True,
            strict_unique_deficit=2,
            strict_article_deficit=1,
        )
        self.assertEqual(default_strict.status, "BAD:LOW_PROFILE")
        self.assertEqual(custom_strict.verdict, "PASS_NEAR_THRESHOLD")
        self.assertEqual(custom_strict.unique_deficit, 2)
        self.assertEqual(custom_strict.article_deficit, 1)

    def test_near_threshold_allows_non_blocking_old_link_warning(self):
        rows = make_rows(8, domain="theespionne.ch")
        rows[0]["source_title"] = "Interview archive 2015"
        data = assessment_for(rows, articles=6, locale="CH")

        result = aggregate_assessment(
            "theespionne.ch",
            "RECHECK",
            rows,
            data,
            strict_mode=True,
            strict_unique_deficit=2,
            strict_article_deficit=1,
        )

        self.assertEqual(result.status, "GOOD (NEAR THRESHOLD)")
        self.assertEqual(result.verdict, "PASS_NEAR_THRESHOLD")
        self.assertEqual((result.unique_quality, result.article_links), (8, 6))
        self.assertEqual(result.old_links, 1)

    def test_threshold_matrix_matches_good_near_and_low_profile(self):
        cases = [
            ("LT", "drop.lt", False, 1, 1),
            ("PL", "drop.pl", False, 1, 1),
            ("CH", "drop.ch", False, 1, 1),
            ("CH", "drop.ch", True, 2, 1),
        ]
        for locale, domain, strict_mode, unique_deficit, article_deficit in cases:
            with self.subTest(locale=locale, strict_mode=strict_mode):
                required_unique, required_articles = thresholds_for_locale(locale)
                near_unique, near_articles = near_thresholds_for_locale(
                    locale,
                    strict_mode=strict_mode,
                    strict_unique_deficit=unique_deficit,
                    strict_article_deficit=article_deficit,
                )

                good_rows = make_rows(required_unique, domain=domain)
                for row in good_rows:
                    row["target_url"] = f"https://{domain}/"
                good = aggregate_assessment(
                    domain,
                    locale,
                    good_rows,
                    assessment_for(good_rows, articles=required_articles, locale=locale),
                    strict_mode=strict_mode,
                    strict_unique_deficit=unique_deficit,
                    strict_article_deficit=article_deficit,
                )
                self.assertEqual(good.status, "GOOD")

                near_rows = make_rows(near_unique, domain=domain)
                for row in near_rows:
                    row["target_url"] = f"https://{domain}/"
                near = aggregate_assessment(
                    domain,
                    locale,
                    near_rows,
                    assessment_for(near_rows, articles=near_articles, locale=locale),
                    strict_mode=strict_mode,
                    strict_unique_deficit=unique_deficit,
                    strict_article_deficit=article_deficit,
                )
                expected_near = "GOOD" if (near_unique, near_articles) == (required_unique, required_articles) else "GOOD (NEAR THRESHOLD)"
                self.assertEqual(near.status, expected_near)

                if near_unique > 0:
                    weak_rows = make_rows(near_unique - 1, domain=domain)
                    for row in weak_rows:
                        row["target_url"] = f"https://{domain}/"
                    weak = aggregate_assessment(
                        domain,
                        locale,
                        weak_rows,
                        assessment_for(weak_rows, articles=min(near_articles, len(weak_rows)), locale=locale),
                        strict_mode=strict_mode,
                        strict_unique_deficit=unique_deficit,
                        strict_article_deficit=article_deficit,
                    )
                    self.assertEqual(weak.status, "BAD:LOW_PROFILE")

    def test_beyond_near_threshold_is_rejected(self):
        rows = make_rows(5, domain="drop.de")
        data = assessment_for(rows, articles=3, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(result.status, "BAD:LOW_PROFILE")

    def test_two_two_two_is_too_weak(self):
        rows = make_rows(2, domain="drop.de")
        data = assessment_for(rows, articles=2, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(result.status, "BAD:LOW_PROFILE")

    def test_five_five_five_is_below_de_near_threshold(self):
        rows = make_rows(5, domain="muscatel-gin.de")
        for row in rows:
            row["target_url"] = "https://muscatel-gin.de/"
        data = assessment_for(rows, articles=5, locale="DE")
        result = aggregate_assessment("muscatel-gin.de", "DE", rows, data)
        self.assertEqual(result.status, "BAD:LOW_PROFILE")
        self.assertEqual(
            (result.unique_quality, result.article_links, result.homepage_links),
            (5, 5, 5),
        )

    def test_homepage_share_below_half_is_hard_stop(self):
        rows = make_rows(15, domain="drop.de")
        for index, row in enumerate(rows):
            row["target_url"] = "https://drop.de/" if index < 4 else f"https://drop.de/inner-{index}"
        data = assessment_for(rows, articles=7, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(result.status, "BAD:HOMEPAGE_SHARE")
        self.assertIn("27%", result.reason)
        self.assertTrue(result.hard_stop_reasons)

    def test_stale_profile_without_any_2016_plus_quality_link_is_rejected(self):
        rows = make_rows(9, domain="drop.de")
        data = assessment_for(rows, articles=5, locale="DE")
        for item in data["link_assessments"][:3]:
            item["age_signal"] = "OLD_2015_OR_EARLIER"
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "BAD:STALE_PROFILE")
        self.assertEqual(result.old_links, 3)
        self.assertEqual(result.modern_links, 0)

    def test_one_confirmed_2016_plus_quality_link_satisfies_freshness(self):
        rows = make_rows(9, domain="drop.de")
        rows[2]["source_title"] = "Editorial mention 2017"
        data = assessment_for(rows, articles=5, locale="DE")
        data["link_assessments"][0]["age_signal"] = "OLD_2015_OR_EARLIER"
        data["link_assessments"][1]["age_signal"] = "OLD_2015_OR_EARLIER"
        data["link_assessments"][2]["age_signal"] = "NORMAL_2017_PLUS"
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "GOOD")
        self.assertEqual(result.modern_links, 1)

    def test_freshness_share_over_limit_is_rejected(self):
        rows = make_rows(9, domain="drop.de")
        for index in range(5):
            rows[index]["source_url"] = f"https://donor{index}.example/2014/old-mention/"
        for index in range(5, 9):
            rows[index]["source_url"] = f"https://donor{index}.example/2018/new-mention/"
        data = assessment_for(rows, articles=5, locale="DE")
        result = aggregate_assessment(
            "drop.de",
            "DE",
            rows,
            data,
            freshness_cutoff_year=2016,
            freshness_max_old_share_percent=50,
        )
        self.assertEqual(result.status, "BAD:STALE_PROFILE")
        self.assertEqual(result.old_links, 5)
        self.assertEqual(result.modern_links, 4)

    def test_freshness_share_limit_is_configurable(self):
        rows = make_rows(9, domain="drop.de")
        for index in range(5):
            rows[index]["source_url"] = f"https://donor{index}.example/2014/old-mention/"
        for index in range(5, 9):
            rows[index]["source_url"] = f"https://donor{index}.example/2018/new-mention/"
        data = assessment_for(rows, articles=5, locale="DE")
        result = aggregate_assessment(
            "drop.de",
            "DE",
            rows,
            data,
            freshness_cutoff_year=2016,
            freshness_max_old_share_percent=60,
        )
        self.assertEqual(result.status, "GOOD")

    def test_aggregate_reports_quality_link_year_range(self):
        rows = make_rows(4, domain="drop.de")
        rows[0]["source_url"] = "https://donor1.example/2014/mention/"
        rows[1]["source_title"] = "Editorial mention 2018"
        rows[2]["page_years"] = [2021]
        data = assessment_for(rows, articles=2, locale="DE")
        result = aggregate_assessment(
            "drop.de",
            "DE",
            rows,
            data,
            freshness_max_old_share_percent=100,
        )
        self.assertEqual(result.link_year_min, 2014)
        self.assertEqual(result.link_year_max, 2021)
        self.assertEqual(result.link_year_count, 3)

    def test_link_year_extraction_ignores_twentieth_century_noise(self):
        self.assertEqual(
            extract_explicit_years(
                "Company founded in 1982",
                "https://donor.example/2017/article/",
                "Archive 1999",
            ),
            [2017],
        )

    def test_freshness_ignores_majestic_first_indexed_when_source_has_no_year(self):
        rows = make_rows(9, domain="drop.de")
        for index, row in enumerate(rows):
            row["source_url"] = f"https://donor{index}.example/page"
            row["first_indexed"] = "02 Mar 2014" if index < 5 else "14 Apr 2018"
        data = assessment_for(rows, articles=5, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "GOOD")
        self.assertEqual(result.old_links, 0)
        self.assertEqual(result.modern_links, 0)
        self.assertEqual(result.unknown_age_links, 9)

    def test_model_modern_age_without_explicit_2016_plus_does_not_override_old_sources(self):
        rows = make_rows(9, domain="drop.de")
        rows[0]["source_url"] = "https://donor1.example/2011/old-review/"
        rows[1]["source_title"] = "Old event 2014 recap"
        data = assessment_for(rows, articles=5, locale="DE")
        for item in data["link_assessments"]:
            item["age_signal"] = "NORMAL_2017_PLUS"
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "BAD:STALE_PROFILE")
        self.assertEqual(result.old_links, 2)
        self.assertEqual(result.modern_links, 0)

    def test_historic_anchor_spam_hard_stops(self):
        rows = make_rows(9, domain="drop.de")
        data = assessment_for(
            rows,
            articles=5,
            locale="DE",
            anchor_risk="SPAM",
            anchor_reasons=["casino anchors in Historic"],
        )
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "BAD:AI_HARD_STOP")
        self.assertTrue(result.hard_stop_reasons)

    def test_single_spam_donor_is_excluded_but_not_automatic_hard_stop(self):
        rows = make_rows(7)
        data = assessment_for(rows)
        data["link_assessments"][0].update(
            quality="SPAM",
            count_quality=False,
            prohibited_topic="CASINO",
        )
        result = aggregate_assessment("drop.lt", "LT", rows, data)
        self.assertEqual(result.status, "GOOD (NEAR THRESHOLD)")
        self.assertFalse(result.hard_stop_reasons)
        self.assertEqual(result.unique_quality, 6)

    def test_duplicate_donor_page_counts_once(self):
        rows = make_rows(2)
        rows[1]["source_domain"] = rows[0]["source_domain"]
        rows[1]["source_url"] = rows[0]["source_url"] + "/"
        result = aggregate_assessment("drop.lt", "LT", rows, assessment_for(rows, articles=2))
        self.assertEqual(result.unique_quality, 1)
        self.assertEqual(result.article_links, 1)

    def test_syndicated_same_article_path_counts_once_across_different_domains(self):
        path = "/hirek/olvas/-korda-racing-mindenki-hozta-sot-tulteljesitette-a-vart-eredmenyt-2023-04-21-132354"
        rows = [
            {
                "record_id": f"M{i}",
                "source_domain": f"info{i}.hu",
                "source_url": f"https://info{i}.hu{path}",
                "source_title": "Korda Racing: mindenki hozta a várt eredményt",
                "target_url": "https://koradracing.hu/",
            }
            for i in range(1, 8)
        ]
        self.assertEqual(len(unique_backlinks(rows)), 7)
        result = aggregate_assessment("koradracing.hu", "HU", rows, assessment_for(rows, articles=7, locale="HU"))
        self.assertEqual(result.unique_quality, 7)
        self.assertEqual(result.article_links, 1)
        self.assertEqual(result.status, "BAD:LOW_PROFILE")

    def test_same_article_title_on_different_donors_preserves_unique_but_caps_articles(self):
        rows = make_rows(9, domain="drop.de")
        for index, row in enumerate(rows, start=1):
            row.update(
                source_domain=f"news{index}.example",
                source_url=f"https://news{index}.example/local/path-{index}",
                source_title="Government announces the same regional consumer voucher programme",
                target_url="https://drop.de/",
            )
        result = aggregate_assessment(
            "drop.de",
            "DE",
            rows,
            assessment_for(rows, articles=9, locale="DE"),
        )
        self.assertEqual(result.unique_quality, 9)
        self.assertEqual(result.article_links, 1)
        self.assertEqual(result.status, "BAD:LOW_PROFILE")
        self.assertTrue(any("копии/репосты" in warning for warning in result.warnings))

    def test_near_identical_fetched_text_caps_article_cluster(self):
        rows = make_rows(9, domain="drop.de")
        copied_text = " ".join(
            [
                "The regional government announced a consumer voucher programme for local shops",
                "Applications open in April and participating businesses receive the same conditions",
                "The campaign supports commerce throughout the islands with a shared public platform",
            ]
            * 8
        )
        for index, row in enumerate(rows[:5], start=1):
            row["source_title"] = f"Regional commerce update number {index}"
            row["_article_page_text"] = copied_text + f" publisher footer {index}"
            row["target_url"] = "https://drop.de/"
        for row in rows[5:]:
            row["target_url"] = "https://drop.de/"
        metric, collapsed = clustered_article_metric(
            rows,
            [f"M{i}" for i in range(1, 6)],
            [],
        )
        self.assertEqual(metric, 1)
        self.assertEqual(collapsed, 4)

    def test_repeated_long_article_anchor_caps_cluster_when_pages_are_blocked(self):
        rows = make_rows(5, domain="drop.de")
        repeated_context = (
            "The voucher purchase works like the previous edition and the campaign "
            "will run for two months through the same public platform"
        )
        for index, row in enumerate(rows, start=1):
            row.update(
                source_domain=f"regional{index}.example",
                source_url=f"https://regional{index}.example/news/{index}",
                source_title=f"Regional commerce voucher publication {index}",
                anchor=repeated_context,
            )
        metric, collapsed = clustered_article_metric(
            rows,
            ["M1", "M2", "M3", "M4", "M5"],
            [],
        )
        self.assertEqual(metric, 1)
        self.assertEqual(collapsed, 4)

    def test_same_campaign_with_different_editorial_titles_is_not_auto_clustered(self):
        rows = make_rows(4, domain="drop.de")
        titles = [
            "Applications open for the Canary Islands consumer voucher programme",
            "Local shops report four million euros redeemed through island vouchers",
            "Merchants explain eligibility rules for the new regional commerce campaign",
            "Second edition of public support scheme receives government funding",
        ]
        for row, source_title in zip(rows, titles):
            row["source_title"] = source_title
        metric, collapsed = clustered_article_metric(
            rows,
            ["M1", "M2", "M3", "M4"],
            [],
        )
        self.assertEqual(metric, 4)
        self.assertEqual(collapsed, 0)

    def test_tracking_query_variants_are_one_source_page(self):
        rows = make_rows(2)
        rows[0].update(
            source_domain="news.example",
            source_url="http://www.news.example/article/?utm_source=feed&fbclid=abc",
        )
        rows[1].update(
            source_domain="news.example",
            source_url="https://news.example/article",
        )
        self.assertEqual(len(unique_backlinks(rows)), 1)

    def test_functional_query_pages_remain_separate_unique_sources(self):
        rows = make_rows(2)
        rows[0].update(source_domain="news.example", source_url="https://news.example/?p=101")
        rows[1].update(source_domain="news.example", source_url="https://news.example/?p=202")
        self.assertEqual(len(unique_backlinks(rows)), 2)

    def test_nonspam_reference_page_counts_as_unique_even_when_not_article(self):
        rows = make_rows(6, domain="drop.de")
        for row in rows:
            row["target_url"] = "https://drop.de/"
        rows[-1].update(
            source_domain="beckschulte.de",
            source_url="https://www.beckschulte.de/leistungen/referenzen",
            source_title="Referenzen – Beckschulte Systemtechnik GmbH",
            outbound_external=18,
            external_domains=14,
        )
        anchor = AnchorScreenAssessment(
            locale="DE",
            language="de",
            topic="local club",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        batch = LinkBatchAssessment(
            pbn_risk="CLEAN",
            pbn_reasons=[],
            hard_stop_reasons=[],
            quality_record_ids=[f"M{i}" for i in range(1, 6)],
            article_record_ids=["M1", "M2", "M3"],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=[f"M{i}" for i in range(1, 7)],
            spam_record_ids=[],
        )
        staged = combine_staged_assessments(anchor, [batch], rows)
        result = aggregate_assessment("drop.de", "DE", rows, staged)
        self.assertEqual(result.unique_quality, 6)
        self.assertEqual(result.article_links, 3)
        self.assertEqual(result.status, "GOOD (NEAR THRESHOLD)")

    def test_obvious_link_farm_is_not_locally_supplemented_as_unique(self):
        rows = make_rows(6, domain="drop.de")
        for row in rows:
            row["target_url"] = "https://drop.de/"
        rows[-1].update(
            source_domain="domains.com.bz",
            source_url="https://www.domains.com.bz/page/137193/",
            source_title="Top Domains – Page 137193",
            outbound_external=1374,
            external_domains=1366,
        )
        anchor = AnchorScreenAssessment(
            locale="DE",
            language="de",
            topic="local club",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        batch = LinkBatchAssessment(
            pbn_risk="CLEAN",
            pbn_reasons=[],
            hard_stop_reasons=[],
            quality_record_ids=[f"M{i}" for i in range(1, 6)],
            article_record_ids=["M1", "M2", "M3"],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=[f"M{i}" for i in range(1, 7)],
            spam_record_ids=[],
        )
        staged = combine_staged_assessments(anchor, [batch], rows)
        result = aggregate_assessment("drop.de", "DE", rows, staged)
        self.assertEqual(result.unique_quality, 5)
        self.assertEqual(result.status, "BAD:LOW_PROFILE")

    def test_staged_assessment_preserves_locale_evidence(self):
        rows = make_rows(2, domain="heimatwein.com")
        anchor = AnchorScreenAssessment(
            locale="DE",
            locale_evidence="Berlin + немецкие винодельни/доноры",
            language="de",
            topic="wine shop",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        batch = LinkBatchAssessment(
            pbn_risk="CLEAN",
            pbn_reasons=[],
            hard_stop_reasons=[],
            quality_record_ids=["M1", "M2"],
            article_record_ids=[],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=["M1", "M2"],
            spam_record_ids=[],
        )
        staged = combine_staged_assessments(anchor, [batch], rows)
        self.assertEqual(staged["locale"], "DE")
        self.assertEqual(staged["locale_evidence"], "Berlin + немецкие винодельни/доноры")

    def test_prepare_evidence_keeps_fresh_and_historic_separate(self):
        evidence = prepare_evidence(
            "drop.lt",
            "LT",
            {"rows": [{"source_url": "https://donor.lt/a", "source_domain": "donor.lt"}]},
            {"rows": [{"anchor": "fresh brand"}]},
            {"rows": [{"anchor": "historic casino"}]},
        )
        self.assertEqual(evidence["anchors_fresh"][0]["anchor"], "fresh brand")
        self.assertEqual(evidence["anchors_historic"][0]["anchor"], "historic casino")
        self.assertEqual(evidence["backlinks"][0]["record_id"], "M1")

    def test_compact_backlinks_include_tf_cf_and_target_context(self):
        row = make_rows(1)[0] | {
            "source_url_tf": 12,
            "source_url_cf": 22,
            "source_domain_tf": 18,
            "source_domain_cf": 27,
            "target_title": "Drop title",
            "target_topic": "News",
        }
        payload = compact_backlink_batch("drop.lt", "LT", [row], 1, 1)
        self.assertNotIn("target_url", payload["columns"])
        values = dict(zip(payload["columns"], payload["rows"][0]))
        self.assertEqual(values["url_cf"], 22)
        self.assertEqual(values["domain_cf"], 27)
        self.assertEqual(values["target_topic"], "News")

    def test_luna_payload_does_not_mix_individual_link_anchor_into_anchor_profile(self):
        row = make_rows(1)[0] | {"anchor": "high quality PBN backlinks"}
        payload = compact_critical_payload("drop.lt", "LT", [row])
        self.assertNotIn("anchor", payload["columns"])

    def test_seo_pbn_reason_is_not_domain_hard_stop(self):
        reason = "Массовая сеть PBN с SEO-ссылками и спамными анкорами"
        self.assertTrue(is_seo_noise_only_reason(reason))
        batch = LinkBatchAssessment(
            pbn_risk="SPAM",
            pbn_reasons=[reason],
            hard_stop_reasons=[reason],
            quality_record_ids=[],
            article_record_ids=[],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=[],
            spam_record_ids=["M1"],
        )
        sanitized, warnings = sanitize_seo_only_batch(batch)
        self.assertEqual(sanitized.pbn_risk.value, "CLEAN")
        self.assertEqual(sanitized.hard_stop_reasons, [])
        self.assertTrue(warnings)

    def test_staged_age_groups_preserve_unknown_for_final_count(self):
        rows = make_rows(2)
        anchor = AnchorScreenAssessment(
            locale="LT",
            language="lt",
            topic="news",
            anchor_risk="CLEAN",
            anchor_reasons=[],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        batch = LinkBatchAssessment(
            pbn_risk="CLEAN",
            pbn_reasons=[],
            hard_stop_reasons=[],
            quality_record_ids=["M1", "M2"],
            article_record_ids=[],
            old_record_ids=["M1"],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=["M2"],
            spam_record_ids=[],
        )
        staged = combine_staged_assessments(anchor, [batch], rows)
        result = aggregate_assessment("drop.lt", "LT", rows, staged)
        self.assertEqual(result.old_links, 1)
        self.assertEqual(result.unknown_age_links, 1)

    def test_local_precheck_rejects_when_near_is_impossible(self):
        rows = make_rows(5, domain="drop.de")
        result = local_backlink_precheck("drop.de", "!DE", rows)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:LOW_PROFILE")
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(result.api_calls, 0)
        self.assertEqual(result.article_links, -1)
        self.assertIsNone(local_backlink_precheck("drop.de", "!DE", make_rows(6, domain="drop.de")))

    def test_local_precheck_without_manual_locale_uses_softest_gate(self):
        rows = make_rows(5, domain="drop.de")
        self.assertIsNone(local_backlink_precheck("drop.de", "DE", rows))
        self.assertIsNone(local_backlink_precheck("drop.de", "TEST", rows))

    def test_local_precheck_never_rejects_for_unknown_article_count(self):
        rows = make_rows(6, domain="drop.de")
        for index, row in enumerate(rows, start=1):
            row["source_url"] = f"https://donor{index}.example/"
            row["source_title"] = "Directory"
            row["outbound_external"] = 149
            row["external_domains"] = 74
        self.assertIsNone(local_backlink_precheck("drop.de", "DE", rows))

    def test_local_strict_precheck_rejects_when_homepage_share_cannot_reach_near(self):
        rows = make_rows(8, domain="drop.de")
        for index, row in enumerate(rows):
            row["target_url"] = "https://drop.de/" if index < 3 else f"https://drop.de/inner-{index}"
        self.assertIsNone(local_backlink_precheck("drop.de", "!DE", rows))
        result = local_backlink_precheck("drop.de", "!DE", rows, strict_mode=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HOMEPAGE_SHARE")
        self.assertEqual(result.early_stop_stage, "local_homepage_share_impossible")
        self.assertEqual(result.api_calls, 0)
        self.assertIsNone(
            local_backlink_precheck(
                "drop.de",
                "!DE",
                rows,
                strict_mode=True,
                strict_unique_deficit=3,
                strict_article_deficit=1,
            )
        )

    def test_historic_pages_detect_reused_domain_without_ai_tokens(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://www.2018f18worlds.com/",
                    "page_title": "勝ちに拘る競馬",
                    "language": "ja Japanese, 98% confidence",
                    "referring_urls": 200,
                    "referring_domains": 25,
                    "last_seen": "26 Mar 2024",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/news",
                    "page_title": "F18 Worlds 2018",
                    "last_seen": "27 Mar 2019",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/sponsors",
                    "page_title": "F18 Worlds 2018",
                    "last_seen": "28 Mar 2019",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/single-post/2017/07/17/Registration-is-Open",
                    "page_title": "",
                    "last_seen": "25 May 2018",
                },
            ]
        }
        result = local_historic_pages_precheck("2018f18worlds.com", "HU", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")
        self.assertEqual(result.api_calls, 0)
        self.assertEqual(result.early_stop_stage, "local_historic_pages")

    def test_historic_pages_detect_indonesian_gambling_reuse(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://www.rebootbooks.org/",
                    "page_title": "CAKEPTOGEL >> Situs Slot Main Pakai Deposit Pulsa Indosat Terbaru Gampang Menang.",
                    "language": "id Indonesian",
                    "referring_urls": 50,
                    "referring_domains": 8,
                    "last_seen": "14 May 2025",
                },
                {
                    "page_url": "https://www.rebootbooks.org/blog/reboot-reading-project",
                    "page_title": "ReBoot Books, Business & Reading",
                    "last_seen": "16 Apr 2024",
                },
                {
                    "page_url": "https://www.rebootbooks.org/about",
                    "page_title": "About ReBoot Books",
                    "last_seen": "16 Apr 2024",
                },
                {
                    "page_url": "https://www.rebootbooks.org/news",
                    "page_title": "ReBoot Books News",
                    "last_seen": "16 Apr 2024",
                },
            ]
        }
        result = local_historic_pages_precheck("rebootbooks.org", "US", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")
        self.assertEqual(result.early_stop_stage, "local_historic_pages")

    def test_historic_pages_detect_german_erotic_reuse(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://miricalls.com/",
                    "page_title": "Belebung Ihrer sexuellen Verbindung - erotische Massage und erotikads.ch",
                    "language": "de German",
                    "referring_urls": 20,
                    "referring_domains": 5,
                    "last_seen": "14 Jul 2025",
                },
                {
                    "page_url": "https://miricalls.com/blog/miri-calls-project",
                    "page_title": "Miri Calls project",
                    "last_seen": "23 Jun 2016",
                },
                {
                    "page_url": "https://miricalls.com/about",
                    "page_title": "About Miri Calls",
                    "last_seen": "23 Jun 2016",
                },
                {
                    "page_url": "https://miricalls.com/news",
                    "page_title": "Miri Calls News",
                    "last_seen": "23 Jun 2016",
                },
            ]
        }
        result = local_historic_pages_precheck("miricalls.com", "DE", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")

    def test_historic_pages_detect_multiple_forbidden_inner_pages(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://www.blauer-esel.at/handy-casinos",
                    "page_title": "Handy Casinos",
                    "last_seen": "23 Jun 2023",
                    "referring_urls": 10,
                    "referring_domains": 2,
                },
                {
                    "page_url": "https://www.blauer-esel.at/tests/boomcasino",
                    "page_title": "Boom Casino",
                    "last_seen": "23 Jun 2023",
                    "referring_urls": 8,
                    "referring_domains": 2,
                },
            ]
        }
        result = local_historic_pages_precheck("blauer-esel.at", "AT", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")
        self.assertEqual(result.early_stop_stage, "local_historic_pages")

    def test_historic_pages_do_not_reject_one_forbidden_inner_page_without_other_signals(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://example.at/news/casino-charity-event",
                    "page_title": "Casino charity event",
                    "last_seen": "23 Jun 2023",
                    "referring_urls": 1,
                    "referring_domains": 1,
                },
            ]
        }
        self.assertIsNone(local_historic_pages_precheck("example.at", "AT", pages))

    def test_historic_pages_reject_single_crypto_signals_category(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://deltalloydonk.org/",
                    "page_title": "Delta Lloyd Regatta",
                    "last_seen": "27 Sep 2022",
                },
                {
                    "page_url": "https://deltalloydonk.org/category/krypto-signale/",
                    "page_title": "Krypto Signale",
                    "last_seen": "27 Sep 2022",
                    "referring_urls": 1,
                    "referring_domains": 1,
                },
            ]
        }
        result = local_historic_pages_precheck("deltalloydonk.org", "NL", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")
        self.assertIn("crypto/krypto signals", result.reason)

    def test_historic_pages_detect_wp_asset_doorway_spam(self):
        pages = {
            "rows": [
                {
                    "page_url": "http://minkstikampai.lt/",
                    "page_title": "Minksti kampai",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "http://minkstikampai.lt/wp-content/baby-s3Q8q6v-2791810.html",
                    "page_title": "mountain buggy reservedelar",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "http://minkstikampai.lt/wp-content/baby-G7Y5S5w-2783933.html",
                    "page_title": "i coo car seat",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "http://minkstikampai.lt/wp-content/baby-c2a7W5X-2775881.html",
                    "page_title": "baby jogger city mini double stroller 2011 vs 2012",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "http://minkstikampai.lt/wp-includes/poussettes-m5Z7-3615151.html",
                    "page_title": "poussette loola pas cher belgique",
                    "last_seen": "25 Apr 2016",
                },
            ]
        }
        result = local_historic_pages_precheck("minkstikampai.lt", "LT", pages)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:HISTORIC_PAGES")
        self.assertEqual(result.early_stop_stage, "local_historic_pages")
        self.assertIn("WordPress", result.reason)

    def test_historic_pages_do_not_reject_single_wp_asset_html_page(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://example.lt/wp-content/cache/readme.html",
                    "page_title": "WordPress cache readme",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "https://example.lt/wp-content/uploads/manual.html",
                    "page_title": "Download manual",
                    "last_seen": "25 Apr 2016",
                },
                {
                    "page_url": "https://example.lt/about",
                    "page_title": "About company",
                    "last_seen": "25 Apr 2016",
                },
            ]
        }
        self.assertIsNone(local_historic_pages_precheck("example.lt", "LT", pages))

    def test_historic_pages_do_not_reject_consistent_old_site(self):
        pages = {
            "rows": [
                {
                    "page_url": "https://www.2018f18worlds.com/",
                    "page_title": "F18 Worlds 2018",
                    "language": "en English",
                    "referring_urls": 200,
                    "referring_domains": 25,
                    "last_seen": "27 Mar 2019",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/news",
                    "page_title": "F18 Worlds 2018",
                    "last_seen": "27 Mar 2019",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/sponsors",
                    "page_title": "F18 Worlds 2018",
                    "last_seen": "28 Mar 2019",
                },
                {
                    "page_url": "https://www.2018f18worlds.com/about-the-regatta",
                    "page_title": "F18 Worlds 2018",
                    "last_seen": "23 Mar 2019",
                },
            ]
        }
        self.assertIsNone(local_historic_pages_precheck("2018f18worlds.com", "HU", pages))

    def test_historic_pages_payload_is_compact_and_prioritizes_risk(self):
        columns, rows, truncated = compact_historic_pages_payload(
            {
                "rows": [
                    {"page_url": "https://drop.test/news", "page_title": "News 2018", "last_seen": "2018"},
                    {"page_url": "https://drop.test/", "page_title": "勝ちに拘る競馬", "last_seen": "2024"},
                ],
                "truncated": False,
            },
            max_rows=1,
        )
        self.assertEqual(columns[0], "url")
        self.assertEqual(rows[0][1], "勝ちに拘る競馬")
        self.assertTrue(truncated)

    def test_page_evidence_fetches_only_selected_candidates_with_cap(self):
        rows = make_rows(3)
        page = {
            "status": "OK",
            "http_status": 200,
            "final_url": "https://donor.example/article",
            "page_title": "Article",
            "description": "Description",
            "text_excerpt": "Long editorial text",
            "error": "",
        }
        with patch("domain_ai.fetch_article_page", return_value=page) as fetch:
            evidence, skipped = collect_article_page_evidence(
                rows, {"M1", "M2"}, max_pages=1, max_chars=1000
            )
        self.assertEqual([item["id"] for item in evidence], ["M1"])
        self.assertEqual(skipped, 1)
        fetch.assert_called_once()

    def test_article_preview_extracts_target_link_context_and_density(self):
        html = """
        <html>
          <head><title>Local event recap</title><meta name="description" content="Editorial recap"></head>
          <body>
            <main>
              <article>
                <p>The yearly cultural festival featured several community partners,
                including <a href="https://drop.lt/about">Drop Project</a>, with interviews and photos.</p>
                <p>More editorial text about the event and its organizers.</p>
              </article>
              <footer><a href="https://unrelated.example/">Footer link</a></footer>
            </main>
          </body>
        </html>
        """
        preview = extract_article_page_preview(
            html,
            source_url="https://donor.example/story",
            target_url="https://drop.lt/",
            max_chars=500,
        )
        self.assertTrue(preview["target_link_found"])
        self.assertEqual(preview["target_link_count"], 1)
        self.assertIn("Drop Project", preview["target_link_texts"])
        self.assertIn("content", preview["link_dom_area"])
        self.assertIn("community partners", preview["link_context_excerpt"])
        self.assertGreaterEqual(preview["external_links_count"], 2)
        self.assertGreater(preview["visible_text_chars"], 50)

    def test_half_article_ids_count_as_half_with_cap(self):
        rows = make_rows(8, domain="drop.de")
        assessment = {
            "locale": "DE",
            "language": "de",
            "topic": "events",
            "pbn_risk": "CLEAN",
            "pbn_reasons": [],
            "anchor_risk": "CLEAN",
            "anchor_reasons": [],
            "hard_stop_reasons": [],
            "summary": "",
            "warnings": [],
            "link_assessments": [
                {
                    "record_id": row["record_id"],
                    "quality": "QUALITY",
                    "link_type": "ARTICLE",
                    "count_quality": True,
                    "count_article": True,
                    "article_weight": 0.5,
                    "prohibited_topic": "NONE",
                    "age_signal": "UNKNOWN",
                    "reason": "borderline annotation",
                }
                for row in rows
            ],
        }
        result = aggregate_assessment("drop.de", "DE", rows, assessment)
        self.assertEqual(result.unique_quality, 8)
        self.assertEqual(result.article_links, 2)

    def test_half_article_total_rounds_up_for_thresholds(self):
        rows = make_rows(9, domain="drop.com")
        assessment = assessment_for(rows, articles=5, locale="EN")
        assessment["link_assessments"][4]["article_weight"] = 0.5

        result = aggregate_assessment("drop.com", "EN", rows, assessment)

        self.assertEqual(result.status, "GOOD")
        self.assertEqual(result.article_links, 5)
        self.assertEqual(result.article_deficit, 0)

    def test_sol_confirms_articles_from_fetched_page_content(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.fetch_page_content = True
        checker.max_article_pages = 12
        checker.article_text_chars = 1200
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            if text_format is FirstBatchAssessment:
                self.assertEqual(payload["page_columns"][0], "id")
                self.assertEqual([row[0] for row in payload["pages"]], ["M1", "M2"])
                ids = [str(row[0]) for row in payload["rows"]]
                return FirstBatchAssessment(
                    locale="LT",
                    language="lt",
                    topic="news",
                    anchor_risk="CLEAN",
                    anchor_reasons=[],
                    pbn_risk="CLEAN",
                    pbn_reasons=[],
                    hard_stop_reasons=[],
                    quality_record_ids=ids,
                    article_record_ids=ids[:1],
                    old_record_ids=[],
                    modern_record_ids=ids[:1],
                    borderline_record_ids=[],
                    fresh_record_ids=[],
                    unknown_age_record_ids=ids,
                    spam_record_ids=[],
                ), 100, 20
            raise AssertionError(f"unexpected schema: {text_format}")

        checker._parse = fake_parse
        page_evidence = [
            {"id": "M1", "fetch_status": "OK", "text_excerpt": "Article one"},
            {"id": "M2", "fetch_status": "OK", "text_excerpt": "Directory"},
        ]
        with patch("domain_ai.collect_article_page_evidence", return_value=(page_evidence, 0)):
            verdict = checker.evaluate(
                "drop.lt",
                "LT",
                {"rows": make_rows(7)},
                {"rows": [{"anchor": "brand"}]},
                {"rows": [{"anchor": "old brand"}]},
            )
        self.assertEqual(verdict.article_links, 1)
        self.assertEqual(verdict.api_calls, 1)
        self.assertEqual(calls, [FirstBatchAssessment])

    def test_browser_fallback_checks_blocked_article_pages_when_it_can_change_result(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.article_fallback_max_output_tokens = 800
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.fetch_page_content = True
        checker.max_article_pages = 12
        checker.max_browser_article_pages = 5
        checker.article_text_chars = 1200
        calls = []

        rows = [
            {
                "source_domain": f"donor{i}.de",
                "source_url": f"https://donor{i}.de/article-{i}",
                "source_title": f"Article {i}",
                "target_url": "https://drop.de/",
            }
            for i in range(1, 10)
        ]

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            ids = [str(row[0]) for row in payload["rows"]]
            if text_format is FirstBatchAssessment:
                return (
                    FirstBatchAssessment(
                        locale="DE",
                        language="de",
                        topic="news",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        pbn_risk="CLEAN",
                        pbn_reasons=[],
                        hard_stop_reasons=[],
                        quality_record_ids=ids,
                        article_record_ids=[],
                        old_record_ids=[],
                        modern_record_ids=[],
                        borderline_record_ids=[],
                        fresh_record_ids=[],
                        unknown_age_record_ids=ids,
                        spam_record_ids=[],
                    ),
                    100,
                    20,
                )
            if text_format is ArticleFallbackAssessment:
                return ArticleFallbackAssessment(article_record_ids=ids[:5]), 50, 10
            raise AssertionError(f"unexpected schema: {text_format}")

        def fake_browser_fetch(url, max_chars):
            article_token = url.rstrip("/").rsplit("/", 1)[-1].replace("-", "")
            return {
                "status": "OK",
                "http_status": 0,
                "final_url": url,
                "page_title": f"Browser article {article_token}",
                "description": f"Independent description {article_token}",
                "text_excerpt": " ".join(
                    f"{article_token}word{index}" for index in range(80)
                ),
                "error": "",
            }

        checker._parse = fake_parse
        failed_pages = [
            {"id": f"M{i}", "fetch_status": "FETCH_ERROR", "text_excerpt": ""}
            for i in range(1, 10)
        ]
        with patch("domain_ai.collect_article_page_evidence", return_value=(failed_pages, 0)), patch(
            "domain_ai._is_public_http_url",
            return_value=True,
        ):
            verdict = checker.evaluate(
                "drop.de",
                "DE",
                {"rows": rows},
                {"rows": [{"anchor": "brand"}]},
                {"rows": [{"anchor": "old brand"}]},
                browser_page_fetcher=fake_browser_fetch,
            )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.article_links, 5)
        self.assertEqual(verdict.api_calls, 2)
        self.assertEqual(verdict.early_stop_stage, "browser_article_fallback")
        self.assertEqual(calls, [FirstBatchAssessment, ArticleFallbackAssessment])

    def test_historic_pages_context_rides_with_first_sol_batch(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.fetch_page_content = False
        checker.max_historic_pages_context = 2
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            self.assertIn("historic_pages_columns", payload)
            self.assertEqual(len(payload["historic_pages"]), 2)
            ids = [str(row[0]) for row in payload["rows"]]
            return FirstBatchAssessment(
                locale="LT",
                language="lt",
                topic="news",
                anchor_risk="CLEAN",
                anchor_reasons=[],
                pbn_risk="CLEAN",
                pbn_reasons=[],
                hard_stop_reasons=[],
                quality_record_ids=ids,
                article_record_ids=[],
                old_record_ids=[],
                modern_record_ids=ids,
                borderline_record_ids=[],
                fresh_record_ids=[],
                unknown_age_record_ids=[],
                spam_record_ids=[],
            ), 100, 20

        checker._parse = fake_parse
        historic_pages = {
            "rows": [
                {"page_url": "https://drop.lt/", "page_title": "Drop LT", "last_seen": "2024"},
                {"page_url": "https://drop.lt/news", "page_title": "Drop LT news 2018", "last_seen": "2018"},
            ]
        }
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            {"rows": make_rows(7, domain="drop.lt")},
            {"rows": [{"anchor": "brand"}]},
            {"rows": [{"anchor": "old brand"}]},
            historic_pages_report=historic_pages,
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.api_calls, 1)
        self.assertEqual(calls, [FirstBatchAssessment])

    def test_exact_half_homepage_share_is_allowed(self):
        rows = make_rows(6, domain="drop.de")
        data = assessment_for(rows, articles=3, locale="DE")
        result = aggregate_assessment("drop.de", "DE", rows, data)
        self.assertEqual(result.status, "GOOD (NEAR THRESHOLD)")

    def test_single_bad_historic_anchor_is_not_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "brand"}]},
            {"rows": [{"anchor": "best online casino"}]},
        )
        self.assertEqual(reasons, [])

    def test_single_explicit_forex_anchor_is_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "brand"}]},
            {"rows": [{"anchor": "fxと外為の無料相談", "referring_domains": 1}]},
        )
        self.assertTrue(reasons)
        self.assertIn("forex/trading", reasons[0])

    def test_substantial_historic_anchor_spam_is_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "brand", "referring_domains": 4}]},
            {
                "rows": [
                    {"anchor": "best online casino", "referring_domains": 6},
                    {"anchor": "casino bonus", "referring_domains": 3},
                ]
            },
        )
        self.assertTrue(reasons)
        self.assertIn("Historic", reasons[0])

    def test_indonesian_gambling_anchors_are_local_hard_stop_when_substantial(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "reboot books", "referring_domains": 4}]},
            {
                "rows": [
                    {"anchor": "CAKEPTOGEL situs slot deposit pulsa", "referring_domains": 6},
                ]
            },
        )
        self.assertTrue(reasons)
        self.assertIn("casino/betting", reasons[0])

    def test_erotic_anchors_are_local_hard_stop_when_substantial(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "miri calls", "referring_domains": 4}]},
            {
                "rows": [
                    {"anchor": "erotikads.ch erotische Massage", "referring_domains": 6},
                ]
            },
        )
        self.assertTrue(reasons)
        self.assertIn("adult", reasons[0])

    def test_exam_dump_historic_anchors_are_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "ombudsman-kv.ch", "referring_domains": 8}]},
            {
                "rows": [
                    {
                        "anchor": "cas-004 exam dumps - search www.itdumpskr.com",
                        "referring_domains": 1,
                        "total_links": 1,
                    },
                    {
                        "anchor": "c1000-143 practice test questions newdumpspdf.com",
                        "referring_domains": 1,
                        "total_links": 1,
                    },
                    {
                        "anchor": "pdfvce 4a0-220 desktop-based practice test software",
                        "referring_domains": 1,
                        "total_links": 1,
                    },
                ]
            },
        )
        self.assertTrue(reasons)
        self.assertIn("exam/certification dumps", reasons[0])

    def test_normal_exam_word_is_not_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "school final exam schedule", "referring_domains": 1}]},
            {"rows": [{"anchor": "university exam office", "referring_domains": 1}]},
        )
        self.assertEqual(reasons, [])

    def test_unexplained_model_risk_is_downgraded(self):
        batch = LinkBatchAssessment(
            pbn_risk="RISK",
            pbn_reasons=[],
            hard_stop_reasons=[],
            quality_record_ids=[],
            article_record_ids=[],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=[],
            spam_record_ids=[],
        )
        sanitized, warnings = sanitize_seo_only_batch(batch)
        self.assertEqual(sanitized.pbn_risk.value, "CLEAN")
        self.assertTrue(warnings)

    def test_brand_url_natural_batch_reason_is_not_ai_risk(self):
        batch = LinkBatchAssessment(
            pbn_risk="RISK",
            pbn_reasons=["Historic в основном естественный итальянский брендовый и URL-профиль"],
            hard_stop_reasons=[],
            quality_record_ids=[],
            article_record_ids=[],
            old_record_ids=[],
            modern_record_ids=[],
            borderline_record_ids=[],
            fresh_record_ids=[],
            unknown_age_record_ids=[],
            spam_record_ids=[],
        )
        sanitized, warnings = sanitize_seo_only_batch(batch)
        self.assertEqual(sanitized.pbn_risk.value, "CLEAN")
        self.assertEqual(sanitized.pbn_reasons, [])
        self.assertTrue(warnings)

    def test_brand_url_natural_anchor_reason_is_not_ai_risk(self):
        anchor = AnchorScreenAssessment(
            locale="CH",
            language="de",
            topic="local project",
            anchor_risk="RISK",
            anchor_reasons=[
                "Historic-профиль естественный и тематически связный; брендовые, URL и тематические анкоры; явной запрещённой тематики нет"
            ],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        sanitized, warnings = sanitize_seo_only_anchor(anchor)
        self.assertEqual(sanitized.anchor_risk.value, "CLEAN")
        self.assertEqual(sanitized.anchor_reasons, [])
        self.assertTrue(warnings)

    def test_autogenerated_seo_anchor_noise_without_forbidden_topic_is_ignored(self):
        self.assertTrue(
            is_seo_noise_only_reason(
                "Fresh содержит заметный всплеск автогенерированных SEO-анкорoв backlinks и authority; явной запрещённой тематики нет"
            )
        )

    def test_brand_wording_does_not_hide_forbidden_topic(self):
        anchor = AnchorScreenAssessment(
            locale="CH",
            language="de",
            topic="local project",
            anchor_risk="RISK",
            anchor_reasons=["Брендовый профиль, но заметны casino и betting анкоры"],
            hard_stop_reasons=[],
            summary="",
            warnings=[],
        )
        sanitized, _warnings = sanitize_seo_only_anchor(anchor)
        self.assertEqual(sanitized.anchor_risk.value, "RISK")
        self.assertEqual(sanitized.anchor_reasons, ["Брендовый профиль, но заметны casino и betting анкоры"])

    def test_unverified_plain_lander_root_redirect_is_not_hard_stop(self):
        anchor = AnchorScreenAssessment(
            locale="FR",
            language="fr",
            topic="startup incubator",
            anchor_risk="RISK",
            anchor_reasons=[],
            hard_stop_reasons=[
                "Корень домена перенаправляет на /lander; вероятный doorway-переюз"
            ],
            summary="",
            warnings=[],
        )
        sanitized, warnings = sanitize_seo_only_anchor(anchor)
        self.assertEqual(sanitized.anchor_risk.value, "CLEAN")
        self.assertEqual(sanitized.hard_stop_reasons, [])
        self.assertTrue(warnings)

    def test_affiliate_directory_reprofile_remains_hard_stop(self):
        anchor = AnchorScreenAssessment(
            locale="FR",
            language="fr",
            topic="startup incubator",
            anchor_risk="RISK",
            anchor_reasons=[],
            hard_stop_reasons=[
                "Исходный startup-incubator репрофилирован и перенаправлен на affiliate-директорию TopRanked"
            ],
            summary="",
            warnings=[],
        )
        sanitized, _warnings = sanitize_seo_only_anchor(anchor)
        self.assertEqual(sanitized.anchor_risk.value, "RISK")
        self.assertEqual(
            sanitized.hard_stop_reasons,
            ["Исходный startup-incubator репрофилирован и перенаправлен на affiliate-директорию TopRanked"],
        )

    def test_affiliate_redirect_with_strong_forbidden_topic_remains_risk(self):
        anchor = AnchorScreenAssessment(
            locale="FR",
            language="fr",
            topic="startup incubator",
            anchor_risk="SPAM",
            anchor_reasons=[],
            hard_stop_reasons=[
                "Корень домена перенаправляет на affiliate casino directory"
            ],
            summary="",
            warnings=[],
        )
        sanitized, _warnings = sanitize_seo_only_anchor(anchor)
        self.assertEqual(sanitized.anchor_risk.value, "SPAM")
        self.assertEqual(
            sanitized.hard_stop_reasons,
            ["Корень домена перенаправляет на affiliate casino directory"],
        )

    def test_critical_sort_prioritizes_prohibited_signal(self):
        normal = make_rows(1)[0] | {
            "source_title": "Trusted editorial interview",
            "source_domain_tf": 40,
        }
        risky = make_rows(1)[0] | {
            "record_id": "M2",
            "source_url": "https://risk.example/online-casino/",
            "source_title": "Online casino doorway",
            "source_domain_tf": 0,
        }
        ordered = sort_backlinks_for_critical([normal, risky])
        self.assertEqual(ordered[0]["record_id"], "M2")

    def test_seo_backlink_noise_is_not_local_hard_stop(self):
        reasons = scan_anchor_hard_stops(
            {"rows": [{"anchor": "buy backlinks and aged domains"}]},
            {"rows": [{"anchor": "expired domains SEO"}]},
        )
        self.assertEqual(reasons, [])

    def test_local_source_age_precheck_rejects_obviously_old_profile_without_2016_plus(self):
        rows = [
            {
                "record_id": "M1",
                "source_domain": "heavyplanet.net",
                "source_url": "http://www.heavyplanet.net/2009/03/",
                "source_title": "Psychedoomelic records",
                "target_url": "https://psychedoomelic.com/",
            },
            {
                "record_id": "M2",
                "source_domain": "eternal-terror.com",
                "source_url": "https://eternal-terror.com/2011/09/29/wretched-black-ambience/",
                "source_title": "Wretched - Black Ambience",
                "target_url": "https://psychedoomelic.com/",
            },
            {
                "record_id": "M3",
                "source_domain": "lahabitacion235.com",
                "source_url": "http://www.lahabitacion235.com/musica/alunah-white-hoarhound-2012.html",
                "source_title": "Alunah White Hoarhound 2012",
                "target_url": "https://psychedoomelic.com/",
            },
        ]
        result = local_source_age_precheck("psychedoomelic.com", "HU", rows)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "BAD:STALE_PROFILE")
        self.assertEqual(result.api_calls, 0)

    def test_local_source_age_precheck_allows_any_explicit_2016_plus_signal(self):
        rows = make_rows(3, domain="drop.de")
        rows[0]["source_url"] = "https://donor.example/2011/old"
        rows[1]["source_url"] = "https://donor2.example/review-2017"
        self.assertIsNone(local_source_age_precheck("drop.de", "DE", rows))

    def test_anchor_payload_deduplicates_and_limits_each_index(self):
        evidence = {
            "domain": "drop.lt",
            "batch_locale_hint": "LT",
            "anchors_fresh": [
                {"anchor": "Brand", "topic": "x"},
                {"anchor": " brand ", "topic": "x"},
                {"anchor": "Second", "topic": "x"},
            ],
            "anchors_historic": [{"anchor": "Old", "topic": "x"}],
        }
        payload, count = compact_anchor_payload(evidence, 1)
        self.assertEqual(count, 2)
        self.assertEqual(payload["fresh"][0][0], "Brand")

    def test_staged_checker_stops_after_first_link_batch_when_threshold_reached(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.model = "test-sol"
        checker.screen_model = "test-luna"
        checker.reasoning_effort = ""
        checker.screen_max_output_tokens = 900
        checker.max_risk_anchors = 100
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        profile_modes = []
        models = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            profile_modes.append(payload.get("profile_mode"))
            models.append(model_name or checker.model)
            ids = [str(row[0]) for row in payload["rows"]]
            if text_format is CriticalScreenAssessment:
                return (
                    CriticalScreenAssessment(
                        anchor_risk="SPAM",
                        pbn_risk="SPAM",
                        hard_stop_reasons=[
                            "Массовая сеть PBN с SEO-ссылками и спамными анкорами"
                        ],
                    ),
                    200,
                    30,
                )
            if text_format is FirstBatchAssessment:
                return (
                    FirstBatchAssessment(
                        locale="LT",
                        language="lt",
                        topic="news",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        pbn_risk="CLEAN",
                        pbn_reasons=[],
                        hard_stop_reasons=[],
                        quality_record_ids=ids[:7],
                        article_record_ids=[],
                        old_record_ids=[],
                        modern_record_ids=ids[:7],
                        borderline_record_ids=[],
                        fresh_record_ids=[],
                        unknown_age_record_ids=ids[:7],
                        spam_record_ids=[],
                    ),
                    200,
                    30,
                )
            return (
                LinkBatchAssessment(
                    pbn_risk="CLEAN",
                    pbn_reasons=[],
                    hard_stop_reasons=[],
                    quality_record_ids=ids[:7],
                    article_record_ids=[],
                    old_record_ids=[],
                    modern_record_ids=ids[:7],
                    borderline_record_ids=[],
                    fresh_record_ids=[],
                    unknown_age_record_ids=ids[:7],
                    spam_record_ids=[],
                ),
                200,
                30,
            )

        checker._parse = fake_parse
        backlinks = {
            "rows": [
                {
                    "source_domain": f"donor{i}.lt",
                    "source_url": f"https://donor{i}.lt/article-{i}",
                    "source_title": f"Article {i}",
                    "source_topic": "News",
                    "target_url": "https://drop.lt/",
                }
                for i in range(1, 41)
            ]
        }
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            backlinks,
            {"rows": [{"anchor": "brand", "referring_domains": 4}]},
            {"rows": [{"anchor": "drop.lt", "referring_domains": 3}]},
            majestic_status="GOOD OLD",
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.api_calls, 2)
        self.assertEqual(verdict.backlinks_sent, 20)
        self.assertEqual(verdict.early_stop_stage, "strict_threshold_reached")
        self.assertEqual(verdict.input_tokens, 400)
        self.assertEqual(profile_modes, ["GOOD_OLD", "GOOD_OLD"])
        self.assertEqual(models, ["test-luna", "test-sol"])
        self.assertEqual(verdict.model, "test-luna → test-sol")
        self.assertTrue(any("Быстрый скрининг" in warning for warning in verdict.warnings))

    def test_staged_checker_does_not_early_accept_copied_article_cluster(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.model = "test-terra"
        checker.screen_model = "test-luna"
        checker.reasoning_effort = ""
        checker.max_risk_anchors = 100
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.enable_luna_screen = False
        checker.fetch_page_content = True
        checker.max_article_pages = 12
        checker.max_browser_article_pages = 0
        checker.article_text_chars = 1200
        checker.freshness_filter_enabled = False

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            ids = [str(row[0]) for row in payload["rows"]]
            common = dict(
                pbn_risk="CLEAN",
                pbn_reasons=[],
                hard_stop_reasons=[],
                quality_record_ids=ids,
                article_record_ids=ids,
                old_record_ids=[],
                modern_record_ids=ids,
                borderline_record_ids=[],
                fresh_record_ids=[],
                unknown_age_record_ids=[],
                spam_record_ids=[],
            )
            if text_format is FirstBatchAssessment:
                return (
                    FirstBatchAssessment(
                        locale="DE",
                        language="de",
                        topic="regional news",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        **common,
                    ),
                    100,
                    20,
                )
            if text_format is LinkBatchAssessment:
                return LinkBatchAssessment(**common), 100, 20
            raise AssertionError(f"unexpected schema: {text_format}")

        def fake_page_evidence(batch_rows, candidate_ids, max_pages, max_chars):
            pages = []
            for row in batch_rows:
                record_id = str(row["record_id"])
                number = int(record_id.removeprefix("M"))
                if number <= 10:
                    page_title = "Same regional voucher press release published across portals"
                    text = "copied regional voucher campaign conditions participating merchants " * 40
                else:
                    page_title = f"Article {number}"
                    text = " ".join(f"donor{number}word{index}" for index in range(100))
                pages.append(
                    {
                        "id": record_id,
                        "fetch_status": "OK",
                        "http_status": 200,
                        "page_title": page_title,
                        "description": "",
                        "text_excerpt": text,
                        "target_link_found": True,
                        "target_link_texts": ["target"],
                        "link_dom_area": "content",
                        "link_context_excerpt": text[:300],
                        "external_links_count": 1,
                        "visible_text_chars": len(text),
                        "external_link_density": 0.01,
                    }
                )
            return pages, 0

        checker._parse = fake_parse
        rows = [
            {
                "source_domain": f"donor{i}.de",
                "source_url": f"https://donor{i}.de/article-{i}",
                "source_title": (
                    "Same regional voucher press release published across portals"
                    if i <= 10
                    else f"Article {i}"
                ),
                "source_topic": "News",
                "target_url": "https://drop.de/",
            }
            for i in range(1, 21)
        ]
        with patch("domain_ai.collect_article_page_evidence", side_effect=fake_page_evidence):
            verdict = checker.evaluate(
                "drop.de",
                "DE",
                {"rows": rows},
                {"rows": [{"anchor": "brand"}]},
                {"rows": [{"anchor": "old brand"}]},
            )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.api_calls, 2)
        self.assertEqual(verdict.backlinks_sent, 20)
        self.assertEqual(verdict.article_links, 11)
        self.assertEqual(verdict.early_stop_stage, "strict_threshold_reached")

    def test_staged_checker_freshness_early_exit_uses_configured_old_share_name(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.model = "test-sol"
        checker.screen_model = "test-luna"
        checker.reasoning_effort = ""
        checker.screen_max_output_tokens = 900
        checker.max_risk_anchors = 100
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.freshness_filter_enabled = True
        checker.freshness_cutoff_year = 2016
        checker.freshness_max_old_share_percent = 100

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            ids = [str(row[0]) for row in payload.get("rows", [])]
            if text_format is CriticalScreenAssessment:
                return (
                    CriticalScreenAssessment(
                        anchor_risk="CLEAN",
                        pbn_risk="CLEAN",
                        hard_stop_reasons=[],
                    ),
                    100,
                    10,
                )
            if text_format is FirstBatchAssessment:
                return (
                    FirstBatchAssessment(
                        locale="LT",
                        language="lt",
                        topic="news",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        pbn_risk="CLEAN",
                        pbn_reasons=[],
                        hard_stop_reasons=[],
                        quality_record_ids=ids,
                        article_record_ids=[],
                        old_record_ids=[],
                        modern_record_ids=ids,
                        borderline_record_ids=[],
                        fresh_record_ids=[],
                        unknown_age_record_ids=[],
                        spam_record_ids=[],
                    ),
                    200,
                    20,
                )
            raise AssertionError(f"unexpected schema: {text_format}")

        checker._parse = fake_parse
        rows = []
        for i in range(1, 41):
            year = 2014 if i == 1 else 2020
            rows.append(
                {
                    "source_domain": f"donor{i}.lt",
                    "source_url": f"https://donor{i}.lt/{year}/article-{i}",
                    "source_title": f"Article {i}",
                    "source_topic": "News",
                    "target_url": "https://drop.lt/",
                }
            )

        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            {"rows": rows},
            {"rows": [{"anchor": "brand", "referring_domains": 4}]},
            {"rows": [{"anchor": "drop.lt", "referring_domains": 3}]},
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.early_stop_stage, "strict_threshold_reached")

    def test_luna_critical_stop_skips_quality_model(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.6-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append((text_format, model_name))
            return (
                CriticalScreenAssessment(
                    anchor_risk="SPAM",
                    pbn_risk="SPAM",
                    hard_stop_reasons=["casino doorway"],
                ),
                100,
                20,
            )

        checker._parse = fake_parse
        backlinks = {
            "rows": [
                {
                    "source_domain": f"donor{i}.lt",
                    "source_url": f"https://donor{i}.lt/article-{i}",
                    "source_title": f"Article {i}",
                    "target_url": "https://drop.lt/",
                }
                for i in range(1, 11)
            ]
        }
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            backlinks,
            {"rows": [{"anchor": "brand"}]},
            {"rows": [{"anchor": "old brand"}]},
        )
        self.assertEqual(verdict.status, "BAD:AI_HARD_STOP")
        self.assertEqual(verdict.early_stop_stage, "luna_critical_screen")
        self.assertEqual(calls, [(CriticalScreenAssessment, "gpt-5.6-luna")])

    def test_luna_anchor_only_alarm_is_rechecked_by_sol(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "gpt-5.6-luna"
        checker.model = "gpt-5.6-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            if text_format is CriticalScreenAssessment:
                return (
                    CriticalScreenAssessment(
                        anchor_risk="SPAM",
                        pbn_risk="CLEAN",
                        hard_stop_reasons=["casino в одном анкоре"],
                    ),
                    100,
                    20,
                )
            ids = [row[0] for row in payload["rows"]]
            return (
                FirstBatchAssessment(
                    locale="LT",
                    language="lt",
                    topic="news",
                    anchor_risk="CLEAN",
                    anchor_reasons=[],
                    pbn_risk="CLEAN",
                    pbn_reasons=[],
                    hard_stop_reasons=[],
                    quality_record_ids=ids,
                    article_record_ids=[],
                    old_record_ids=[],
                    modern_record_ids=ids,
                    borderline_record_ids=[],
                    fresh_record_ids=[],
                    unknown_age_record_ids=ids,
                    spam_record_ids=[],
                ),
                200,
                40,
            )

        checker._parse = fake_parse
        backlinks = {"rows": make_rows(7, domain="drop.lt")}
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            backlinks,
            {"rows": [{"anchor": "casino", "referring_domains": 1}]},
            {"rows": [{"anchor": "brand", "referring_domains": 5}]},
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(calls, [CriticalScreenAssessment, FirstBatchAssessment])

    def test_sol_anchor_precheck_rejects_before_large_batch(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.anchor_precheck_max_output_tokens = 700
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.enable_sol_anchor_precheck = True
        checker.fetch_page_content = False
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            self.assertIs(text_format, AnchorScreenAssessment)
            self.assertIn("precheck_reasons", payload)
            return (
                AnchorScreenAssessment(
                    locale="DE",
                    language="de",
                    topic="casino",
                    anchor_risk="SPAM",
                    anchor_reasons=["casino anchors"],
                    hard_stop_reasons=["casino anchors"],
                    summary="spam anchors",
                    warnings=[],
                ),
                120,
                10,
            )

        checker._parse = fake_parse
        verdict = checker.evaluate(
            "drop.de",
            "DE",
            {"rows": make_rows(9, domain="drop.de")},
            {"rows": [{"anchor": "brand", "referring_domains": 5}]},
            {"rows": [{"anchor": "best online casino bonus", "referring_domains": 1}]},
        )
        self.assertEqual(verdict.status, "BAD:AI_HARD_STOP")
        self.assertEqual(verdict.early_stop_stage, "sol_anchor_precheck")
        self.assertEqual(verdict.api_calls, 1)
        self.assertEqual(verdict.backlinks_sent, 0)
        self.assertEqual(verdict.input_tokens, 120)
        self.assertEqual(calls, [AnchorScreenAssessment])

    def test_clean_sol_anchor_precheck_skips_anchors_in_first_big_batch(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.anchor_precheck_max_output_tokens = 700
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.enable_sol_anchor_precheck = True
        checker.fetch_page_content = False
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            if text_format is AnchorScreenAssessment:
                self.assertIn("precheck_reasons", payload)
                return (
                    AnchorScreenAssessment(
                        locale="LT",
                        language="lt",
                        topic="brand",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        hard_stop_reasons=[],
                        summary="clean",
                        warnings=[],
                    ),
                    80,
                    8,
                )
            if text_format is LinkBatchAssessment:
                self.assertNotIn("anchors", payload)
                ids = [str(row[0]) for row in payload["rows"]]
                return (
                    LinkBatchAssessment(
                        pbn_risk="CLEAN",
                        pbn_reasons=[],
                        hard_stop_reasons=[],
                        quality_record_ids=ids,
                        article_record_ids=[],
                        old_record_ids=[],
                        modern_record_ids=ids,
                        borderline_record_ids=[],
                        fresh_record_ids=[],
                        unknown_age_record_ids=[],
                        spam_record_ids=[],
                    ),
                    200,
                    30,
                )
            raise AssertionError(f"unexpected schema: {text_format}")

        checker._parse = fake_parse
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            {"rows": make_rows(7, domain="drop.lt")},
            {"rows": [{"anchor": "brand", "referring_domains": 5}]},
            {"rows": [{"anchor": "best online casino bonus", "referring_domains": 1}]},
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.api_calls, 2)
        self.assertEqual(verdict.backlinks_sent, 7)
        self.assertEqual(verdict.input_tokens, 280)
        self.assertEqual(calls, [AnchorScreenAssessment, LinkBatchAssessment])

    def test_uncertain_sol_anchor_precheck_falls_back_to_combined_first_batch(self):
        checker = OpenAIDomainChecker.__new__(OpenAIDomainChecker)
        checker.client = object()
        checker.last_error = ""
        checker.model_notice = ""
        checker._model_access_checked = True
        checker.screen_model = "test-luna"
        checker.model = "test-sol"
        checker.screen_max_output_tokens = 900
        checker.batch_max_output_tokens = 2500
        checker.anchor_precheck_max_output_tokens = 700
        checker.batch_size = 10
        checker.max_risk_anchors = 100
        checker.enable_luna_screen = False
        checker.enable_sol_anchor_precheck = True
        checker.fetch_page_content = False
        calls = []

        def fake_parse(prompt, payload, text_format, max_output_tokens, model_name=None):
            calls.append(text_format)
            if text_format is AnchorScreenAssessment:
                return (
                    AnchorScreenAssessment(
                        locale="LT",
                        language="lt",
                        topic="brand",
                        anchor_risk="RISK",
                        anchor_reasons=["single suspicious anchor"],
                        hard_stop_reasons=[],
                        summary="uncertain",
                        warnings=[],
                    ),
                    80,
                    8,
                )
            if text_format is FirstBatchAssessment:
                self.assertIn("anchors", payload)
                ids = [str(row[0]) for row in payload["rows"]]
                return (
                    FirstBatchAssessment(
                        locale="LT",
                        language="lt",
                        topic="brand",
                        anchor_risk="CLEAN",
                        anchor_reasons=[],
                        pbn_risk="CLEAN",
                        pbn_reasons=[],
                        hard_stop_reasons=[],
                        quality_record_ids=ids,
                        article_record_ids=[],
                        old_record_ids=[],
                        modern_record_ids=ids,
                        borderline_record_ids=[],
                        fresh_record_ids=[],
                        unknown_age_record_ids=[],
                        spam_record_ids=[],
                    ),
                    200,
                    30,
                )
            raise AssertionError(f"unexpected schema: {text_format}")

        checker._parse = fake_parse
        verdict = checker.evaluate(
            "drop.lt",
            "LT",
            {"rows": make_rows(7, domain="drop.lt")},
            {"rows": [{"anchor": "brand", "referring_domains": 5}]},
            {"rows": [{"anchor": "best online casino bonus", "referring_domains": 1}]},
        )
        self.assertEqual(verdict.status, "GOOD")
        self.assertEqual(verdict.api_calls, 2)
        self.assertEqual(calls, [AnchorScreenAssessment, FirstBatchAssessment])

if __name__ == "__main__":
    unittest.main()
