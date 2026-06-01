# Norah Ops Bot — Project Context

## What This Project Is

A Telegram bot that serves as the operational hub for **Norah**, a restaurant in Madrid, Spain. It handles daily sales data entry, operational notes, owner reporting, and an intelligent alert system. Staff enter data via Telegram messages; owners receive structured nightly reports and anomaly alerts. An AI agent (Claude) answers natural-language questions from owners against the live database.

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

---

## AI Agent Event Awareness

All agent query paths use event-aware aggregation via `_regular_shift_metrics()`:

- **`_agent_row_to_dict`**: exposes `reg_lunch_sales`, `reg_lunch_covers`, `reg_dinner_sales`, `reg_dinner_covers` in every row dict; `avg_ticket`, `lunch_avg`, `dinner_avg` are event-excluded.
- **`_sum_period_rows`**: sums reg fields across rows, computes `avg_ticket` from period sums (not daily avg averages). `sales` = sum of `z_total_sales` (full revenue). `covers` = sum of event_in_cm-aware total covers.
- **`_exec_get_period_summary`**: calls `get_full_days_in_period()` → `_sum_period_rows()`.
- **`_fmt_snapshot`**: uses `_regular_shift_metrics()` for `lunch_avg`, `dinner_avg`, `avg_ticket`. Displays raw `lunch_sales`/`dinner_sales` (full shift revenue) alongside event-excluded avg tickets.
- **`send_evening_alerts`**: 19-field unpack from `get_full_day()`; revenue = `z_total_sales` if > 0, covers = event_in_cm-aware formula. Alert comparisons use these values.

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
- **Agent tool extensibility.** `AGENT_TOOLS` list is structured for easy addition of new query tools beyond the current 9.
- **Legacy role config.** `OWNERS_CHAT_IDS` in `settings` table still supported alongside `chat_roles` table; both code paths are live.
- **No opening/shift-start alerts.** All scheduled alerts are end-of-day. Real-time shift alerts would require a second scheduled job or webhook triggers.
- **Float rounding on avg ticket.** Headline avg ticket may show 1¢ low (e.g., 45.915 → 45.91 instead of 45.92) due to Python float arithmetic. Accepted.
- **JS-level period avg ticket.** If the dashboard JS computes a period avg by averaging daily avg_ticket values, event days will slightly distort the result (because the denominator varies per day). The correct approach is to sum regular_sales and regular_covers across days then divide — which the backend already does via `_sum_period_rows`. If JS does its own averaging, small distortion may appear on weeks/months containing events.

---

## Changelog

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
