# Keyword Planner tools for the A4 fork of google-ads-mcp.
#
# Read-only access to KeywordPlanIdeaService (the "Keyword Planner" in the
# Google Ads UI). Nothing is created or changed in the account: no keyword
# plans are saved, the service only returns ideas and search volumes.
#
# Defaults: Poland (geoTargetConstants/2616), Polish (languageConstants/1030),
# Google Search only (no search partners). Bids are returned in PLN.

"""Tools for keyword research (search volumes, competition, bid ranges)."""

from typing import Any, Dict, List, Literal, Optional, Union

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

keywords_mcp = FastMCP("keywords")

DEFAULT_GEO_IDS = [2616]  # Poland
DEFAULT_LANGUAGE_ID = 1030  # Polish
MAX_SEED_KEYWORDS = 20
MAX_HISTORICAL_KEYWORDS = 1000
MAX_IDEAS = 2000

Network = Literal["GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"]

_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11,
    "DECEMBER": 12,
}


def _service(client: Any) -> Any:
    return client.get_service(
        "KeywordPlanIdeaService", interceptors=[MCPHeaderInterceptor()]
    )


def _format_ads_error(ex: GoogleAdsException) -> str:
    lines = [f"Request ID: {ex.request_id}"]
    for error in ex.failure.errors:
        lines.append(f"Google Ads API Error: {error.message}")
    text = "\n".join(lines)
    if "DEVELOPER_TOKEN" in text.upper() or "not approved" in text.lower():
        text += (
            "\nHint: Keyword Planner needs a developer token with Basic or "
            "Standard access (Google Ads > Admin > API Center)."
        )
    return text


def _micros_to_pln(value: Any) -> Optional[float]:
    if not value:
        return None
    return round(int(value) / 1_000_000, 2)


def _enum_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "name", str(value))


def _metrics_to_dict(metrics: Any, include_monthly: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "avg_monthly_searches": int(metrics.avg_monthly_searches or 0),
        "competition": _enum_name(metrics.competition),
        "competition_index": int(metrics.competition_index or 0),
        "low_top_of_page_bid_pln": _micros_to_pln(metrics.low_top_of_page_bid_micros),
        "high_top_of_page_bid_pln": _micros_to_pln(metrics.high_top_of_page_bid_micros),
    }
    if include_monthly:
        monthly = []
        for m in metrics.monthly_search_volumes:
            month_name = _enum_name(m.month) or ""
            monthly.append(
                {
                    "year": int(m.year),
                    "month": _MONTHS.get(month_name, month_name),
                    "searches": int(m.monthly_searches or 0),
                }
            )
        out["monthly"] = monthly
    return out


def _fill_targeting(
    client: Any,
    request: Any,
    geo_target_ids: List[int],
    language_id: int,
    network: str,
) -> None:
    request.language = f"languageConstants/{int(language_id)}"
    request.geo_target_constants.extend(
        f"geoTargetConstants/{int(g)}" for g in geo_target_ids
    )
    request.keyword_plan_network = getattr(
        client.enums.KeywordPlanNetworkEnum, network
    )


@keywords_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ideas(
    customer_id: Union[str, int],
    seed_keywords: List[str] = [],
    page_url: Optional[str] = None,
    geo_target_ids: List[int] = DEFAULT_GEO_IDS,
    language_id: int = DEFAULT_LANGUAGE_ID,
    network: Network = "GOOGLE_SEARCH",
    min_monthly_searches: int = 0,
    limit: int = 300,
    include_monthly: bool = False,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Keyword Planner: new keyword ideas with search volume, competition and bids.

    Give seed keywords (up to 20), a page URL, or both. Read-only: nothing is
    created in the account. Defaults: Poland, Polish, Google Search only.

    Args:
        customer_id: Google Ads customer ID (used for quota/currency only).
        seed_keywords: e.g. ["kurs trenera personalnego", "kurs dietetyka online"].
        page_url: e.g. "https://a4academy.pl/vip/" – ideas based on the page content.
        geo_target_ids: geo target constant IDs (2616 = Poland).
        language_id: language constant ID (1030 = Polish).
        network: GOOGLE_SEARCH (default) or GOOGLE_SEARCH_AND_PARTNERS.
        min_monthly_searches: drop ideas below this average monthly volume.
        limit: max ideas returned, sorted by avg monthly searches (max 2000).
        include_monthly: add the last 12 months of searches per idea.

    Returns:
        {"count", "ideas": [{"text", "avg_monthly_searches", "competition",
        "competition_index", "low_top_of_page_bid_pln",
        "high_top_of_page_bid_pln", ["monthly"]}]}.
        Bids are the Keyword Planner's top-of-page bid range in PLN.
    """
    seeds = [k.strip() for k in seed_keywords if k and k.strip()]
    if not seeds and not page_url:
        raise ToolError("Podaj seed_keywords albo page_url (albo oba).")
    if len(seeds) > MAX_SEED_KEYWORDS:
        raise ToolError(f"Maksymalnie {MAX_SEED_KEYWORDS} słów startowych.")
    limit = max(1, min(int(limit), MAX_IDEAS))

    cid = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = cid
    request.include_adult_keywords = False
    _fill_targeting(client, request, geo_target_ids, language_id, network)

    if seeds and page_url:
        request.keyword_and_url_seed.url = page_url
        request.keyword_and_url_seed.keywords.extend(seeds)
    elif seeds:
        request.keyword_seed.keywords.extend(seeds)
    else:
        request.url_seed.url = page_url

    try:
        results = list(_service(client).generate_keyword_ideas(request=request))
    except GoogleAdsException as ex:
        raise ToolError(_format_ads_error(ex))

    ideas_out: List[Dict[str, Any]] = []
    for idea in results:
        row = {"text": idea.text}
        row.update(_metrics_to_dict(idea.keyword_idea_metrics, include_monthly))
        if row["avg_monthly_searches"] >= min_monthly_searches:
            ideas_out.append(row)
    ideas_out.sort(key=lambda r: r["avg_monthly_searches"], reverse=True)
    return {"count": len(ideas_out[:limit]), "total_found": len(ideas_out), "ideas": ideas_out[:limit]}


@keywords_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def volumes(
    customer_id: Union[str, int],
    keywords: List[str],
    geo_target_ids: List[int] = DEFAULT_GEO_IDS,
    language_id: int = DEFAULT_LANGUAGE_ID,
    network: Network = "GOOGLE_SEARCH",
    include_monthly: bool = True,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Keyword Planner: search volume and bids for exactly the keywords you give.

    Use to check a known list (up to 1000). Read-only. Defaults: Poland,
    Polish, Google Search only. Google may merge close variants (e.g. with and
    without Polish characters) into one row - see "close_variants".

    Returns:
        {"count", "keywords": [{"text", "close_variants", "avg_monthly_searches",
        "competition", "competition_index", "low_top_of_page_bid_pln",
        "high_top_of_page_bid_pln", ["monthly"]}]}.
    """
    words = [k.strip() for k in keywords if k and k.strip()]
    if not words:
        raise ToolError("Podaj co najmniej jedno słowo kluczowe.")
    if len(words) > MAX_HISTORICAL_KEYWORDS:
        raise ToolError(f"Maksymalnie {MAX_HISTORICAL_KEYWORDS} słów na raz.")

    cid = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = cid
    request.keywords.extend(words)
    request.include_adult_keywords = False
    _fill_targeting(client, request, geo_target_ids, language_id, network)

    try:
        response = _service(client).generate_keyword_historical_metrics(request=request)
    except GoogleAdsException as ex:
        raise ToolError(_format_ads_error(ex))

    rows: List[Dict[str, Any]] = []
    for result in response.results:
        row = {"text": result.text, "close_variants": list(result.close_variants)}
        row.update(_metrics_to_dict(result.keyword_metrics, include_monthly))
        rows.append(row)
    rows.sort(key=lambda r: r["avg_monthly_searches"], reverse=True)
    return {"count": len(rows), "keywords": rows}
