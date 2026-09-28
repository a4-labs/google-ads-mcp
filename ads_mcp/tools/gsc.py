# Google Search Console tools for the A4 fork of google-ads-mcp.
#
# Read-only access to the Search Console API (webmasters v3), using the same
# Google OAuth token that FastMCP obtains for the Google Ads tools. Requires the
# https://www.googleapis.com/auth/webmasters.readonly scope (see coordinator.py)
# and the "Google Search Console API" enabled in the Google Cloud project.

"""Tools for reading Google Search Console data."""

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import requests
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

gsc_mcp = FastMCP("gsc")

_API = "https://www.googleapis.com/webmasters/v3"
_TIMEOUT = 60

Dimension = Literal["query", "page", "country", "device", "date", "searchAppearance"]


def _token() -> str:
    """Returns the Google access token of the signed-in user."""
    from fastmcp.server.dependencies import get_access_token

    token_obj = get_access_token()
    if not token_obj or not token_obj.token:
        raise ToolError(
            "No Google access token. Reconnect the connector and sign in again."
        )
    return token_obj.token


def _request(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.request(
        method,
        url,
        headers={"Authorization": f"Bearer {_token()}"},
        json=body,
        timeout=_TIMEOUT,
    )
    if response.status_code == 403 and "insufficient" in response.text.lower():
        raise ToolError(
            "Missing Search Console permission (webmasters.readonly scope). "
            "Disconnect and reconnect the connector to grant it."
        )
    if response.status_code >= 400:
        raise ToolError(f"Search Console API error {response.status_code}: {response.text[:800]}")
    return response.json() if response.text else {}


@gsc_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_sites() -> List[Dict[str, str]]:
    """Lists Search Console properties the signed-in user can access.

    Call this first to get the exact `site_url` for `search_analytics`.
    Domain properties look like `sc-domain:example.com`, URL-prefix
    properties like `https://example.com/`.

    Returns:
        List of {site_url, permission_level}.
    """
    data = _request("GET", f"{_API}/sites")
    return [
        {"site_url": s.get("siteUrl", ""), "permission_level": s.get("permissionLevel", "")}
        for s in data.get("siteEntry", [])
    ]


@gsc_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_analytics(
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: List[Dimension] = ["query"],
    row_limit: int = 1000,
    start_row: int = 0,
    search_type: Literal["web", "image", "video", "news", "discover", "googleNews"] = "web",
    filters: Optional[List[Dict[str, str]]] = None,
    data_state: Literal["final", "all"] = "final",
) -> Dict[str, Any]:
    """Queries Search Console performance data (clicks, impressions, CTR, position).

    Args:
        site_url: Exact property from `list_sites`, e.g. "sc-domain:a4academy.pl"
            or "https://a4academy.pl/".
        start_date: YYYY-MM-DD. Data is available for roughly the last 16 months.
        end_date: YYYY-MM-DD (inclusive). The last 2-3 days are usually incomplete.
        dimensions: Group rows by these, e.g. ["query"], ["page"], ["query", "page"],
            ["date"], ["query", "device"]. Order matters for the `keys` in each row.
        row_limit: 1-25000 rows per call. Page with `start_row` for more.
        start_row: Offset for paging (0-based).
        search_type: Search type, default "web".
        filters: Optional AND-combined filters, each
            {"dimension": "query"|"page"|"country"|"device",
             "operator": "contains"|"notContains"|"equals"|"notEquals"|"includingRegex"|"excludingRegex",
             "expression": "..."}.
            Example: [{"dimension": "query", "operator": "notContains", "expression": "a4"}]
        data_state: "final" (default) or "all" to include fresh, not yet final data.

    Returns:
        {"rows": [{"keys": [...], "clicks", "impressions", "ctr", "position"}],
         "row_count", "start_row", "has_more"}.
        Rows are sorted by clicks descending. Very low-volume (anonymized)
        queries are never returned by Google, so totals by query are lower than
        property totals.
    """
    row_limit = max(1, min(int(row_limit), 25000))
    body: Dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": list(dimensions),
        "rowLimit": row_limit,
        "startRow": max(0, int(start_row)),
        "type": search_type,
        "dataState": data_state,
    }
    if filters:
        body["dimensionFilterGroups"] = [
            {
                "groupType": "and",
                "filters": [
                    {
                        "dimension": f["dimension"],
                        "operator": f.get("operator", "contains"),
                        "expression": f["expression"],
                    }
                    for f in filters
                ],
            }
        ]

    url = f"{_API}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
    data = _request("POST", url, body)
    rows = [
        {
            "keys": r.get("keys", []),
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": round(r.get("ctr", 0.0), 4),
            "position": round(r.get("position", 0.0), 1),
        }
        for r in data.get("rows", [])
    ]
    return {
        "rows": rows,
        "row_count": len(rows),
        "start_row": body["startRow"],
        "has_more": len(rows) == row_limit,
    }
