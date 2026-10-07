# Write tools for the A4 fork of google-ads-mcp.
#
# Every tool works in two steps:
#   1. confirm=False (default): the operations are sent to the Google Ads API
#      with validate_only=True. Nothing is saved. The tool returns a readable
#      preview and a one-time confirmation_id (valid for 15 minutes).
#   2. confirm=True + confirmation_id: the exact same parameters are executed.
#
# Safety switches (environment variables):
#   ADS_WRITE_ENABLED=true             - without it every write tool refuses.
#   ADS_WRITE_ALLOWED_CUSTOMERS=123,.. - comma separated customer IDs that may
#                                        be modified. Others are refused.
#
# Hard rules: campaigns, ad groups, asset groups, budgets and conversion
# actions can never be removed (and never set to status REMOVED). Only
# ENABLED / PAUSED. Removing is allowed only for criteria (negative keywords,
# keywords, DSA webpage targets), for unlinking assets from asset groups and -
# through the dedicated ads_remove tool only - for ads that are already PAUSED.

"""Tools for modifying a Google Ads account (preview -> confirm)."""

import hashlib
import json
import os
import secrets
import time
from typing import Any, Callable, Dict, List, Literal, Optional, Union

import requests
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

write_mcp = FastMCP("write")

MAX_OPERATIONS = 200
CONFIRMATION_TTL_SECONDS = 15 * 60

# Text limits for Performance Max / RSA assets.
TEXT_LIMITS = {
    "HEADLINE": 30,
    "LONG_HEADLINE": 90,
    "DESCRIPTION": 90,
    "BUSINESS_NAME": 25,
}
# Max number of assets of a given text field type in one asset group.
ASSET_GROUP_MAX = {
    "HEADLINE": 15,
    "LONG_HEADLINE": 5,
    "DESCRIPTION": 5,
    "BUSINESS_NAME": 1,
}
RSA_HEADLINE_MAX_LEN = 30
RSA_DESCRIPTION_MAX_LEN = 90

# Operations whose "remove" (or status REMOVED) is forbidden.
_PROTECTED_OPERATIONS = {
    "campaign_operation",
    "ad_group_operation",
    "ad_group_ad_operation",
    "asset_group_operation",
    "campaign_budget_operation",
    "conversion_action_operation",
}

# In-memory store of previews waiting for confirmation.
_PENDING: Dict[str, Dict[str, Any]] = {}

MatchType = Literal["EXACT", "PHRASE", "BROAD"]
Status = Literal["ENABLED", "PAUSED"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_write_allowed(customer_id: str) -> None:
    if os.environ.get("ADS_WRITE_ENABLED", "").strip().lower() != "true":
        raise ToolError(
            "Zapis jest wyłączony. Ustaw ADS_WRITE_ENABLED=true na serwerze."
        )
    allowed = {
        utils.clean_customer_id(x)
        for x in os.environ.get("ADS_WRITE_ALLOWED_CUSTOMERS", "").split(",")
        if x.strip()
    }
    if customer_id not in allowed:
        raise ToolError(
            f"Konto {customer_id} nie jest na liście ADS_WRITE_ALLOWED_CUSTOMERS."
        )


def _fingerprint(tool: str, params: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": tool, "params": params}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cleanup_pending() -> None:
    now = time.time()
    for key in [k for k, v in _PENDING.items() if v["expires"] < now]:
        _PENDING.pop(key, None)


def _consume_confirmation(fp: str, confirmation_id: Optional[str]) -> None:
    _cleanup_pending()
    if not confirmation_id:
        raise ToolError(
            "confirm=True wymaga confirmation_id z podglądu (wywołaj najpierw z confirm=False)."
        )
    entry = _PENDING.pop(confirmation_id, None)
    if entry is None:
        raise ToolError(
            "Nieznany lub wygasły confirmation_id. Zrób podgląd jeszcze raz."
        )
    if entry["fp"] != fp:
        raise ToolError(
            "Parametry różnią się od podglądu. Zrób podgląd jeszcze raz z tymi parametrami."
        )


def _issue_confirmation(fp: str) -> str:
    _cleanup_pending()
    confirmation_id = secrets.token_urlsafe(9)
    _PENDING[confirmation_id] = {
        "fp": fp,
        "expires": time.time() + CONFIRMATION_TTL_SECONDS,
    }
    return confirmation_id


def _enum_name(message: Any, field: str) -> Optional[str]:
    """Returns the enum value name of `field` on a raw protobuf message."""
    descriptor = message.DESCRIPTOR.fields_by_name.get(field)
    if descriptor is None or descriptor.enum_type is None:
        return None
    value = getattr(message, field)
    enum_value = descriptor.enum_type.values_by_number.get(value)
    return enum_value.name if enum_value else None


def _guard_operations(ops: List[Any], allow_remove: frozenset = frozenset()) -> None:
    """Raises if an operation would remove a protected object.

    `allow_remove` lists operation fields whose "remove" is permitted for the
    calling tool (used only by ads_remove, which checks the ads itself).
    """
    for op in ops:
        op_field = op._pb.WhichOneof("operation")
        if op_field is None:
            raise ToolError("Pusta operacja w mutate.")
        inner = getattr(op._pb, op_field)
        kind = inner.WhichOneof("operation")
        if op_field in _PROTECTED_OPERATIONS:
            if kind == "remove" and op_field not in allow_remove:
                raise ToolError(
                    f"Usuwanie ({op_field}) jest zablokowane. Użyj statusu PAUSED."
                )
            if kind in ("create", "update"):
                obj = getattr(inner, kind)
                if _enum_name(obj, "status") == "REMOVED":
                    raise ToolError(
                        f"Status REMOVED ({op_field}) jest zablokowany. Użyj PAUSED."
                    )


def _format_ads_error(ex: GoogleAdsException) -> str:
    lines = [f"Request ID: {ex.request_id}"]
    for error in ex.failure.errors:
        location = ""
        if error.location and error.location.field_path_elements:
            location = " @ " + ".".join(
                el.field_name
                + (f"[{el.index}]" if el.index is not None and el.index >= 0 else "")
                for el in error.location.field_path_elements
            )
        lines.append(f"Google Ads API Error: {error.message}{location}")
    return "\n".join(lines)


def _gaql(client: Any, customer_id: str, query: str) -> List[Any]:
    service = client.get_service(
        "GoogleAdsService", interceptors=[MCPHeaderInterceptor()]
    )
    try:
        rows: List[Any] = []
        for batch in service.search_stream(customer_id=customer_id, query=query):
            rows.extend(batch.results)
        return rows
    except GoogleAdsException as ex:
        raise ToolError(_format_ads_error(ex))


def _result_resource_names(response: Any) -> List[str]:
    names: List[str] = []
    for mor in response.mutate_operation_responses:
        field = mor._pb.WhichOneof("response")
        if field is None:
            names.append("")
            continue
        result = getattr(mor._pb, field)
        names.append(getattr(result, "resource_name", ""))
    return names


def _run(
    tool: str,
    customer_id: Union[str, int],
    params: Dict[str, Any],
    build: Callable[[Any, str], tuple],
    confirm: bool,
    confirmation_id: Optional[str],
    login_customer_id: Union[str, int, None] = None,
    allow_remove: frozenset = frozenset(),
) -> Dict[str, Any]:
    """Shared preview/confirm flow.

    `build(client, customer_id)` returns (operations, summary_lines, warnings).
    """
    customer_id = utils.clean_customer_id(customer_id)
    _check_write_allowed(customer_id)
    fp = _fingerprint(tool, {"customer_id": customer_id, **params})
    if confirm:
        _consume_confirmation(fp, confirmation_id)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ops, summary, warnings = build(client, customer_id)
    if not ops:
        raise ToolError("Brak operacji do wykonania.")
    if len(ops) > MAX_OPERATIONS:
        raise ToolError(
            f"Za dużo operacji ({len(ops)}). Maksimum na jedno wywołanie: {MAX_OPERATIONS}."
        )
    _guard_operations(ops, allow_remove)

    service = client.get_service(
        "GoogleAdsService", interceptors=[MCPHeaderInterceptor()]
    )
    try:
        request = client.get_type("MutateGoogleAdsRequest")
        request.customer_id = customer_id
        request.mutate_operations.extend(ops)
        request.validate_only = not confirm
        response = service.mutate(request=request)
    except GoogleAdsException as ex:
        raise ToolError(_format_ads_error(ex))

    if not confirm:
        return {
            "mode": "preview",
            "saved": False,
            "validated_by_google": True,
            "operation_count": len(ops),
            "operations": summary,
            "warnings": warnings,
            "confirmation_id": _issue_confirmation(fp),
            "next_step": (
                "Pokaż podgląd użytkownikowi. Po jego zgodzie wywołaj to samo "
                "narzędzie z identycznymi parametrami oraz confirm=True i tym confirmation_id."
            ),
        }

    results = _result_resource_names(response)
    print(
        json.dumps(
            {
                "event": "ads_mcp_write",
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tool": tool,
                "customer_id": customer_id,
                "params": params,
                "results": results,
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
    return {
        "mode": "executed",
        "saved": True,
        "operation_count": len(ops),
        "operations": summary,
        "warnings": warnings,
        "results": results,
    }


def _new_op(client: Any) -> Any:
    return client.get_type("MutateOperation")


def _set_update_mask(client: Any, operation: Any, obj: Any) -> None:
    client.copy_from(
        operation.update_mask, protobuf_helpers.field_mask(None, obj._pb)
    )


def _check_text(text: str, max_len: int, label: str) -> None:
    if not text or not text.strip():
        raise ToolError(f"Pusty tekst ({label}).")
    if len(text) > max_len:
        raise ToolError(
            f"{label} „{text}” ma {len(text)} znaków, limit {max_len}."
        )


# ---------------------------------------------------------------------------
# Negative keywords
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def negatives_add(
    customer_id: Union[str, int],
    level: Literal["account", "shared_set", "campaign", "ad_group"],
    keywords: List[Dict[str, str]],
    target_id: Union[str, int, None] = None,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Adds negative keywords. Preview first (confirm=False), then confirm.

    Args:
        customer_id: Google Ads customer ID.
        level: "account" (account-level negative keyword list, applies to all
            Search and PMax campaigns), "shared_set" (a shared negative keyword
            list; target_id = shared set ID), "campaign" (target_id = campaign
            ID) or "ad_group" (target_id = ad group ID).
        keywords: list of {"text": "...", "match_type": "EXACT"|"PHRASE"|"BROAD"}.
            Text without quotes or brackets.
        target_id: ID of the shared set / campaign / ad group (not for "account").
        confirm: False = validate and preview only, True = save.
        confirmation_id: required with confirm=True (from the preview).

    Example: negatives_add(customer_id="4379790242", level="account",
        keywords=[{"text": "logowanie", "match_type": "PHRASE"}])
    """
    params = {"level": level, "keywords": keywords, "target_id": target_id}

    def build(client, cid):
        if level != "account" and not target_id:
            raise ToolError("target_id jest wymagany dla tego poziomu.")
        shared_set_rn = None
        if level == "account":
            rows = _gaql(
                client,
                cid,
                "SELECT shared_set.resource_name FROM shared_set "
                "WHERE shared_set.type = 'ACCOUNT_LEVEL_NEGATIVE_KEYWORDS' "
                "AND shared_set.status = 'ENABLED'",
            )
            if not rows:
                raise ToolError(
                    "Konto nie ma listy wykluczeń na poziomie konta. Utwórz ją w panelu (Admin → Account settings → Negative keywords)."
                )
            shared_set_rn = rows[0].shared_set.resource_name
        elif level == "shared_set":
            shared_set_rn = f"customers/{cid}/sharedSets/{target_id}"

        ops, summary = [], []
        for kw in keywords:
            text = (kw.get("text") or "").strip().strip('"[]')
            match = (kw.get("match_type") or "").upper()
            if match not in ("EXACT", "PHRASE", "BROAD"):
                raise ToolError(f"Zły match_type dla „{text}”: {match}")
            _check_text(text, 80, "Słowo kluczowe")
            op = _new_op(client)
            if shared_set_rn:
                crit = op.shared_criterion_operation.create
                crit.shared_set = shared_set_rn
            elif level == "campaign":
                crit = op.campaign_criterion_operation.create
                crit.campaign = f"customers/{cid}/campaigns/{target_id}"
                crit.negative = True
            else:
                crit = op.ad_group_criterion_operation.create
                crit.ad_group = f"customers/{cid}/adGroups/{target_id}"
                crit.negative = True
            crit.keyword.text = text
            crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match]
            ops.append(op)
            summary.append(f"Dodaj wykluczenie [{level} {target_id or ''}] {match}: {text}")
        return ops, summary, []

    return _run("negatives_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def criteria_remove(
    customer_id: Union[str, int],
    resource_names: List[str],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Removes criteria: negative keywords, keywords, DSA webpage targets.

    Args:
        resource_names: full resource names, e.g.
            customers/123/campaignCriteria/111~222,
            customers/123/adGroupCriteria/333~444,
            customers/123/sharedCriteria/555~666.
            Get them with the search tool first.
        confirm / confirmation_id: preview -> confirm flow.
    """
    params = {"resource_names": resource_names}

    def build(client, cid):
        ops, summary = [], []
        for rn in resource_names:
            parts = rn.split("/")
            if len(parts) != 4 or parts[0] != "customers" or parts[1] != cid:
                raise ToolError(f"Zły resource_name: {rn}")
            kind = parts[2]
            op = _new_op(client)
            if kind == "campaignCriteria":
                op.campaign_criterion_operation.remove = rn
            elif kind == "adGroupCriteria":
                op.ad_group_criterion_operation.remove = rn
            elif kind == "sharedCriteria":
                op.shared_criterion_operation.remove = rn
            else:
                raise ToolError(
                    f"criteria_remove obsługuje tylko campaignCriteria, adGroupCriteria i sharedCriteria, nie: {kind}"
                )
            ops.append(op)
            summary.append(f"Usuń kryterium {rn}")
        return ops, summary, []

    return _run("criteria_remove", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def ads_remove(
    customer_id: Union[str, int],
    resource_names: List[str],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Removes ads (ad_group_ad) permanently. Only ads that are already PAUSED.

    Enabled ads are refused - pause them first with status_set. Campaigns, ad
    groups, asset groups, budgets and conversion actions still cannot be
    removed by any tool.

    Args:
        resource_names: full ad resource names, e.g.
            customers/123/adGroupAds/111~222 (ad group ID ~ ad ID).
            Get them with the search tool first.
        confirm / confirmation_id: preview -> confirm flow.
    """
    params = {"resource_names": resource_names}

    def build(client, cid):
        names = []
        for rn in resource_names:
            parts = rn.split("/")
            if (
                len(parts) != 4
                or parts[0] != "customers"
                or parts[1] != cid
                or parts[2] != "adGroupAds"
                or "~" not in parts[3]
            ):
                raise ToolError(
                    f"Zły resource_name reklamy: {rn} (oczekiwano customers/{cid}/adGroupAds/<grupa>~<reklama>)"
                )
            names.append(rn)
        if len(set(names)) != len(names):
            raise ToolError("Powtórzone resource_name na liście.")
        quoted = ", ".join(f"'{rn}'" for rn in names)
        rows = _gaql(
            client,
            cid,
            "SELECT ad_group_ad.resource_name, ad_group_ad.status FROM ad_group_ad "
            f"WHERE ad_group_ad.resource_name IN ({quoted})",
        )
        statuses = {}
        for row in rows:
            status = row.ad_group_ad.status
            statuses[row.ad_group_ad.resource_name] = getattr(status, "name", str(status))
        ops, summary = [], []
        for rn in names:
            status = statuses.get(rn)
            if status is None:
                raise ToolError(f"Nie znaleziono reklamy {rn}.")
            if status != "PAUSED":
                raise ToolError(
                    f"Reklama {rn} ma status {status}. Najpierw wstrzymaj ją (PAUSED), potem usuń."
                )
            op = _new_op(client)
            op.ad_group_ad_operation.remove = rn
            ops.append(op)
            summary.append(f"Usuń wstrzymaną reklamę {rn}")
        return ops, summary, []

    return _run(
        "ads_remove",
        customer_id,
        params,
        build,
        confirm,
        confirmation_id,
        login_customer_id,
        allow_remove=frozenset({"ad_group_ad_operation"}),
    )


# ---------------------------------------------------------------------------
# Keywords (positive)
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def keywords_add(
    customer_id: Union[str, int],
    ad_group_id: Union[str, int],
    keywords: List[Dict[str, str]],
    status: Status = "ENABLED",
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Adds (positive) keywords to a Search ad group.

    Args:
        keywords: list of {"text": "...", "match_type": "EXACT"|"PHRASE"|"BROAD"}.
    """
    params = {"ad_group_id": ad_group_id, "keywords": keywords, "status": status}

    def build(client, cid):
        ops, summary = [], []
        for kw in keywords:
            text = (kw.get("text") or "").strip().strip('"[]')
            match = (kw.get("match_type") or "").upper()
            if match not in ("EXACT", "PHRASE", "BROAD"):
                raise ToolError(f"Zły match_type dla „{text}”: {match}")
            _check_text(text, 80, "Słowo kluczowe")
            op = _new_op(client)
            crit = op.ad_group_criterion_operation.create
            crit.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
            crit.status = client.enums.AdGroupCriterionStatusEnum[status]
            crit.keyword.text = text
            crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match]
            ops.append(op)
            summary.append(f"Dodaj słowo kluczowe [grupa {ad_group_id}] {match}: {text}")
        return ops, summary, []

    return _run("keywords_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# DSA webpage targets
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def dsa_webpage_targets_add(
    customer_id: Union[str, int],
    ad_group_id: Union[str, int],
    targets: List[Dict[str, str]],
    negative: bool = False,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Adds webpage targets (or exclusions) to a Dynamic Search Ads ad group.

    Args:
        ad_group_id: ID of the SEARCH_DYNAMIC_ADS ad group.
        targets: list of {"url": "https://a4academy.pl/kurs/x/",
            "rule": "URL_EQUALS"|"URL_CONTAINS", "name": "optional label"}.
        negative: True = exclude these pages instead of targeting them.
    """
    params = {"ad_group_id": ad_group_id, "targets": targets, "negative": negative}

    def build(client, cid):
        ops, summary = [], []
        for t in targets:
            url = (t.get("url") or "").strip()
            rule = (t.get("rule") or "URL_EQUALS").upper()
            if not url:
                raise ToolError("Pusty url w targets.")
            if rule not in ("URL_EQUALS", "URL_CONTAINS"):
                raise ToolError(f"Zła reguła {rule} (URL_EQUALS albo URL_CONTAINS).")
            op = _new_op(client)
            crit = op.ad_group_criterion_operation.create
            crit.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
            if negative:
                crit.negative = True
            else:
                crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            crit.webpage.criterion_name = (t.get("name") or url)[:255]
            cond = client.get_type("WebpageConditionInfo")
            cond.operand = client.enums.WebpageConditionOperandEnum.URL
            cond.operator = (
                client.enums.WebpageConditionOperatorEnum.EQUALS
                if rule == "URL_EQUALS"
                else client.enums.WebpageConditionOperatorEnum.CONTAINS
            )
            cond.argument = url
            crit.webpage.conditions.append(cond)
            ops.append(op)
            summary.append(
                f"{'Wyklucz' if negative else 'Kieruj na'} stronę [{rule}] {url} (grupa DSA {ad_group_id})"
            )
        return ops, summary, []

    return _run("dsa_webpage_targets_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# Performance Max assets
# ---------------------------------------------------------------------------


def _asset_group_counts(client: Any, cid: str, asset_group_id: Union[str, int]) -> Dict[str, int]:
    rows = _gaql(
        client,
        cid,
        "SELECT asset_group_asset.field_type FROM asset_group_asset "
        f"WHERE asset_group.id = {int(asset_group_id)} "
        "AND asset_group_asset.status != 'REMOVED'",
    )
    counts: Dict[str, int] = {}
    for r in rows:
        name = r.asset_group_asset.field_type.name
        counts[name] = counts.get(name, 0) + 1
    return counts


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def pmax_text_assets_add(
    customer_id: Union[str, int],
    asset_group_id: Union[str, int],
    field_type: Literal["HEADLINE", "LONG_HEADLINE", "DESCRIPTION", "BUSINESS_NAME"],
    texts: List[str],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Adds text assets to a Performance Max asset group.

    Limits: HEADLINE max 30 chars (max 15 per group), LONG_HEADLINE 90 (max 5),
    DESCRIPTION 90 (max 5, at least one must be <= 60), BUSINESS_NAME 25 (max 1).
    The preview reports how many assets of this type the group will have.
    To replace a text: remove the old one with asset_group_assets_remove, then add.
    """
    params = {"asset_group_id": asset_group_id, "field_type": field_type, "texts": texts}

    def build(client, cid):
        max_len = TEXT_LIMITS[field_type]
        for t in texts:
            _check_text(t, max_len, field_type)
        counts = _asset_group_counts(client, cid, asset_group_id)
        current = counts.get(field_type, 0)
        warnings = []
        if current + len(texts) > ASSET_GROUP_MAX[field_type]:
            raise ToolError(
                f"Grupa ma już {current} × {field_type}, limit to {ASSET_GROUP_MAX[field_type]}. "
                "Najpierw usuń stare (asset_group_assets_remove)."
            )
        ops, summary = [], []
        for i, text in enumerate(texts, start=1):
            temp_rn = f"customers/{cid}/assets/-{i}"
            asset_op = _new_op(client)
            asset = asset_op.asset_operation.create
            asset.resource_name = temp_rn
            asset.text_asset.text = text
            link_op = _new_op(client)
            link = link_op.asset_group_asset_operation.create
            link.asset = temp_rn
            link.asset_group = f"customers/{cid}/assetGroups/{asset_group_id}"
            link.field_type = client.enums.AssetFieldTypeEnum[field_type]
            ops.extend([asset_op, link_op])
            summary.append(f"Dodaj {field_type} ({len(text)} zn.): {text}")
        summary.append(
            f"Po zmianie grupa {asset_group_id} będzie mieć {current + len(texts)} × {field_type}."
        )
        return ops, summary, warnings

    return _run("pmax_text_assets_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def pmax_image_assets_add(
    customer_id: Union[str, int],
    asset_group_id: Union[str, int],
    field_type: Literal[
        "MARKETING_IMAGE",
        "SQUARE_MARKETING_IMAGE",
        "PORTRAIT_MARKETING_IMAGE",
        "LOGO",
        "LANDSCAPE_LOGO",
    ],
    image_urls: List[str],
    name_prefix: str = "MCP",
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Downloads images from public URLs, uploads them and links them to a PMax asset group.

    Aspect ratios: MARKETING_IMAGE 1.91:1 (min 600x314), SQUARE_MARKETING_IMAGE
    1:1 (min 300x300), PORTRAIT_MARKETING_IMAGE 4:5 (min 480x600), LOGO 1:1
    (min 128x128), LANDSCAPE_LOGO 4:1 (min 512x128). Max 5 MB per image.
    Google validates the dimensions in the preview step.
    """
    params = {
        "asset_group_id": asset_group_id,
        "field_type": field_type,
        "image_urls": image_urls,
        "name_prefix": name_prefix,
    }

    def build(client, cid):
        ops, summary = [], []
        stamp = time.strftime("%Y%m%d")
        for i, url in enumerate(image_urls, start=1):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
            except Exception as ex:  # noqa: BLE001
                raise ToolError(f"Nie udało się pobrać obrazu {url}: {ex}")
            data = resp.content
            if len(data) > 5 * 1024 * 1024:
                raise ToolError(f"Obraz {url} ma ponad 5 MB.")
            temp_rn = f"customers/{cid}/assets/-{i}"
            asset_op = _new_op(client)
            asset = asset_op.asset_operation.create
            asset.resource_name = temp_rn
            asset.name = f"{name_prefix} {field_type} {stamp} {i} {hashlib.md5(data).hexdigest()[:6]}"
            asset.image_asset.data = data
            link_op = _new_op(client)
            link = link_op.asset_group_asset_operation.create
            link.asset = temp_rn
            link.asset_group = f"customers/{cid}/assetGroups/{asset_group_id}"
            link.field_type = client.enums.AssetFieldTypeEnum[field_type]
            ops.extend([asset_op, link_op])
            summary.append(f"Dodaj {field_type}: {url} ({len(data)//1024} KB)")
        return ops, summary, []

    return _run("pmax_image_assets_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def pmax_video_assets_add(
    customer_id: Union[str, int],
    asset_group_id: Union[str, int],
    youtube_video_ids: List[str],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Links YouTube videos (by video ID, e.g. "dQw4w9WgXcQ") to a PMax asset group."""
    params = {"asset_group_id": asset_group_id, "youtube_video_ids": youtube_video_ids}

    def build(client, cid):
        ops, summary = [], []
        for i, vid in enumerate(youtube_video_ids, start=1):
            vid = vid.strip()
            temp_rn = f"customers/{cid}/assets/-{i}"
            asset_op = _new_op(client)
            asset = asset_op.asset_operation.create
            asset.resource_name = temp_rn
            asset.name = f"YT {vid}"
            asset.youtube_video_asset.youtube_video_id = vid
            link_op = _new_op(client)
            link = link_op.asset_group_asset_operation.create
            link.asset = temp_rn
            link.asset_group = f"customers/{cid}/assetGroups/{asset_group_id}"
            link.field_type = client.enums.AssetFieldTypeEnum.YOUTUBE_VIDEO
            ops.extend([asset_op, link_op])
            summary.append(f"Dodaj film YouTube {vid}")
        return ops, summary, []

    return _run("pmax_video_assets_add", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def asset_group_assets_remove(
    customer_id: Union[str, int],
    resource_names: List[str],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Unlinks assets (texts, images, videos) from PMax asset groups.

    The asset itself stays in the asset library. Use for removing outdated
    promotions, wrong texts or auto-generated videos.

    Args:
        resource_names: asset_group_asset resource names, format
            customers/{cid}/assetGroupAssets/{asset_group_id}~{asset_id}~{field_type}
            (get them via search on asset_group_asset).
    """
    params = {"resource_names": resource_names}

    def build(client, cid):
        ops, summary = [], []
        for rn in resource_names:
            if not rn.startswith(f"customers/{cid}/assetGroupAssets/"):
                raise ToolError(f"Zły resource_name: {rn}")
            op = _new_op(client)
            op.asset_group_asset_operation.remove = rn
            ops.append(op)
            summary.append(f"Odepnij komponent {rn}")
        return ops, summary, [
            "Google odrzuci zmianę, jeśli grupa spadnie poniżej minimum (3 nagłówki, 1 długi nagłówek, 2 opisy, 1 obraz poziomy, 1 kwadratowy, 1 logo)."
        ]

    return _run("asset_group_assets_remove", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# Responsive Search Ads
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def rsa_create(
    customer_id: Union[str, int],
    ad_group_id: Union[str, int],
    final_url: str,
    headlines: List[Union[str, Dict[str, str]]],
    descriptions: List[str],
    path1: Optional[str] = None,
    path2: Optional[str] = None,
    status: Status = "PAUSED",
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Creates a Responsive Search Ad (default PAUSED).

    Args:
        headlines: 3-15 items, each max 30 chars. Plain string or
            {"text": "...", "pin": "HEADLINE_1"|"HEADLINE_2"|"HEADLINE_3"}.
        descriptions: 2-4 items, each max 90 chars.
        path1 / path2: display URL paths, max 15 chars each.
    RSA texts cannot be edited: create a new ad, then pause the old one with status_set.
    """
    params = {
        "ad_group_id": ad_group_id,
        "final_url": final_url,
        "headlines": headlines,
        "descriptions": descriptions,
        "path1": path1,
        "path2": path2,
        "status": status,
    }

    def build(client, cid):
        if not 3 <= len(headlines) <= 15:
            raise ToolError("RSA wymaga 3–15 nagłówków.")
        if not 2 <= len(descriptions) <= 4:
            raise ToolError("RSA wymaga 2–4 opisów.")
        for p in (path1, path2):
            if p and len(p) > 15:
                raise ToolError(f"Ścieżka „{p}” ma ponad 15 znaków.")
        op = _new_op(client)
        aga = op.ad_group_ad_operation.create
        aga.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        aga.status = client.enums.AdGroupAdStatusEnum[status]
        aga.ad.final_urls.append(final_url)
        summary = [f"Nowa reklama RSA [{status}] w grupie {ad_group_id} → {final_url}"]
        for h in headlines:
            text = h if isinstance(h, str) else h.get("text", "")
            pin = None if isinstance(h, str) else h.get("pin")
            _check_text(text, RSA_HEADLINE_MAX_LEN, "Nagłówek RSA")
            asset = client.get_type("AdTextAsset")
            asset.text = text
            if pin:
                asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[pin]
            aga.ad.responsive_search_ad.headlines.append(asset)
            summary.append(f"  Nagłówek{f' [{pin}]' if pin else ''}: {text}")
        for d in descriptions:
            _check_text(d, RSA_DESCRIPTION_MAX_LEN, "Opis RSA")
            asset = client.get_type("AdTextAsset")
            asset.text = d
            aga.ad.responsive_search_ad.descriptions.append(asset)
            summary.append(f"  Opis: {d}")
        if path1:
            aga.ad.responsive_search_ad.path1 = path1
        if path2:
            aga.ad.responsive_search_ad.path2 = path2
        return [op], summary, []

    return _run("rsa_create", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# Status, budget, bidding, conversions
# ---------------------------------------------------------------------------

_STATUS_TARGETS = {
    "campaigns": ("campaign_operation", "CampaignStatusEnum"),
    "adGroups": ("ad_group_operation", "AdGroupStatusEnum"),
    "adGroupAds": ("ad_group_ad_operation", "AdGroupAdStatusEnum"),
    "adGroupCriteria": ("ad_group_criterion_operation", "AdGroupCriterionStatusEnum"),
    "assetGroups": ("asset_group_operation", "AssetGroupStatusEnum"),
}


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def status_set(
    customer_id: Union[str, int],
    resource_names: List[str],
    status: Status,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Enables or pauses campaigns, ad groups, ads, keywords or asset groups.

    Args:
        resource_names: e.g. customers/123/campaigns/456,
            customers/123/adGroups/789, customers/123/adGroupAds/789~111,
            customers/123/adGroupCriteria/789~222, customers/123/assetGroups/333.
        status: ENABLED or PAUSED (REMOVED is not allowed).
    """
    params = {"resource_names": resource_names, "status": status}

    def build(client, cid):
        ops, summary = [], []
        for rn in resource_names:
            parts = rn.split("/")
            if len(parts) != 4 or parts[0] != "customers" or parts[1] != cid:
                raise ToolError(f"Zły resource_name: {rn}")
            target = _STATUS_TARGETS.get(parts[2])
            if not target:
                raise ToolError(f"status_set nie obsługuje typu {parts[2]}.")
            op_field, enum_name = target
            op = _new_op(client)
            sub = getattr(op, op_field)
            obj = sub.update
            obj.resource_name = rn
            obj.status = getattr(client.enums, enum_name)[status]
            _set_update_mask(client, sub, obj)
            ops.append(op)
            summary.append(f"Ustaw {status}: {rn}")
        return ops, summary, []

    return _run("status_set", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def campaign_budget_set(
    customer_id: Union[str, int],
    campaign_id: Union[str, int],
    daily_amount_pln: float,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Sets the average daily budget of a campaign (in the account currency, PLN).

    The preview shows old -> new budget and warns when the budget is shared
    with other campaigns.
    """
    params = {"campaign_id": campaign_id, "daily_amount_pln": daily_amount_pln}

    def build(client, cid):
        if daily_amount_pln <= 0:
            raise ToolError("Budżet musi być większy od 0.")
        rows = _gaql(
            client,
            cid,
            "SELECT campaign.name, campaign_budget.resource_name, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}",
        )
        if not rows:
            raise ToolError(f"Nie znaleziono kampanii {campaign_id}.")
        row = rows[0]
        budget_rn = row.campaign_budget.resource_name
        old = row.campaign_budget.amount_micros / 1e6
        warnings = []
        if row.campaign_budget.explicitly_shared:
            shared = _gaql(
                client,
                cid,
                "SELECT campaign.name FROM campaign "
                f"WHERE campaign.campaign_budget = '{budget_rn}' AND campaign.status != 'REMOVED'",
            )
            warnings.append(
                "Budżet współdzielony z: " + ", ".join(r.campaign.name for r in shared)
            )
        op = _new_op(client)
        sub = op.campaign_budget_operation
        obj = sub.update
        obj.resource_name = budget_rn
        obj.amount_micros = int(round(daily_amount_pln * 100)) * 10000
        _set_update_mask(client, sub, obj)
        summary = [
            f"Budżet dzienny „{row.campaign.name}”: {old:.2f} zł → {daily_amount_pln:.2f} zł"
        ]
        return [op], summary, warnings

    return _run("campaign_budget_set", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def campaign_target_roas_set(
    customer_id: Union[str, int],
    campaign_id: Union[str, int],
    target_roas: Optional[float],
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Sets (or clears) target ROAS on a Maximize Conversion Value campaign.

    Args:
        target_roas: e.g. 2.0 = 200%. None clears the target (pure Maximize
            Conversion Value). Works only for campaigns already using
            MAXIMIZE_CONVERSION_VALUE; other strategy changes go through raw_mutate.
    """
    params = {"campaign_id": campaign_id, "target_roas": target_roas}

    def build(client, cid):
        rows = _gaql(
            client,
            cid,
            "SELECT campaign.name, campaign.bidding_strategy_type, "
            "campaign.maximize_conversion_value.target_roas FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}",
        )
        if not rows:
            raise ToolError(f"Nie znaleziono kampanii {campaign_id}.")
        row = rows[0]
        if row.campaign.bidding_strategy_type.name != "MAXIMIZE_CONVERSION_VALUE":
            raise ToolError(
                f"Kampania używa {row.campaign.bidding_strategy_type.name}. To narzędzie obsługuje tylko MAXIMIZE_CONVERSION_VALUE."
            )
        old = row.campaign.maximize_conversion_value.target_roas or None
        op = _new_op(client)
        sub = op.campaign_operation
        obj = sub.update
        obj.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        if target_roas is not None:
            if target_roas <= 0:
                raise ToolError("target_roas musi być > 0 albo None.")
            obj.maximize_conversion_value.target_roas = float(target_roas)
        sub.update_mask.paths.append("maximize_conversion_value.target_roas")
        summary = [
            f"Docelowy ROAS „{row.campaign.name}”: {old if old else 'brak'} → {target_roas if target_roas else 'brak'}"
        ]
        return [op], summary, [
            "Zmiana docelowego ROAS uruchamia ponowną naukę algorytmu; zmieniaj o max ~20% naraz."
        ]

    return _run("campaign_target_roas_set", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def conversion_action_set_primary(
    customer_id: Union[str, int],
    conversion_action_id: Union[str, int],
    primary_for_goal: bool,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Makes a conversion action primary (used for bidding) or secondary.

    The preview lists which actions will be primary in the same category.
    """
    params = {"conversion_action_id": conversion_action_id, "primary_for_goal": primary_for_goal}

    def build(client, cid):
        rows = _gaql(
            client,
            cid,
            "SELECT conversion_action.name, conversion_action.category, "
            "conversion_action.primary_for_goal FROM conversion_action "
            f"WHERE conversion_action.id = {int(conversion_action_id)}",
        )
        if not rows:
            raise ToolError(f"Nie znaleziono konwersji {conversion_action_id}.")
        ca = rows[0].conversion_action
        same = _gaql(
            client,
            cid,
            "SELECT conversion_action.name, conversion_action.primary_for_goal FROM conversion_action "
            f"WHERE conversion_action.category = '{ca.category.name}' "
            "AND conversion_action.status = 'ENABLED'",
        )
        primaries = [
            r.conversion_action.name
            for r in same
            if r.conversion_action.primary_for_goal and r.conversion_action.name != ca.name
        ]
        if primary_for_goal:
            primaries.append(ca.name)
        op = _new_op(client)
        sub = op.conversion_action_operation
        obj = sub.update
        obj.resource_name = f"customers/{cid}/conversionActions/{conversion_action_id}"
        obj.primary_for_goal = primary_for_goal
        sub.update_mask.paths.append("primary_for_goal")
        summary = [
            f"„{ca.name}” ({ca.category.name}): {'główna' if ca.primary_for_goal else 'dodatkowa'} → {'główna' if primary_for_goal else 'dodatkowa'}",
            f"Główne w kategorii {ca.category.name} po zmianie: {', '.join(primaries) or 'brak'}",
        ]
        return [op], summary, []

    return _run("conversion_action_set_primary", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# Creating campaigns and ad groups
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def search_campaign_create(
    customer_id: Union[str, int],
    name: str,
    daily_budget_pln: float,
    bidding: Literal["MAXIMIZE_CONVERSION_VALUE", "MAXIMIZE_CONVERSIONS"] = "MAXIMIZE_CONVERSION_VALUE",
    target_roas: Optional[float] = None,
    geo_target_ids: List[int] = [2616],
    language_ids: List[int] = [1030],
    status: Status = "PAUSED",
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Creates a Search campaign (Google Search only, no partners, no Display).

    Creates a non-shared budget, the campaign, location and language targeting.
    Defaults: Poland (2616), Polish (1030), status PAUSED.
    Then add ad groups (ad_group_create), keywords (keywords_add) and ads (rsa_create).
    """
    params = {
        "name": name,
        "daily_budget_pln": daily_budget_pln,
        "bidding": bidding,
        "target_roas": target_roas,
        "geo_target_ids": geo_target_ids,
        "language_ids": language_ids,
        "status": status,
    }

    def build(client, cid):
        if daily_budget_pln <= 0:
            raise ToolError("Budżet musi być większy od 0.")
        budget_rn = f"customers/{cid}/campaignBudgets/-1"
        campaign_rn = f"customers/{cid}/campaigns/-2"
        ops, summary = [], []

        op = _new_op(client)
        budget = op.campaign_budget_operation.create
        budget.resource_name = budget_rn
        budget.name = f"{name} – budżet {time.strftime('%Y-%m-%d %H:%M')}"
        budget.amount_micros = int(round(daily_budget_pln * 100)) * 10000
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        ops.append(op)

        op = _new_op(client)
        camp = op.campaign_operation.create
        camp.resource_name = campaign_rn
        camp.name = name
        camp.status = client.enums.CampaignStatusEnum[status]
        camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        camp.campaign_budget = budget_rn
        camp.network_settings.target_google_search = True
        camp.network_settings.target_search_network = False
        camp.network_settings.target_content_network = False
        camp.network_settings.target_partner_search_network = False
        camp.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
        if bidding == "MAXIMIZE_CONVERSION_VALUE":
            if target_roas:
                camp.maximize_conversion_value.target_roas = float(target_roas)
            else:
                client.copy_from(
                    camp.maximize_conversion_value,
                    client.get_type("MaximizeConversionValue"),
                )
        else:
            client.copy_from(
                camp.maximize_conversions, client.get_type("MaximizeConversions")
            )
        ops.append(op)

        for geo in geo_target_ids:
            op = _new_op(client)
            crit = op.campaign_criterion_operation.create
            crit.campaign = campaign_rn
            crit.location.geo_target_constant = f"geoTargetConstants/{int(geo)}"
            ops.append(op)
        for lang in language_ids:
            op = _new_op(client)
            crit = op.campaign_criterion_operation.create
            crit.campaign = campaign_rn
            crit.language.language_constant = f"languageConstants/{int(lang)}"
            ops.append(op)

        summary.append(
            f"Nowa kampania Search „{name}” [{status}], budżet {daily_budget_pln:.2f} zł/dzień, "
            f"{bidding}{f' tROAS {target_roas}' if target_roas else ''}, "
            f"lokalizacje {geo_target_ids}, języki {language_ids}, tylko wyszukiwarka Google"
        )
        return ops, summary, []

    return _run("search_campaign_create", customer_id, params, build, confirm, confirmation_id, login_customer_id)


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def ad_group_create(
    customer_id: Union[str, int],
    campaign_id: Union[str, int],
    name: str,
    type: Literal["SEARCH_STANDARD", "SEARCH_DYNAMIC_ADS"] = "SEARCH_STANDARD",
    status: Status = "PAUSED",
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Creates an ad group in a Search campaign (default PAUSED)."""
    params = {"campaign_id": campaign_id, "name": name, "type": type, "status": status}

    def build(client, cid):
        op = _new_op(client)
        ag = op.ad_group_operation.create
        ag.name = name
        ag.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        ag.status = client.enums.AdGroupStatusEnum[status]
        ag.type_ = client.enums.AdGroupTypeEnum[type]
        return [op], [f"Nowa grupa reklam „{name}” ({type}) [{status}] w kampanii {campaign_id}"], []

    return _run("ad_group_create", customer_id, params, build, confirm, confirmation_id, login_customer_id)


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------


@write_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def raw_mutate(
    customer_id: Union[str, int],
    operations: List[Dict[str, Any]],
    description: str,
    confirm: bool = False,
    confirmation_id: Optional[str] = None,
    login_customer_id: Union[str, int, None] = None,
) -> Dict[str, Any]:
    """Runs arbitrary GoogleAdsService.Mutate operations (escape hatch).

    Use only when no dedicated tool exists (e.g. creating a Performance Max
    campaign, a DSA ad, changing a bidding strategy). Same rules apply:
    preview first, then confirm; removing campaigns / ad groups / ads / asset
    groups / budgets / conversion actions is blocked here. To remove paused
    ads use the dedicated ads_remove tool.

    Args:
        operations: list of MutateOperation objects as JSON dicts (field names
            as in the API, snake_case or camelCase), e.g.
            [{"campaign_operation": {"update": {"resource_name": "customers/1/campaigns/2",
              "status": "PAUSED"}, "update_mask": "status"}}]
            Temporary IDs (negative numbers) may be used to link new objects.
        description: one sentence describing what this change does (logged).
    """
    params = {"operations": operations, "description": description}

    def build(client, cid):
        mutate_cls = type(client.get_type("MutateOperation"))
        ops = []
        for i, payload in enumerate(operations):
            try:
                ops.append(mutate_cls.from_json(json.dumps(payload)))
            except Exception as ex:  # noqa: BLE001
                raise ToolError(f"Operacja {i}: niepoprawny JSON MutateOperation: {ex}")
        summary = [f"raw_mutate: {description}"] + [
            json.dumps(p, ensure_ascii=False)[:500] for p in operations
        ]
        return ops, summary, []

    return _run("raw_mutate", customer_id, params, build, confirm, confirmation_id, login_customer_id)
