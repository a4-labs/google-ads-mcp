# Write tools (A4 fork)

Namespace `write` (enabled in `ads_mcp/tools_config.yaml`). Every tool works in two steps:

1. `confirm=false` (default): operations are sent with `validate_only=true`. Nothing is saved. The tool returns a preview and a one-time `confirmation_id` (valid 15 minutes, bound to the exact parameters).
2. `confirm=true` + `confirmation_id`: the same parameters are executed.

## Required environment variables

| Variable | Value |
|---|---|
| `ADS_WRITE_ENABLED` | `true` (otherwise every write tool refuses) |
| `ADS_WRITE_ALLOWED_CUSTOMERS` | comma separated customer IDs, e.g. `4379790242` |

## Hard rules

- Campaigns, ad groups, ads, asset groups, budgets and conversion actions can never be removed or set to `REMOVED`. Only `ENABLED` / `PAUSED`.
- New campaigns, ad groups and ads are created `PAUSED` by default.
- Max 200 operations per call.
- Every executed change is logged to stdout as one JSON line (`"event": "ads_mcp_write"`).
- Pending previews are kept in memory: after a restart, or with more than one instance, redo the preview.

## Tools

| Tool | What it does |
|---|---|
| `negatives_add` | Negative keywords: account list, shared list, campaign or ad group |
| `criteria_remove` | Removes negative keywords, keywords, DSA webpage targets |
| `keywords_add` | Adds keywords to a Search ad group |
| `dsa_webpage_targets_add` | Adds DSA page targets or page exclusions |
| `pmax_text_assets_add` | Adds headlines / long headlines / descriptions to a PMax asset group |
| `pmax_image_assets_add` | Uploads images from URLs and links them to a PMax asset group |
| `pmax_video_assets_add` | Links YouTube videos to a PMax asset group |
| `asset_group_assets_remove` | Unlinks assets from a PMax asset group |
| `rsa_create` | Creates a Responsive Search Ad |
| `status_set` | ENABLED / PAUSED for campaigns, ad groups, ads, keywords, asset groups |
| `campaign_budget_set` | Daily budget (PLN) |
| `campaign_target_roas_set` | Target ROAS on Maximize Conversion Value campaigns |
| `conversion_action_set_primary` | Primary / secondary conversion action |
| `search_campaign_create` | New Search campaign with budget, location and language |
| `ad_group_create` | New ad group (standard or DSA) |
| `raw_mutate` | Any `GoogleAdsService.Mutate` operations (escape hatch, same guards) |
