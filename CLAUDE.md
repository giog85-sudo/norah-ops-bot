# Norah Ops Bot — Project Context

## What This Project Is

A Telegram bot that serves as the operational hub for **Norah**, a restaurant in Madrid, Spain. It handles daily sales data entry, operational notes, owner reporting, and an intelligent alert system. Staff enter data via Telegram messages; owners receive structured nightly reports and anomaly alerts. An AI agent (Claude) answers natural-language questions from owners against the live database.

---

## Working Agreements

When making changes to the codebase, update CLAUDE.md as part of the same work — not as a follow-up. Specifically:

- New endpoints, fields, or schema columns → document under the relevant architecture section
- New defensive patterns, helpers, or invariants → document in the appropriate algorithm/logic section
- Behavior changes that affect output, math, or API contracts → update the affected section AND add a changelog entry with today's date
- Bug fixes that reveal something non-obvious about the system → add the lesson learned (so the same pitfall isn't rediscovered later)

End every architectural change session by checking whether CLAUDE.md still reflects current system state. If not, fix it before considering the work done.

CLAUDE.md is the briefing for the next session — keeping it fresh is part of the work, not after the work.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3 (async) |
| Telegram | `python-telegram-bot` with `JobQueue` |
| Database | PostgreSQL via `psycopg3` |
| AI agent | Anthropic Claude API (`anthropic`) |
| Timezone | `zoneinfo` (Europe/Madrid default) |
| Hosting | Single-process bot; env-var configured |

---

## Telegram Chat Roles

Assigned via `/setchatrole`. Stored in `chat_roles` table.

| Role constant | Purpose | Capabilities |
|---|---|---|
| `ROLE_OPS_ADMIN` | Admin/setup chat | All commands; reset DB; delete days; all data entry |
| `ROLE_OWNERS_SILENT` | Owners read-only | Receives daily posts, weekly digests, evening alerts; rejects input |
| `ROLE_MANAGER_INPUT` | Staff data entry | Submit full daily reports; auto-note saving; analytics |
| `ROLE_OWNERS_REQUESTS` | Owner AI chat | Natural-language queries answered by Claude agent |

Legacy: `OWNERS_CHAT_IDS` key in `settings` table still supported for backward compatibility.

---

## Database Tables

All tables in PostgreSQL. Connection via `get_conn()`.

### `daily_stats`
Simple daily KPIs (legacy/fallback).
- `day` DATE PK
- `sales` FLOAT
- `covers` INT
- `created_at` TIMESTAMPTZ

### `full_daily_stats`
Rich daily breakdown with lunch/dinner split. Primary data table.
- `day` DATE PK
- `total_sales`, `visa`, `cash`, `tips` FLOAT
- `lunch_sales` FLOAT, `lunch_pax` INT, `lunch_walkins` INT, `lunch_noshows` INT
- `dinner_sales` FLOAT, `dinner_pax` INT, `dinner_walkins` INT, `dinner_noshows` INT
- `created_at` TIMESTAMPTZ
- **Event columns** (added 2026-06-01, all idempotent `ALTER TABLE IF NOT EXISTS`):
  - `z_total_sales` FLOAT — Z-report total (includes venue fee); canonical revenue for event days
  - `transferencia` FLOAT — Transferencia payment total from Z-report closeouts
  - `event_pax` INT — Guest count from `LineType=="Menú"` Agora line item
  - `event_menu_total` FLOAT — Revenue from event menu line item
  - `event_timeframe` TEXT — TimeFrame of event (`"Noche"`, `"Mediodía"`, `"Tarde"`)
  - `venue_fee` FLOAT — `z_total_sales − total_net`, €1 tolerance (0 on most event days)
  - `event_in_cm` BOOLEAN NOT NULL DEFAULT TRUE — whether event guests are already counted in CoverManager's `dinner_pax`/`lunch_pax`

**`event_in_cm` flag:** Default `TRUE` means event pax is part of the regular CM cover count and should not be added again. Set `FALSE` only for historical external events where guests were not booked through CM. Currently only **2026-05-25** is `FALSE` (36 external guests).

**Angie's operational policy (confirmed 2026-06-01):** All future events are registered in CoverManager as standard reservations. The `TRUE` default is the permanent norm going forward. `FALSE` will never be needed for new event days — it exists only to correctly represent the single historical exception on 2026-05-25.

**Cover math throughout the codebase:**
`total_covers = lunch_pax + dinner_pax + (event_pax IF NOT event_in_cm ELSE 0)`

**`upsert_full_day()` ON CONFLICT behaviour:** All columns are updated on conflict **except** `event_in_cm`, which is only set on initial INSERT. Subsequent pipeline re-runs preserve any manually-set flag value.

### `notes_entries`
Operational notes/incidents tagged by day.
- `id` SERIAL PK
- `day` DATE
- `chat_id`, `user_id` BIGINT
- `text` TEXT
- `created_at` TIMESTAMPTZ
- Index on `day`

### `settings`
Key-value config store.
- `key` TEXT PK
- `value` TEXT
- Used for `OWNERS_CHAT_IDS` (legacy chat role config)

### `chat_roles`
Role assignments per chat.
- `chat_id` BIGINT PK
- `role` TEXT
- `chat_type` TEXT
- `title` TEXT
- `updated_at` TIMESTAMPTZ
- Index on `role`

SQL upserts use `ON CONFLICT DO UPDATE`. All queries use parameterised `%s` placeholders.

### `daily_product_sales`
Agora line items aggregated by (day, product, timeframe). Populated by `/run-pipeline?save=true`.
- `report_day` DATE — NOT NULL
- `product` TEXT — NOT NULL
- `family` TEXT — product family (nullable)
- `timeframe` TEXT — NOT NULL (raw Agora TimeFrame value, e.g. `"Mediodía"`, `"Noche"`)
- `quantity` NUMERIC — units sold
- `net` NUMERIC — net revenue
- `gross` NUMERIC — gross revenue
- PRIMARY KEY: `(report_day, product, timeframe)`
- Index on `report_day`

Pipeline upsert: DELETE + INSERT (not merge), so each `/run-pipeline?save=true` run replaces the day's rows cleanly.

### `daily_server_sales`
Server revenue aggregated by (day, user_name) with lunch/dinner split. Populated by `/run-pipeline?save=true`.
- `report_day` DATE — NOT NULL
- `user_name` TEXT — NOT NULL (Agora `User` field, `"Unknown"` if blank)
- `lunch_revenue` NUMERIC
- `lunch_covers` INTEGER — distinct `DocumentId` count for lunch shift (NULL if no document IDs available)
- `dinner_revenue` NUMERIC
- `dinner_covers` INTEGER — distinct `DocumentId` count for dinner shift (NULL if no document IDs available)
- `total_revenue` NUMERIC
- `tips` NUMERIC DEFAULT 0 — server's individual tip total from `GetTipsByUserReportRequest`, matched by `UserName`. Added 2026-06-04 via idempotent `ADD COLUMN IF NOT EXISTS`. Rows written before the migration stay at 0 until next pipeline run.
- PRIMARY KEY: `(report_day, user_name)`
- Index on `report_day`

TimeFrame classification mirrors `_LUNCH_FRAMES_BOT` / `_DINNER_FRAMES_BOT` constants defined in bot.py (same sets as agora_integration.py `_LUNCH_FRAMES`/`_DINNER_FRAMES` plus `"día"/"dia"`).

**EXTRAS filtering:** `user_name == "EXTRAS"` (case-insensitive) is excluded from all `/api/dashboard/servers` response sections. The row is still written to the DB; exclusion is at query time.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | required | Telegram bot API token |
| `DATABASE_URL` | required | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | `""` | Claude API key for owner AI agent |
| `TZ_NAME` / `TIMEZONE` | `Europe/Madrid` | Timezone for scheduling and business-day cutoff |
| `CUTOFF_HOUR` | `11` | Before this hour (local), "today" = yesterday's business day |
| `WEEKLY_DIGEST_HOUR` | `9` | Monday morning digest send hour |
| `DAILY_POST_HOUR` | `11` | Daily owners report send hour |
| `DAILY_POST_MINUTE` | `5` | Daily owners report send minute |
| `ALERT_EVENING_HOUR` | `21` | Evening anomaly alert send hour |
| `ALERT_LUNCH_TICKET_MIN` | `35` | Lunch avg ticket minimum (EUR) |
| `ALERT_NOSHOW_MULTIPLIER` | `2.0` | No-show spike: fires if >= N× same-weekday average |
| `ALERT_SERVICE_IMBALANCE_PCT` | `65` | One service >= this % of revenue triggers imbalance alert |
| `ALERT_REVENUE_VS_COVERS_DROP_PCT` | `20` | Revenue drop alert when covers are normal but sales drop this % |
| `ALERT_TIPS_DROP_PCT` | `30` | Tips % drop vs same-weekday avg to trigger alert |
| `ALERT_TICKET_EROSION_DAYS` | `3` | Consecutive same-weekday days of declining avg ticket to flag |
| `ALERT_STRONG_DAY_MISS_PCT` | `25` | Fri/Sat revenue shortfall threshold (%) |
| `ALERT_WEEK_PACE_PCT` | `25` | Monday week-pace warning: drop vs last Monday (%) |
| `ALERT_POSITIVE_REVENUE_PCT` | `15` | Dinner turnaround positive alert threshold (%) |
| `ALERT_POSITIVE_COVERS_PCT` | `10` | Covers top-percentile positive alert threshold |
| `ALERT_TOP_PERCENTILE` | `10` | Top-N% revenue percentile for positive alert |
| `ACCESS_MODE` | `RESTRICTED` | `OPEN` or `RESTRICTED` |
| `ALLOWED_USER_IDS` | `""` | Comma-separated user IDs allowed in RESTRICTED mode |

---

## Scheduled Jobs

All run via `JobQueue` respecting `TZ_NAME`. Three jobs:

| Job | Schedule | Recipients | Function |
|---|---|---|---|
| `daily_post_to_owners` | Daily @ `DAILY_POST_HOUR:DAILY_POST_MINUTE` | `ROLE_OWNERS_SILENT` chats | Full daily report (sales, tips, notes, payment split) |
| `weekly_digest_monday` | Monday @ `WEEKLY_DIGEST_HOUR:00` | `ROLE_OWNERS_SILENT` chats | Week's sales/covers summary |
| `evening_alerts` | Daily @ `ALERT_EVENING_HOUR:00` | `ROLE_OWNERS_SILENT` chats | Anomaly alerts for previous business day |

---

## Agora POS Integration

### Event Detection

Norah occasionally hosts private events or large group bookings with a pre-agreed menu. Detection rule: any line item with `LineType == "Menú"` in the `GetSalesAnalyticsReportRequest` response (vs `LineType == "Línea"` for regular à la carte items).

- **Confirmed by Angie (2026-06-01):** `LineType == "Menú"` fires whenever there is a pre-agreed set menu with a client — this covers both private venue buyouts and group bookings. Norah has **no menú del día**, so the rule cannot false-positive on regular service.
- Product name on all known event lines: `MENU NORAH`. Categories: `MENUS EVENTOS`.
- From the Menú line item: `Quantity` → `event_pax`, `Net` → `event_menu_total`, `TimeFrame` → `event_timeframe` (`"Noche"` / `"Mediodía"` / `"Tarde"`).
- Detection runs inside `get_daily_sales()` in `agora_integration.py`, after the closeout enrichment block. Results are stored on the `DailySales` dataclass.
- Verified zero false positives across all 8 historical event days (2026-02-01 → 2026-05-25).

### Revenue Source: Z Report (`GetPosCloseOutsRequest`)

- `z_total_sales` = `TotalSales` summed across `PosCloseOuts` records matching `BusinessDay == date`. Query window is `date → date+2` to catch records closed after midnight.
- Regular days: `z_total_sales == lunch_sales + dinner_sales` (verified against Angie's manual reports).
- Event days: `z_total_sales > lunch + dinner` by the **venue fee** amount, because venue fees are billed at POS-ticket level and do not appear as Agora line items.
- `venue_fee = z_total_sales − total_net` with €1 rounding tolerance (`abs(raw) < 1.0 → treat as 0`).
- `transferencia`: summed from each closeout's `Payments` array where `MethodName` contains `"tranferencia"` or `"transferencia"`. **Agora's API misspells it without the 's'** — always match both defensively; always display the correct spelling `"Transferencia"` to owners.

### Daily Post — Event-Aware Template

On event days three conditional fragments are injected. On non-event days the post is **byte-for-byte identical** to the standard template.

**(A)** `"Of which Transferencia: X (event)"` — one line directly under `Total Sales Day`. Only when `transferencia > 0`.

**(B)** `"Transferencia: X"` — payment line between `Cash` and `Tips`. Only when `transferencia > 0`.

**(C)** Event block between the Dinner block and `📝 Notes:`. Only when `event_menu_total > 0`:
```
🎉 Event (Noche):
Menu (36 pax): 2448,00
Venue fee: 1500,00    ← omit if venue_fee == 0
Total: 3948,00
```

Silent value adjustments on event days (transparent to readers):
- `Total Sales Day` uses `z_total_sales` (not `lunch + dinner`); equal on non-event days → no-op.
- The shift matching `event_timeframe` subtracts `event_menu_total` from its displayed sales figure.
- Headline `Avg Ticket` = `(lunch_sales + dinner_sales − event_menu_total) / total_covers` (regular consumption per CM cover).
- Shift avg tickets use the same event-adjusted sales divided by CM pax.

### Data Source Separation

| Metric | Source | Notes |
|---|---|---|
| `lunch_pax`, `dinner_pax`, `total_covers` | CoverManager | Authoritative cover count |
| `lunch_walkins`, `dinner_walkins` | CoverManager | Walk-in scan (`prov ∈ {"walk in","walk-in","walkin"}`, `status ∈ {5,9}`) |
| `lunch_noshows`, `dinner_noshows` | CoverManager | No-show scan (`status=-3`, same-day, `last_update_status ≥ 12:00`) |
| `event_pax` | Agora POS — Menú line `Quantity` | **Never** added to CM counts |
| `event_menu_total`, `event_timeframe` | Agora POS — Menú line `Net`, `TimeFrame` | — |
| `venue_fee`, `transferencia`, `z_total_sales` | Agora Z report (`GetPosCloseOutsRequest`) | Derived from closeout |
| Shift sales (`lunch_net`, `dinner_net`) | Agora POS — line items grouped by `TimeFrame` | Includes event menu revenue before adjustment |

`event_pax` is tracked separately and added to `total_covers` only when `event_in_cm = FALSE` (i.e., external guests not booked through CM). For the standard case (`event_in_cm = TRUE`) event guests are already counted inside `lunch_pax`/`dinner_pax` and must not be double-counted.

### `/preview-post` Endpoint

`GET /preview-post?date=YYYY-MM-DD` — auth-protected (same Bearer token as other Flask endpoints). Returns the rendered daily post as `text/plain`. **Does not send to Telegram. Does not write to DB.**

Implementation: calls `build_owners_post_for_day(report_day, dry_run=True)`. When `dry_run=True`:
- `get_full_day()` is skipped (`full_row = None`) — any cached DB row is ignored.
- The function always goes through the live Agora + CM fetch, which has full event detection and the `*(Agora POS)*` annotation.

Use this any time you need to inspect a day's post before resending a corrected version to owners.

### `/run-pipeline?date=YYYY-MM-DD` Endpoint

`GET /run-pipeline?date=YYYY-MM-DD[&save=true]` — auth-protected. Fetches live Agora + CoverManager data for the given date and returns the computed `DailySales` object as JSON. **Does not write to DB by default.**

Adding `?save=true` opts in to DB persistence:
- If a row already exists: does a full UPDATE of all live fields from fresh Agora + CM data (`total_sales`, `visa`, `cash`, `tips`, `lunch_sales`, `lunch_pax`, `lunch_walkins`, `lunch_noshows`, `dinner_sales`, `dinner_pax`, `dinner_walkins`, `dinner_noshows`, `z_total_sales`, `transferencia`, `event_pax`, `event_menu_total`, `event_timeframe`, `venue_fee`). `event_in_cm` is the only field excluded — it is never overwritten by pipeline runs.
- If no row exists: does a full INSERT including CM data via `upsert_full_day()`.

Use this to backfill or refresh any historical day. Safe to re-run: CM data comes from the live API, and `event_in_cm` is always preserved.

### `/admin/event-flag?date=YYYY-MM-DD` Endpoint

Auth-protected. GET/POST for reading and patching event metadata.

**GET** — returns current values: `event_in_cm`, `event_pax`, `venue_fee`, `z_total_sales`, `event_menu_total`, `event_timeframe` for the date.

**POST** — patches one or more fields. Accepted body params:
- `value` (`"true"` / `"false"`) — sets `event_in_cm`
- Integer fields: `lunch_pax`, `dinner_pax`, `lunch_walkins`, `dinner_walkins`, `lunch_noshows`, `dinner_noshows`

Only provided fields are updated; unspecified fields are untouched. `event_in_cm` is never overwritten by pipeline re-runs — only by this endpoint.

### Verified Reference — 2026-05-25 (First Event Day)

`event_in_cm = FALSE` (36 external guests not booked through CoverManager).

| Field | Value |
|---|---|
| `z_total_sales` | 5784.60 |
| `transferencia` | 3948.00 |
| `event_pax` | 36 |
| `event_menu_total` | 2448.00 |
| `event_timeframe` | `"Noche"` |
| `venue_fee` | 1500.00 |
| `lunch_pax` | 16 |
| `dinner_pax` | 24 |
| CM covers | 16 + 24 = 40 |
| Regular consumption | 794.60 + (3490.00 − 2448.00) = 1836.60 |

**Daily post context** (reported to owners):
- Covers displayed = 40 (CM only)
- Avg ticket = 1836.60 / 40 = **45.92**

**Dashboard / AI agent context** (analytics):
- `total_covers` = 16 + 24 + 36 = **76** (event_pax added because `event_in_cm = FALSE`)
- `total_sales` = **5784.60** (full revenue including event menu + venue fee)
- `avg_ticket` = **45.91** — event-excluded: regular consumption (1836.60) / regular covers (40). Note: `avg_ticket ≠ total_sales / total_covers` (5784.60 / 76 = 76.11) — this identity is intentionally broken; see Avg Ticket Metric Definition section.

Reconciles exactly with Angie's manual report.

---

## Alert System Logic

`send_evening_alerts` fires for the **previous business day** if a `full_daily_stats` record exists. Comparisons use same-weekday history (up to 5 prior occurrences).

### Negative alerts

| # | Alert | Condition |
|---|---|---|
| 1 | Dinner no-show spike | `dinner_noshows >= ALERT_NOSHOW_MULTIPLIER × same-weekday avg` |
| 2 | Service imbalance | Lunch or dinner alone >= `ALERT_SERVICE_IMBALANCE_PCT`% of daily revenue |
| 3 | Revenue drop (normal covers) | Covers >= 85% of avg but revenue drops >= `ALERT_REVENUE_VS_COVERS_DROP_PCT`% |
| 4 | Tips % drop | Tips as % of sales drops >= `ALERT_TIPS_DROP_PCT`% vs same-weekday avg |
| 5a | Ticket erosion | Lunch or dinner avg ticket declines `ALERT_TICKET_EROSION_DAYS` consecutive same-weekday occurrences |
| 5b | Lunch ticket floor | `lunch_avg < ALERT_LUNCH_TICKET_MIN` (absolute minimum, not trend) |
| 6 | Fri/Sat underperformance | Fri or Sat revenue <= `-(ALERT_STRONG_DAY_MISS_PCT)`% vs same-weekday avg |
| 7 | Week-pace warning | Monday only: revenue <= `-(ALERT_WEEK_PACE_PCT)`% vs previous Monday |

### Positive alerts

| # | Alert | Condition |
|---|---|---|
| 8 | Top-percentile revenue day | `total_sales` >= top `ALERT_TOP_PERCENTILE`% of all history (needs ≥10 records) |
| 9 | Top-percentile covers day | `covers` >= top `ALERT_POSITIVE_COVERS_PCT`% of all history (needs ≥10 records) |
| 10 | Dinner turnaround | Dinner avg ticket bounces >= `ALERT_POSITIVE_REVENUE_PCT`% vs prev-3 same-weekday avg |

If no alerts fire, a "no anomalies" message is sent. Alerts only send if data exists for the day.

`send_evening_alerts` unpacks all 19 fields from `get_full_day()` and uses event-aware values:
- Revenue = `z_total_sales` if > 0, else `total_sales`
- `total_covers = lunch_pax + dinner_pax + (event_pax IF NOT event_in_cm ELSE 0)`

---

## Avg Ticket Metric Definition

`avg_ticket`, `lunch_avg_ticket`, and `dinner_avg_ticket` are **event-excluded** metrics. They represent regular F&B spend per regular cover — not distorted by fixed event pricing.

```
regular_lunch_sales   = lunch_sales  − event_menu_total  (if lunch event, else 0)
regular_dinner_sales  = dinner_sales − event_menu_total  (if dinner/Noche event, else 0)
regular_lunch_covers  = lunch_pax   − event_pax  (if event_in_cm AND lunch event, else 0)
regular_dinner_covers = dinner_pax  − event_pax  (if event_in_cm AND Noche event, else 0)

lunch_avg_ticket  = regular_lunch_sales  / regular_lunch_covers
dinner_avg_ticket = regular_dinner_sales / regular_dinner_covers
avg_ticket        = (regular_lunch_sales + regular_dinner_sales)
                  / (regular_lunch_covers + regular_dinner_covers)
```

For non-event days (`event_timeframe` empty) all terms are zero and the formula collapses to the original `sales / pax` calculation.

The shared implementation is `_regular_shift_metrics(lunch_sales, lunch_pax, dinner_sales, dinner_pax, event_pax, event_menu_total, event_timeframe, event_in_cm)` in `bot.py`. It is called from `_agent_row_to_dict`, `_sum_period_rows`, `_fmt_snapshot`, and both dashboard endpoints.

**`total_covers` and `total_sales` are unchanged** — they always show the full picture (all people in seats, full revenue including venue fees). This intentionally breaks the `avg_ticket = total_sales / total_covers` identity. Events get their own block in the daily Telegram post with explicit per-pax pricing; the avg_ticket metric is reserved for the regular dining business.

---

## Dashboard — UI Structure

Single-file SPA (`dashboard.html`). Three tabs managed by `switchTab(name)`:

| Tab ID | Button | Content |
|---|---|---|
| `tab-overview` | Overview (default) | KPI cards, all charts, Monthly Summary, Booking Sources |
| `tab-fb` | F&B | 6 sections driven by `GET /api/dashboard/products` |
| `tab-staff` | Staff | Server leaderboard analytics (Phase 3d — not yet built) |

Tab state persists in `localStorage` under key `norah_active_tab`. On page load, the last-selected tab is restored (default: `overview`).

Period selector (`#period-controls`) is visible on Overview and F&B tabs; hidden on Staff. The Apply button calls `loadCurrentTab()`, which dispatches to `loadAll()` (Overview) or `loadFBTab()` (F&B).

**`_fbLoaded` flag**: set `true` on first successful `loadFBTab()`. `switchTab('fb')` skips the fetch on re-visit; direct calls from period controls always re-fetch.

### F&B tab sections (all fed by `/api/dashboard/products`)

| Section | Element(s) | Data field |
|---|---|---|
| KPI — Total Products Sold | `#fb-kpi-sold` | Deduped union of `top_by_quantity` + `slow_movers` quantities |
| KPI — Total F&B Revenue | `#fb-kpi-revenue` | Sum of `family_mix[].net` |
| KPI — Food vs Drinks | `#fb-kpi-food-amt/pct`, `#fb-kpi-drinks-amt/pct` | `food_revenue` / `drinks_revenue`; pct computed client-side |
| KPI — Menu Coverage | `#fb-kpi-coverage`, `#fb-kpi-coverage-sub` | `distinct_products_in_period / active_menu_size * 100`; shows "N/A" if `active_menu_size` is null |
| KPI — Non-Product Revenue | `#fb-kpi-nonprod`, `#fb-kpi-nonprod-sub1/2` | `non_product_revenue_total`; sub-lines show venue fees (N events) and other adjustments separately |
| Top 20 Food by Revenue | `#chart-fb-food-rev` (horizontal bar, 560px, `--lunch`) | `food_top_by_revenue`, value key `net` |
| Top 20 Drinks by Revenue | `#chart-fb-drinks-rev` (horizontal bar, 560px, `--dinner`) | `drinks_top_by_revenue`, value key `net` |
| Top 20 Food by Quantity | `#chart-fb-food-qty` (horizontal bar, 560px, `--lunch`) | `food_top_by_quantity`, value key `quantity` |
| Top 20 Drinks by Quantity | `#chart-fb-drinks-qty` (horizontal bar, 560px, `--dinner`) | `drinks_top_by_quantity`, value key `quantity` |
| Family Mix | `#chart-fb-family` (doughnut, 260px) | `family_mix`, `FAM_PALETTE` colors |
| Slow Movers | `#fb-slowmovers-tbody` (scrollable table 296px max) | `slow_movers` (bottom 20 by quantity) |
| Lunch / Dinner Top 20 | `#chart-fb-lunch`, `#chart-fb-dinner` (horizontal bar, 480px) | `lunch_top[:20]`, `dinner_top[:20]` |

**Food vs Drinks card:** Two-column layout with `--lunch` (red) label for Food and `--dinner` (teal) for Drinks, separated by a `--border` vertical divider. Percentages computed as `food_revenue / (food + drinks) * 100`.

**Color convention (food/drinks):** `--lunch` (#FF6B6B coral) = Food / CARTA. `--dinner` (#4ECDC4 teal) = Drinks / non-CARTA. Applied consistently across the Food vs Drinks KPI card and all four food/drinks top-10 charts.

**Menu Coverage card:** `active_menu_size` is the baseline — unique products sold in the trailing 90 days ending at `period_end`. Shows percentage of that baseline sold during the selected period. Useful for spotting menu items that haven't moved recently.

`renderHBar(canvasId, items, color, valueKey, tooltipExtra)`: shared renderer for all horizontal bar charts. Truncates product names > 30 chars. Tooltip shows revenue or units + family + pct_of_total + any `tooltipExtra` lines.

## Dashboard HTTP API

Flask endpoints served alongside the bot. All require `Authorization: Bearer <token>`.

### `/api/stats/daily?from=YYYY-MM-DD&to=YYYY-MM-DD`

Returns per-day rows. All event-aware:
- **`total_sales`**: `COALESCE(NULLIF(z_total_sales, 0), total_sales)` — full revenue including event menu and venue fee
- **`total_covers`**: `lunch_pax + dinner_pax + (event_pax IF NOT event_in_cm ELSE 0)` — all people in seats
- **`dinner_covers`**: `dinner_pax + (event_pax IF NOT event_in_cm AND event_timeframe='Noche' ELSE 0)` — seats in that shift
- **`lunch_covers`**: `lunch_pax + (event_pax IF NOT event_in_cm AND timeframe is non-Noche AND non-empty ELSE 0)`
- **`avg_ticket`**, **`lunch_avg_ticket`**, **`dinner_avg_ticket`**: event-excluded (see Avg Ticket Metric Definition)

### `/api/stats/weekly`

Fetches the same raw fields per day, accumulates `regular_lunch_sales`, `regular_lunch_covers`, `regular_dinner_sales`, `regular_dinner_covers` per week bucket, then computes `avg_ticket` from the period sums — never by averaging daily averages.

### `/run-pipeline?date=YYYY-MM-DD[&save=true]`

When `save=true`: writes `full_daily_stats`, `daily_stats`, and (if `ds.line_items` is non-empty) **also writes `daily_product_sales` and `daily_server_sales`**. The aggregation tables are fully replaced for that day (DELETE + INSERT) so re-running is idempotent.

### `/admin/peek-aggregations?date=YYYY-MM-DD`

Diagnostic read-only endpoint. Returns all rows from `daily_product_sales` and `daily_server_sales` for a given date. Use to verify aggregation after running pipeline.

### `/admin/sync-check?since=YYYY-MM-DD&threshold=N`

Cross-checks `full_daily_stats` vs `daily_product_sales` revenue per day. Surfaces days where the two tables disagree by `>= threshold` EUR (default: 1.0). Read-only.

- `since` (optional): lower bound of scan window (default: 90 days before today). Rejected if in the future.
- `threshold` (optional, default `1.0`): minimum `abs(diff)` to include in `mismatched_days`.
- **`overview_column_used`**: `COALESCE(NULLIF(z_total_sales, 0), total_sales)` — same expression the Overview KPI sums via `/api/stats/daily`.
- `mismatched_days` sorted by `abs(diff)` descending (biggest gap first).
- DB query wrapped in `try/except`; returns HTTP 500 with `error` field on failure.

Response fields: `since`, `until`, `threshold`, `overview_column_used`, `total_full_daily_stats`, `total_daily_product_sales`, `total_diff`, `mismatched_days[]` (`date`, `full_daily_stats_value`, `daily_product_sales_total`, `diff`).

### `/admin/inspect-day?date=YYYY-MM-DD`

Read-only. Returns all `daily_product_sales` rows for a date, grouped by `(product, family, timeframe)`, sorted by `total_net` ASC (most-negative first). Response includes `row_count`, `negative_count`, `negative_total_net`, and a `rows[]` array with `product`, `family`, `timeframe`, `total_qty`, `total_net`, `lunch_net`, `dinner_net`.

Use before calling `/admin/cleanup-negative-lines` to verify which rows will be removed.

### `/admin/cleanup-negative-lines?date=YYYY-MM-DD&confirm=yes`

**POST only.** Surgical DELETE of all `daily_product_sales` rows where `report_day = date AND net < 0`. Requires `confirm=yes` query param; returns 400 with an explanation if omitted. Wrapped in a DB transaction; rolls back on error. Logs every deleted row to stdout as an audit trail before deleting.

Response: `{ date, deleted_count, deleted_total_net }`.

**Reversible:** `/run-pipeline?date=YYYY-MM-DD&save=true` re-imports the original Agora line items (including any corrections present at pipeline-run time).

### `/admin/health-check?from=YYYY-MM-DD&to=YYYY-MM-DD[&since=YYYY-MM-DD]`

Default window: last 90 days. Optional `?since=YYYY-MM-DD` overrides the lower bound without touching the upper bound — useful for inspecting dates older than 90 days (e.g. `?since=2026-03-01`). Returns 400 if `since` is in the future or malformed. Response always includes `since_date` and `until_date` fields confirming the actual window checked.

Anomaly patterns:
- **`silent_save_failure`**: `total_sales > 0` but `(lunch_pax + dinner_pax) = 0`
- **`negative_shift_sales`**: `lunch_sales < 0` or `dinner_sales < 0`
- **`inconsistent_event_pax_without_menu`**: `event_pax > 0` but `event_menu_total = 0`
- **`inconsistent_event_menu_without_pax`**: `event_menu_total > 0` but `event_pax = 0`
- **`event_pax_exceeds_shift_in_cm`**: `event_pax > shift_pax` when `event_in_cm = TRUE`
- **`missing_row`**: non-Sunday operating day with no row in DB
- **`missing_product_aggregation`**: day has `total_sales > 0` but no rows in `daily_product_sales`
- **`missing_server_aggregation`**: day has `total_sales > 0` but no rows in `daily_server_sales`

### `/api/dashboard/*` — Period-based analytics endpoints (added 2026-06-04)

All five endpoints share the same conventions:
- Auth: `Authorization: Bearer DASHBOARD_API_KEY`
- Required params: `period_start=YYYY-MM-DD`, `period_end=YYYY-MM-DD` (both inclusive)
- Validation: 400 if either param missing or malformed; 400 if `period_start > period_end`; 400 if span exceeds 365 days
- All numeric values rounded to 2 decimal places

#### `GET /api/dashboard/products`

Queries `daily_product_sales`. Returns:
- `top_by_revenue` — top 15 products by net revenue, with `pct_of_total`
- `top_by_quantity` — top 15 products by quantity, with `pct_of_total`
- `family_mix` — all families with non-zero sales, sorted by revenue, with `pct_of_total`
- `slow_movers` — bottom 20 by quantity (zero-quantity items excluded)
- `lunch_top` — top 10 by revenue in lunch timeframes (`mediodía`, `comida`, `lunch`, `almuerzo`, `mediodia`)
- `dinner_top` — top 10 by revenue in dinner timeframes (`noche`, `cena`, `dinner`)
- `food_revenue` — sum of net revenue where `family = 'CARTA'` (case-insensitive)
- `drinks_revenue` — sum of net revenue where `family != 'CARTA'` (all other families including unbucketed)
- `distinct_products_in_period` — count of unique product names with any sales in `period_start..period_end`
- `active_menu_size` — count of unique product names with any sales in the trailing 90 days ending at `period_end` (i.e., `period_end − 89d` to `period_end`). `null` if zero products found (no data yet), so the dashboard can show "N/A" without dividing by zero.
- `venue_fees_total` — `SUM(venue_fee)` from `full_daily_stats` for the period (NULL treated as 0). `null` if the secondary query fails.
- `venue_fee_event_count` — count of distinct days where `venue_fee > 0` in the period. `null` on query failure.
- `other_adjustments_total` — sum of positive per-day diffs (`fds_value − dps_total >= 1.0 EUR`) across the period, minus `venue_fees_total`, floored at 0. Represents revenue booked at POS-ticket level that doesn't appear as product line items (excluding identifiable venue fees). `null` on query failure.
- `non_product_revenue_total` — `venue_fees_total + other_adjustments_total`. The "useful" portion of the Overview/F&B gap. `null` on query failure.
- `food_top_by_revenue` — top 20 CARTA-family products by net DESC (same shape as `top_by_revenue`; `pct_of_total` relative to overall period total)
- `drinks_top_by_revenue` — top 20 non-CARTA products by net DESC
- `food_top_by_quantity` — top 20 CARTA products by quantity DESC
- `drinks_top_by_quantity` — top 20 non-CARTA products by quantity DESC

`top_by_revenue` and `top_by_quantity` (original combined fields, now also top 20) are retained for backward compatibility.
`lunch_top` and `dinner_top` are also top 20.

**Dependency note:** `venue_fees_total` is only accurate when events are rung up correctly — i.e., both a `LineType=="Menú"` product line AND the venue fee difference appears in the Z-report closeout. If an event day has `venue_fee = 0` but real venue revenue, it will surface as `other_adjustments_total` instead.

**Secondary query isolation:** The four non-product-revenue fields are computed via a separate `get_conn()` call wrapped in `try/except`. On failure, all four fields return `null` and a warning is logged. The rest of the endpoint response (charts, family mix, etc.) is unaffected.

#### `GET /api/dashboard/servers`

Queries `daily_server_sales`. `EXTRAS` user excluded from all sections. Returns:
- `leaderboard` — sorted by `total_revenue` desc. Each entry: `user_name`, `total_revenue`, `lunch_revenue`, `lunch_tickets`, `dinner_revenue`, `dinner_tickets`, `total_tickets`, `avg_per_check`, `lunch_avg_per_check`, `dinner_avg_per_check`, `tips`, `tips_pct_of_revenue`
- `top_performer` — #1 from leaderboard with `period_share_pct` (% of all server revenue)
- `weekly_consistency` — week-bucketed revenue per server for sparklines (`week_start` aligned to Monday)
- `prior_period_comparison` — same-length period immediately prior; `delta_pct` is `null` if no prior revenue

#### `GET /api/dashboard/events`

Queries `full_daily_stats`. Days are included if `event_menu_total > 0 OR transferencia > 500 OR event_in_cm = FALSE`. Returns:
- `event_days` — list of days with `date`, `transferencia`, `event_pax`, `event_menu_total`, `event_in_cm`, `lunch_sales`, `dinner_sales`, `z_total_sales`
- `summary.total_event_revenue` — sum of `event_menu_total + transferencia` across event days
- `summary.avg_event_pax` — average of non-zero `event_pax` values; `null` if none

#### `GET /api/dashboard/transferencia`

Queries `full_daily_stats`. Returns all days in range with their `transferencia` value (including zero-value days). Summary: `total`, `nonzero_days`, `avg_per_nonzero_day`.

#### `GET /api/dashboard/walkins`

Queries `full_daily_stats`. Returns all days in range with per-shift walk-in counts and percentages. `lunch_walkin_pct = lunch_walkins / lunch_pax * 100` (0.0 if no pax). Summary: `total_walkins`, `total_pax`, `overall_walkin_pct`.

---

## AI Agent Event Awareness

All agent query paths use event-aware aggregation via `_regular_shift_metrics()`:

- **`_agent_row_to_dict`**: exposes `reg_lunch_sales`, `reg_lunch_covers`, `reg_dinner_sales`, `reg_dinner_covers` in every row dict; `avg_ticket`, `lunch_avg`, `dinner_avg` are event-excluded.
- **`_sum_period_rows`**: sums reg fields across rows, computes `avg_ticket` from period sums (not daily avg averages). `sales` = sum of `z_total_sales` (full revenue). `covers` = sum of event_in_cm-aware total covers.
- **`_exec_get_period_summary`**: calls `get_full_days_in_period()` → `_sum_period_rows()`.
- **`_fmt_snapshot`**: uses `_regular_shift_metrics()` for `lunch_avg`, `dinner_avg`, `avg_ticket`. Displays raw `lunch_sales`/`dinner_sales` (full shift revenue) alongside event-excluded avg tickets.
- **`send_evening_alerts`**: 19-field unpack from `get_full_day()`; revenue = `z_total_sales` if > 0, covers = event_in_cm-aware formula. Alert comparisons use these values.

### AI Agent Tools — F&B + Staff (added 2026-06-02)

Four new tools query the `daily_product_sales` / `daily_server_sales` tables:

| Tool | Description |
|---|---|
| `get_top_products(period_start, period_end, metric, limit)` | Top products by revenue or quantity. `metric`: `'revenue'` (default) or `'quantity'`. `limit`: max 50. |
| `get_category_breakdown(period_start, period_end)` | Revenue by product family with % share. |
| `get_server_leaderboard(period_start, period_end, metric)` | Server ranking by revenue or avg_ticket; includes lunch/dinner split. |
| `get_product_trend(product_name, period_start, period_end)` | Daily revenue+quantity for one product. Returns fuzzy name suggestions if exact match not found. |

All four tools require data in `daily_product_sales` / `daily_server_sales`. If a day hasn't been backfilled via `/run-pipeline?save=true`, these tools return empty results for that period.

---

## Data Freshness Model

**Dashboard = latest truth. Telegram posts = immutable snapshots.**

- `/run-pipeline?save=true` always overwrites the DB row with current live data from Agora + CoverManager. Safe to re-run any time.
- The scheduled daily Telegram post is sent once at 11:05 and is never edited. If CM data or Agora data is corrected after the post, the DB will reflect the correction but the chat message will not.
- Discrepancies between dashboard figures and old Telegram reports are **expected** when the underlying data was edited after the report was generated — this is not a bug.
- `event_in_cm` is the single exception to "latest truth": it is set on INSERT and can only be changed via `/admin/event-flag POST`. Pipeline re-runs never overwrite it.

---

## Analytics Commands (Read)

| Command | Args | Returns |
|---|---|---|
| `/today` | — | Today's full daily stats |
| `/yesterday` | — | Yesterday's full daily stats |
| `/dow` | weekday name/number | Historical avg for that weekday (last 6 weeks) |
| `/weekcompare` | — | This week Mon→today vs same days last week |
| `/monthcompare` | — | This month 1st→today vs same dates last month |
| `/weekendcompare` | — | Last Fri+Sat vs previous Fri+Sat |
| `/weekdaymix` | [N weeks, def 8] | Avg sales/covers/ticket/no-show rate by weekday |
| `/noshowrate` | [N weeks, def 8] | No-show rate by weekday, overall/lunch/dinner |
| `/daily` | — | Today's simple sales + covers |
| `/month` | — | Month-to-date totals |
| `/last` | `7` / `6M` / `1Y` | Last N days/months/years |
| `/range` | `YYYY-MM-DD YYYY-MM-DD` | Custom date range totals |
| `/bestday` | — | Best sales day in last 30 days |
| `/worstday` | — | Worst sales day in last 30 days |
| `/noteslast` | `30` / `6M` / `1Y` | Notes from last N period |
| `/findnote` | keyword | Full-text search across notes |
| `/soldout` | days | All `[SOLD OUT]` tagged notes in period |
| `/complaints` | days | All `[COMPLAINT]` tagged notes in period |
| `/tagstats` | days | Count of each tag type in period |
| `/staffnotes` | days | All `[STAFF]` tagged notes in period |

---

## Data Entry Commands (Write)

| Command | Effect |
|---|---|
| `/setdaily SALES COVERS` | Save simple daily KPIs for business day |
| `/edit YYYY-MM-DD SALES COVERS` | Edit historical daily data (admin only) |
| `/setfull` | Enter paste mode; next message parsed as full report block |
| `/setfullguided` | Interactive Q&A for all 13 full-report fields |
| `/confirmfull` | Confirm and save guided flow data |
| `/cancelfull` | Abort paste or guided flow |
| `/report` | Enter notes mode for today |
| `/cancelreport` | Abort notes mode |
| `/reportday YYYY-MM-DD` | View saved notes for a date |
| `/reportdaily` | View saved notes for today |

**Auto-detect:** In `ROLE_MANAGER_INPUT` and `ROLE_OPS_ADMIN` chats, messages matching the full daily report format are parsed and saved automatically without a `/setfull` command.

**Auto-notes:** In `ROLE_MANAGER_INPUT`, messages containing 2+ operational keywords (incident, sold out, complaint, staff, maintenance, etc.) are auto-saved as notes.

Full report supports both English and Spanish field labels. Date input accepts both `YYYY-MM-DD` and `DD/MM/YYYY`.

---

## Note Tags

Tags are detected via case-insensitive substring match anywhere in the note text.

| Tag | Aliases | Emoji | Meaning |
|---|---|---|---|
| `SOLD OUT` | `[sold out]`, `[soldout]`, `[agotado]`, `[sin existencias]` | 🍽️ | Menu/stock unavailability |
| `COMPLAINT` | `[complaint]`, `[complaints]`, `[queja]`, `[quejas]`, `[reclamacion]` | ⚠️ | Customer or operational complaints |
| `STAFF` | `[staff]`, `[personal]`, `[equipo]` | 👥 | Staff/HR issues |
| `MAINTENANCE` | `[maintenance]`, `[mantenimiento]`, `[technical]`, `[tecnico]` | 🔧 | Equipment/building maintenance |
| `INCIDENT` | `[incident]`, `[incidente]`, `[problema]` | 🚨 | Serious incidents |

Multiple tags per note are supported. Tag analytics: `/tagstats`, `/soldout`, `/complaints`, `/staffnotes`.

---

## Coding Conventions

### Date & time
- `business_day_today()` and `previous_business_day(ts)` apply `CUTOFF_HOUR` logic.
- `now_local()` returns tz-aware local time.
- `parse_any_date(s)` accepts `YYYY-MM-DD` and `DD/MM/YYYY`.

### Periods
- `Period(start, end)` dataclass used throughout analytics.
- `period_ending_today(arg)` parses `"7"`, `"6M"`, `"1Y"` strings.

### Number parsing
- `_num(s)` normalises currency strings (€ symbol, comma/dot decimals).
- `_int(s)` strips non-digits for integer fields.

### Formatting output
- `euro_comma(x)` formats floats as `"X,XX"` (Spanish locale).
- `fmt_day_ddmmyyyy(d)` formats dates as `DD/MM/YYYY`.

### Authorisation
- `is_admin(update)` checks `ACCESS_MODE` and `ALLOWED_USER_IDS`.
- `guard_admin(update)` is an async wrapper; replies "Not authorized." if denied.
- Chat-role helpers: `current_chat_role()`, `allow_sales_cmd()`, `allow_notes_cmd()`, `allow_full_cmd()`.

### State management
- Per-user state stored in `app.bot_data` under key `"{chat_id}:{user_id}"`.
- Constants: `REPORT_MODE_KEY`, `FULL_MODE_KEY`, `GUIDED_FULL_KEY`.
- `set_mode()`, `get_mode()`, `clear_mode()` are the only state accessors.

### Naming
- DB columns: `lowercase_with_underscores`
- Config constants: `UPPER_CASE`
- Role constants: `ROLE_` prefix
- Private/utility functions: leading underscore (`_num`, `_append_full_analytics_block`, etc.)

### Error handling
- Broad `except:` blocks on user-facing parsers; prompt user to retry on failure.
- `print()` to stdout for server-side logging.

---

## Deferred / Not Yet Implemented

- **Bot commands are English-only.** The AI agent responds in the user's language (English, Spanish, Russian), but slash commands and structured parsing are English.
- **Dynamic alert threshold tuning.** All thresholds are env vars; no `/setalert` command exists yet.
- **Agent tool extensibility.** `AGENT_TOOLS` list currently has 13 tools (9 original + 4 F&B/staff tools added 2026-06-02). Adding more tools requires a new tool dict in `AGENT_TOOLS`, a `_exec_*` function, and a new `elif` branch in `execute_agent_tool()`.
- **Legacy role config.** `OWNERS_CHAT_IDS` in `settings` table still supported alongside `chat_roles` table; both code paths are live.
- **No opening/shift-start alerts.** All scheduled alerts are end-of-day. Real-time shift alerts would require a second scheduled job or webhook triggers.
- **Float rounding on avg ticket.** Headline avg ticket may show 1¢ low (e.g., 45.915 → 45.91 instead of 45.92) due to Python float arithmetic. Accepted.
- **JS-level period avg ticket.** If the dashboard JS computes a period avg by averaging daily avg_ticket values, event days will slightly distort the result (because the denominator varies per day). The correct approach is to sum regular_sales and regular_covers across days then divide — which the backend already does via `_sum_period_rows`. If JS does its own averaging, small distortion may appear on weeks/months containing events.

---

## Manual Data Corrections

One-off surgical corrections to the DB that cannot be handled by the normal pipeline.

### 2026-03-10 — Remove Sojo Madrid reversal line items from daily_product_sales

**Background:** On 2026-03-07 a Sojo Madrid private event was billed for 54–55 menus. On 2026-03-10, 29 of those menus were reversed in Agora because only 25 should have been billed (the remaining guests were invitados). The reversal posted as ~29 negative line items against the 2026-03-10 date in Agora, totalling approximately −€1,898. This was imported into `daily_product_sales` by the pipeline and created a ~€1,484 gap in the "Other adjustments" bucket of the F&B tab's Non-Product Revenue card.

**Action:** DELETE rows WHERE `report_day = '2026-03-10' AND net < 0` from `daily_product_sales`. Mar 7's data is untouched (the original billing stays as historical record).

**Status:** Pending — inspect via `/admin/inspect-day?date=2026-03-10`, then execute via `POST /admin/cleanup-negative-lines?date=2026-03-10&confirm=yes`.

**Reversible:** `/run-pipeline?date=2026-03-10&save=true` re-imports the original Agora line items (including the negative corrections). Do not run the pipeline for this date after the cleanup unless explicitly needed.

---

## Changelog

### 2026-06-04 — Dashboard analytics endpoints + per-server tips

**Schema**: `daily_server_sales.tips NUMERIC DEFAULT 0` added via idempotent `ADD COLUMN IF NOT EXISTS` in `init_db()`. Existing rows remain at 0 until next pipeline run.

**`agora_integration.py`**:
- `_fetch_tips_by_user()` now returns `(total: float, per_user: dict[str, float])` instead of just `float`. Per-user dict maps `UserName → TipAmount` (summed across multiple records per user per day).
- `DailySales` dataclass gained `tips_by_user: dict` field.
- `get_daily_sales()` unpacks both values: `ds.tips, ds.tips_by_user = _fetch_tips_by_user(...)`.

**`bot.py`**:
- `upsert_server_sales(day_, line_items, tips_by_user=None)` — new optional `tips_by_user` param. Writes each server's tip total by matching `UserName` in the dict. Pipeline call updated to pass `ds.tips_by_user`.
- Five new `/api/dashboard/*` endpoints (all auth-gated, all validate `period_start`/`period_end`/365-day cap):
  - `GET /api/dashboard/products` — product mix from `daily_product_sales`
  - `GET /api/dashboard/servers` — server leaderboard with tips and prior-period comparison from `daily_server_sales`
  - `GET /api/dashboard/events` — event-flagged days from `full_daily_stats`
  - `GET /api/dashboard/transferencia` — daily transferencia values from `full_daily_stats`
  - `GET /api/dashboard/walkins` — walk-in vs reservation share from `full_daily_stats`
- `_validate_period()` helper: shared param validation for all five endpoints.

**No backfill required** — existing `daily_server_sales` rows remain at `tips=0` until re-pipelined. Products, events, transferencia, and walkins endpoints draw from tables already populated.

### 2026-06-06 — Split F&B top-10 charts into food vs drinks

Replaced the two full-width "Top 10 by Revenue / Quantity" charts with four side-by-side charts separated by food (CARTA) vs drinks (non-CARTA). Food charts use `--lunch` color, drinks use `--dinner`. Four new endpoint fields added to `/api/dashboard/products`; original `top_by_revenue`/`top_by_quantity` kept for compatibility.

### 2026-06-06 — Add inspect-day and cleanup-negative-lines admin endpoints

`GET /admin/inspect-day?date=YYYY-MM-DD` — read-only, returns all `daily_product_sales` rows for a date sorted by net ASC (most-negative first).
`POST /admin/cleanup-negative-lines?date=YYYY-MM-DD&confirm=yes` — transaction-wrapped DELETE of negative-net rows; logs audit trail to stdout; reversible via pipeline re-run.
Both are additive — no changes to existing endpoints or tables.

### 2026-06-06 — F&B tab: Non-Product Revenue KPI card

Extended `/api/dashboard/products` with four new fields (`venue_fees_total`, `venue_fee_event_count`, `other_adjustments_total`, `non_product_revenue_total`) computed via a secondary LEFT JOIN query between `full_daily_stats` and `daily_product_sales`. Positive diffs >= €1.0 are summed; venue fees are separated out; residual is `other_adjustments_total`. Added a 5th KPI card to the F&B tab. Secondary query is isolated in its own `try/except` so a failure returns null for those four fields only, leaving the rest of the endpoint intact.

### 2026-06-05 — Add /admin/sync-check diagnostic endpoint

Read-only endpoint that LEFT JOINs `full_daily_stats` against `daily_product_sales` per day and reports days where `COALESCE(NULLIF(z_total_sales,0), total_sales) − SUM(net)` exceeds the threshold. Used to diagnose the ~€4,500 residual gap between the Overview and F&B tabs over 90-day windows. No DB writes, no changes to existing endpoints.

### 2026-06-04 — F&B KPI cards: Food vs Drinks + Menu Coverage

**`/api/dashboard/products` new fields:**
- `food_revenue` — net revenue where `family = 'CARTA'`
- `drinks_revenue` — net revenue where `family != 'CARTA'` (all others including unbucketed)
- `distinct_products_in_period` — unique product count for the selected period
- `active_menu_size` — unique product count in trailing 90 days ending at `period_end`; `null` if no data

**Dashboard:**
- Replaced "Top Family" KPI card with "Food vs Drinks" two-column split (Food=`--lunch`, Drinks=`--dinner` colors).
- Replaced "Distinct Products" KPI card with "Menu Coverage" showing `distinct_products_in_period / active_menu_size` as a percentage; shows "N/A" when `active_menu_size` is null.

### 2026-06-04 — Fix: aggregation tables not written by scheduled daily post

**Bug:** `build_owners_post_for_day` called `upsert_full_day` and `upsert_daily` but not `upsert_product_sales` / `upsert_server_sales`. So the scheduled 11:05 post populated `full_daily_stats` (Overview tab correct) but left `daily_product_sales` and `daily_server_sales` empty (F&B and Staff tabs missing yesterday's data).

**Fix:** Added guarded aggregation calls immediately after `upsert_daily` in the `if not dry_run:` block of `build_owners_post_for_day`. Mirrors the pattern in `/run-pipeline?save=true`. Failure of the aggregation upserts is caught with `try/except` and logged as a warning — it does not prevent the Telegram post from being sent or `full_daily_stats` from being written.

### 2026-06-04 — Phase 3b+3c: Dashboard tab system + F&B tab

**Tab navigation** (`switchTab`, Phase 3b):
- Three-tab nav bar (Overview / F&B / Staff) in `dashboard.html`. `switchTab(name)` hides all `.tab-panel`, shows `#tab-{name}`, updates `.tab-btn.active`, controls period-selector visibility (hidden only for Staff), and persists to `localStorage('norah_active_tab')`.
- Period selector now visible on both Overview and F&B (not just Overview).
- `loadCurrentTab()` dispatcher replaces direct `loadAll()` calls on Apply button, `setPreset`, `setThisMonth`, and `saveToken`.
- `_fbLoaded` flag: lazy-loads F&B on first visit; bypassed on period changes.

**F&B tab content** (Phase 3c — fed entirely by `GET /api/dashboard/products`):
- KPI row: Total Products Sold, Total F&B Revenue, Top Family, Distinct Products.
- Top 10 by Revenue (horizontal bar, `COLORS.overall`).
- Top 10 by Quantity (horizontal bar, `COLORS.dinner`).
- Family Mix doughnut (`FAM_PALETTE`, legend right, 12-color palette).
- Slow Movers table (bottom 20 by quantity, scrollable 296px max-height).
- Lunch Top 10 / Dinner Top 10 side-by-side horizontal bars.
- `renderHBar(canvasId, items, color, valueKey, tooltipExtra)`: shared renderer; `indexAxis:'y'`, `maintainAspectRatio:false`, multi-line tooltips, product names truncated at 30 chars.

### 2026-06-02 — Phase 1 F&B + Staff Analytics

**Schema**: Two new tables added via `CREATE TABLE IF NOT EXISTS` in `init_db()` (second cursor block):
- `daily_product_sales (report_day, product, family, timeframe, quantity, net, gross)` — PK: `(report_day, product, timeframe)`
- `daily_server_sales (report_day, user_name, lunch_revenue, lunch_covers, dinner_revenue, dinner_covers, total_revenue)` — PK: `(report_day, user_name)`

**Pipeline**: `/run-pipeline?save=true` now calls `upsert_product_sales()` and `upsert_server_sales()` after writing `full_daily_stats`. Both helpers do DELETE + INSERT for the day, so re-runs are idempotent.

**Agent tools** (4 new tools, bringing total to 13):
- `get_top_products` — products ranked by revenue/quantity
- `get_category_breakdown` — revenue by product family with % share
- `get_server_leaderboard` — servers ranked by revenue or avg_ticket
- `get_product_trend` — daily rev+qty time series for one product; fuzzy name suggestions if not found

**Health-check**: Two new anomaly patterns (`missing_product_aggregation`, `missing_server_aggregation`) added to `/admin/health-check`.

**New endpoint**: `/admin/peek-aggregations?date=YYYY-MM-DD` — read-only diagnostic; returns both aggregation tables for a date.

**Backfill**: No migration script. Trigger `/run-pipeline?date=YYYY-MM-DD&save=true` per day to populate the new tables. Use `/admin/health-check` to identify which days are missing.

---

### 2026-05-31 – 2026-06-01 — Full Event-Aware Migration

**Schema** (`full_daily_stats`): Added 7 columns via idempotent `ALTER TABLE IF NOT EXISTS` in `init_db()`:
- `z_total_sales` — canonical revenue from Agora Z-report (includes venue fee)
- `transferencia` — bank-transfer payment from Z-report closeout
- `event_pax` — servings sold under `LineType=="Menú"` (MENU NORAH / MENUS EVENTOS)
- `event_menu_total` — event menu revenue
- `event_timeframe` — `'Noche'` | `'Mediodía'` | `'Tarde'`
- `venue_fee` — `z_total_sales − sum(line_items.Net)`, €1 tolerance
- `event_in_cm` — `BOOLEAN NOT NULL DEFAULT TRUE`; excluded from `ON CONFLICT DO UPDATE`

**Event detection:** `LineType == "Menú"` in `GetSalesAnalyticsReportRequest`. Confirmed zero false positives across 8 historical event days. Angie confirmed this fires for all pre-agreed menus (private events and group bookings).

**`event_in_cm` flag:** `TRUE` = event guests already in CM pax (standard). `FALSE` = external guests not booked through CM (only 2026-05-25). Per Angie's policy, all future events are registered in CM — `TRUE` is the permanent default.

**Cover math:** `total_covers = lunch_pax + dinner_pax + (event_pax IF NOT event_in_cm ELSE 0)` applied everywhere.

**Revenue:** `COALESCE(NULLIF(z_total_sales, 0), total_sales)` everywhere.

**Avg ticket redefined as event-excluded metric:** `_regular_shift_metrics()` helper subtracts event menu revenue and in-CM event pax before computing shift avgs. For non-event days the formula is identical to the original. `total_sales` and `total_covers` retain the full-picture values; `avg_ticket = total_sales / total_covers` identity is intentionally broken.

**Per-shift cover display:** `dinner_covers` / `lunch_covers` include external event guests (when `event_in_cm=FALSE`), distributed by `event_timeframe`.

**Period aggregation:** `_sum_period_rows` and `/api/stats/weekly` sum `regular_sales` and `regular_covers` across days then divide — never average daily averages.

**Endpoints added:**
- `/run-pipeline?save=true` — writes all 18 live fields (Agora + CM) to DB; `event_in_cm` excluded. Safe to re-run.
- `/admin/event-flag` GET/POST — read and patch event metadata without re-running pipeline.

**Daily post:** DB-path branch unified with live-path template (event-aware rendering in both paths). `build_owners_post_for_day` is now a single template.

**`get_full_day()`:** Extended from 12 to 19 fields; all callers updated.

**Backfill:** All 72 existing rows refreshed. Verified reference:
- 2026-05-25: `total_covers=76`, `total_sales=5784.60`, `avg_ticket=45.91`, `dinner_covers=60`, `dinner_avg_ticket=43.42`
- 2026-04-11: `total_covers=154`, `avg_ticket=44.69`, `dinner_avg_ticket=46.41` (event pax subtracted from covers and revenue)
- 2026-05-08 (non-event): `avg_ticket=48.18`, `dinner_avg_ticket=49.81` — unchanged from original formula

### 2026-06-01 — Defensive clamp in _regular_shift_metrics()

All four outputs of `_regular_shift_metrics()` are now clamped to non-negative via `max(..., 0)`:
```
regular_lunch_sales   = max(lunch_sales  - event_menu_lunch,   0)
regular_dinner_sales  = max(dinner_sales - event_menu_dinner,  0)
regular_lunch_covers  = max(lunch_pax  - event_pax_in_lunch_cm,  0)
regular_dinner_covers = max(dinner_pax - event_pax_in_dinner_cm, 0)
```

Two real failure modes that motivated this:
- **Silent save=true failure (Apr 10):** `dinner_pax=0` but `event_pax=13`, `event_in_cm=TRUE` → `regular_dinner_covers = 0 − 13 = −13` → `dinner_avg_ticket = −€295`. Clamp makes this `0/0 → 0.0` instead.
- **Agora retroactive void (Mar 10):** Agora changed `dinner_sales` from €1,875.80 to −€22.20 (a ~€1,898 refund processed post-hoc). Clamp makes `dinner_avg_ticket = 0.0` instead of `−€0.54`.

No-op on valid data. Raw fields in `full_daily_stats` are unaffected — only the in-memory avg_ticket calculation is clamped.

### 2026-06-01 — /admin/health-check endpoint

`GET /admin/health-check?from=YYYY-MM-DD&to=YYYY-MM-DD` (default: last 90 days). Auth: Bearer token.

Scans `full_daily_stats` for six anomaly patterns and returns `{checked_days, rows_in_db, anomalies: [{date, issue, detail}]}`:

| Issue code | Condition |
|---|---|
| `silent_save_failure` | `total_sales > 0` AND `(lunch_pax + dinner_pax) = 0` |
| `negative_shift_sales` | `lunch_sales < 0` OR `dinner_sales < 0` |
| `inconsistent_event_pax_without_menu` | `event_pax > 0` AND `event_menu_total = 0` |
| `inconsistent_event_menu_without_pax` | `event_pax = 0` AND `event_menu_total > 0` |
| `event_pax_exceeds_shift_in_cm` | `event_pax > 0`, `event_in_cm=TRUE`, and `event_pax > shift_pax` for the matching timeframe |
| `missing_row` | Non-Sunday operating day with no row in DB |

Run after any batch backfill or when the dashboard looks anomalous. Codifies the manual diagnostic queries used during the June 1 migration.

**Known findings from first run (default 90-day window):**
- `2026-03-03` through `2026-03-07` — five `missing_row` entries. Likely pre-bot-rollout period or a restaurant closure week. Not a bug; noted for awareness.
- `2026-03-10` — `negative_shift_sales`: `dinner_sales=−€22.20`. Agora's view of this day changed between the original report (€1,875.80 dinner sales) and the June 1 backfill (−€22.20). Difference ~€1,898 indicates a void or refund processed retroactively in Agora. Pending investigation with Angie.
- `2026-06-01` — `missing_row`: today, expected (daily post hasn't run yet).
