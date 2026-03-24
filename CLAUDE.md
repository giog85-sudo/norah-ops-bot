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
