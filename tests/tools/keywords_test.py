"""Tests for the Keyword Planner tools (request building and output shape)."""

import unittest
from unittest import mock

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient

import ads_mcp.tools.keywords as keywords

CID = "4379790242"


def _real_client():
    return GoogleAdsClient(
        credentials=mock.Mock(), developer_token="x", use_proto_plus=True
    )


def _metrics(client, avg, comp="HIGH", low=1_500_000, high=6_000_000):
    m = client.get_type("KeywordPlanHistoricalMetrics")
    m.avg_monthly_searches = avg
    m.competition = getattr(client.enums.KeywordPlanCompetitionLevelEnum, comp)
    m.competition_index = 80
    m.low_top_of_page_bid_micros = low
    m.high_top_of_page_bid_micros = high
    v = client.get_type("MonthlySearchVolume")
    v.year = 2026
    v.month = client.enums.MonthOfYearEnum.SEPTEMBER
    v.monthly_searches = avg
    m.monthly_search_volumes.append(v)
    return m


class _FakeService:
    def __init__(self, client):
        self.client = client
        self.requests = []

    def generate_keyword_ideas(self, request):
        self.requests.append(request)
        out = []
        for text, avg in [("kurs trenera", 1000), ("trener kurs online", 50)]:
            r = self.client.get_type("GenerateKeywordIdeaResult")
            r.text = text
            r.keyword_idea_metrics = _metrics(self.client, avg)
            out.append(r)
        return out

    def generate_keyword_historical_metrics(self, request):
        self.requests.append(request)
        resp = self.client.get_type("GenerateKeywordHistoricalMetricsResponse")
        for text in request.keywords:
            r = self.client.get_type("GenerateKeywordHistoricalMetricsResult")
            r.text = text
            r.keyword_metrics = _metrics(self.client, 320)
            resp.results.append(r)
        return resp


class KeywordToolsTest(unittest.TestCase):
    def setUp(self):
        self.client = _real_client()
        self.service = _FakeService(self.client)
        self.client.get_service = mock.Mock(return_value=self.service)
        self.patch = mock.patch.object(
            keywords.utils, "get_googleads_client", return_value=self.client
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_ideas_keyword_seed_defaults(self):
        out = keywords.ideas(CID, seed_keywords=["kurs trenera"], min_monthly_searches=100)
        req = self.service.requests[0]
        self.assertEqual(req.customer_id, CID)
        self.assertEqual(req.language, "languageConstants/1030")
        self.assertEqual(list(req.geo_target_constants), ["geoTargetConstants/2616"])
        self.assertEqual(list(req.keyword_seed.keywords), ["kurs trenera"])
        self.assertEqual(out["count"], 1)
        idea = out["ideas"][0]
        self.assertEqual(idea["avg_monthly_searches"], 1000)
        self.assertEqual(idea["competition"], "HIGH")
        self.assertEqual(idea["low_top_of_page_bid_pln"], 1.5)
        self.assertEqual(idea["high_top_of_page_bid_pln"], 6.0)
        self.assertNotIn("monthly", idea)

    def test_ideas_keyword_and_url_seed(self):
        keywords.ideas(CID, seed_keywords=["vip"], page_url="https://a4academy.pl/vip/")
        req = self.service.requests[0]
        self.assertEqual(req.keyword_and_url_seed.url, "https://a4academy.pl/vip/")
        self.assertEqual(list(req.keyword_and_url_seed.keywords), ["vip"])

    def test_ideas_url_only(self):
        keywords.ideas(CID, page_url="https://a4academy.pl/vip/")
        self.assertEqual(self.service.requests[0].url_seed.url, "https://a4academy.pl/vip/")

    def test_ideas_requires_seed(self):
        with self.assertRaises(ToolError):
            keywords.ideas(CID)

    def test_ideas_too_many_seeds(self):
        with self.assertRaises(ToolError):
            keywords.ideas(CID, seed_keywords=[f"k{i}" for i in range(21)])

    def test_volumes(self):
        out = keywords.volumes(CID, keywords=["kurs dietetyka", " "])
        req = self.service.requests[0]
        self.assertEqual(list(req.keywords), ["kurs dietetyka"])
        self.assertEqual(out["count"], 1)
        row = out["keywords"][0]
        self.assertEqual(row["avg_monthly_searches"], 320)
        self.assertEqual(row["monthly"], [{"year": 2026, "month": 9, "searches": 320}])


class SearchBridgeTest(KeywordToolsTest):
    def test_search_ideas_bridge(self):
        import ads_mcp.tools.search as search_mod
        rows = search_mod.search(
            CID, fields=["kurs trenera"], resource="keyword_planner_ideas",
            conditions=["url=https://a4academy.pl/vip/", "min_searches=100"], limit=10,
        )
        req = self.service.requests[0]
        self.assertEqual(req.keyword_and_url_seed.url, "https://a4academy.pl/vip/")
        self.assertEqual(len(rows), 1)

    def test_search_volumes_bridge(self):
        import ads_mcp.tools.search as search_mod
        rows = search_mod.search(CID, fields=["kurs dietetyka"], resource="keyword_planner_volumes")
        self.assertEqual(rows[0]["avg_monthly_searches"], 320)


if __name__ == "__main__":
    unittest.main()
