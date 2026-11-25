# AI_Console_Agent_Usecases.md (25 Nov 2026) vs the console

What the document asks for that the console does not do, or does differently. Checked against the code in
`production/` and the production database on 25 Nov 2026. "Built" = works today; "Different" = exists but behaves
otherwise; "Not built" = absent.

## What was created in production (25 Nov 2026)

| Use case | Department | Default agent | Key |
|---|---|---|---|
| CP hot-buyer check (active CPs) — UC-CP-1 | Demand | Openhouse CP (`14738fbb…`) | `cp_hot_buyer_active` |
| Lapsed-CP reactivation — UC-CP-2 | Demand | Openhouse CP | `cp_lapsed_reactivation` |
| CP event invite — UC-CP-3 | Demand | Openhouse CP | `cp_event_invite` |
| Owner outreach (JMD) — UC-JMD-1 | Supply | JMD Seller Qualification (`8a0211b0…`) | `jmd_owner_outreach` |

- The CP agent was moved from Supply to **Demand** (the document has no departments; the original spec puts
  "CP activation" under Demand). Its past calls now count for Demand.
- Not created, because they are not calling use cases: UC-CP-4 (the test-call button), UC-JMD-2 (a redial policy,
  see below), UC-JMD-3 (prompt behaviour), UC-JMD-4 (marked "planned").
- Priority and colour were left at their defaults (the document gives none). Each creation is in the audit trail.

### Tag lists: the document's tag names are not what the agents send

The tag lists were built from the tags the live agents actually return (production calls), not the document's names.

| Agent | Document says | Live agent sends |
|---|---|---|
| CP | `callback` | `callback_requested` |
| JMD | `interested_sell`, `interested_rent`, `not_selling`, `already_sold`, `wrong_number`, `callback` | `want_to_sell`, `want_to_sell_later`, `do_not_want_to_sell`, `picked_wrong_number`, `callback_requested`, `yes_unverified`, `not_eligible`, `buyer_lead` (none of the document's six occur) |

| | CP use cases | JMD owner outreach |
|---|---|---|
| good | buyer_captured, visit_booked | want_to_sell |
| soft | inventory_requested, callback_requested | want_to_sell_later, callback_requested, yes_unverified, buyer_lead |
| terminal | not_interested, unclear | do_not_want_to_sell, unclear, not_eligible |
| dnc | wrong_number | picked_wrong_number |
| retry | not_picked | not_picked |

**Please confirm:** `yes_unverified`, `buyer_lead` and `not_eligible` are not in the document; their placement is
our reading of the names. `unclear` is terminal per the document ("treat as soft-no, not retry"), but the default
redial policy still retries `unclear` and `yes_unverified` — remove them from a campaign's retry tags if the
document's rule should hold.

## Agents (§1)

| Document | Console |
|---|---|
| Persona, voice, transcriber (Deepgram, noise reduction off, VAD 0.85), 240 s cap, hard rules | Configured in Bolna, not the console — by design (original spec: prompt editing only) |
| Caller ID rotation "on spam" | **Different**: numbers rotate only for carrier-blocked leads (`rotate_numbers`) |
| Strip Mr/Smt/Dr before upload | **Not built**: names are sent as uploaded |

## Use-case actions (§2, §3, §4 lead routing)

| Document | Console |
|---|---|
| Good/soft tags create a lead for a human | **Built** as hand-offs (good/soft tags of the use case, console campaign calls) |
| Hot Buyer Lead assigned to **the CP's RM** | **Different**: hand-offs are assigned by hand; no CP → RM mapping |
| Append to Google Sheet "RM hot-buyer leads" | **Not built** |
| WhatsApp inventory **for the city** on `inventory_requested`; WhatsApp to RM | **Different**: a rule can send one approved Interakt template; picking city-specific inventory and messaging the RM are not built |
| `callback` → RM task at the stated time | **Different**: no RM tasks; a rule can only schedule an AI redial after N minutes |
| `visit_gap_reason` ∈ {rm_support, commission_payout} → escalation ticket to city head | **Not built**: rules act on `call_tag` only, not other extracted fields; no tickets |
| `price_too_high` / `inventory_mismatch` → pricing/supply feed; `no_clients_market_slow` → nurture list | **Not built** |
| RSVP list to event owner; reminder call T-1 (UC-CP-3) | **Not built** (RSVP is a field, not a tag) |
| Owner `interested_sell` → DI ingest **+ convert** to qualified with RM note, mapped to the CRM society | **Different**: the console pushes finished Supply campaign calls to DI's `/api/ai-calls/ingest`; conversion happens in DI (its own auto-push for `want_to_sell`/`want_to_sell_later`). The console never calls `/convert` or the societies lookup |
| `wrong_number` / `already_sold` → suppress number | **Built** via the use case's `dnc` tags (global do-not-call) |
| `not_interested` ×2 → suppress | **Not built**: no count-based rule |
| `interested_rent` → rental desk; `tenanted_investor` (UC-JMD-4, planned) | **Not built** |
| Extra fields (`buyer_details`, `visit_or_meeting`, `send_inventory`, `asked_if_ai`, `visit_gap_reason`, `rsvp`, `send_details`, `flat_details`, `expected_price`, `timeline`, `owner_remarks`) | **Built as storage only**: kept in the call's extracted data, shown on call detail and in the CSV export; nothing acts on or counts them |

## Redial cascade (UC-JMD-2)

| Document | Console |
|---|---|
| Max 3 attempts, never within 2 h | **Built**: default `max_attempts` 3, gap 180 min |
| Busy / failed / no-answer redialled **from number 2, then number 3** | **Different**: rotation only for carrier-blocked leads |
| Per-number health (connect %, spam flags) | **Not built**: the Numbers card lists numbers, labels and active flags only |

## Test call (UC-CP-4)

Built: "Place test call" with a from-number picker. **Different**: no live transcript — the transcript and recording
appear after the next sync (up to 5 minutes).

## Lists / datasets (§2, §3)

The document never says "dataset"; it describes the lists each use case calls and their columns. No dataset was
created: the document holds no list data.

| Document | Console |
|---|---|
| CP input row: `contact_number, cp_name, firm_name, area, city, visits_count, last_visit_date, societies_worked` | **Built**: `contact_number` is picked as the phone; the other seven are exactly the CP agent's variables (the agent also has `cp_status`) |
| UC-CP-1 vs UC-CP-2 split by `cp_status` = active / lapsed | **Built**: a `cp_status` column becomes the dataset's segment, so one CP dataset can feed both workflows (segment active / lapsed) |
| `cp_status` derived from visits (≥1 visit in 30 days = active; last visit > 30/45 days = lapsed) | **Not built**: the list must arrive with `cp_status` already set; the console does not compute it from `last_visit_date` |
| UC-CP-1 "weekly" | **Built** as a workflow schedule trigger |
| UC-CP-3 "ad-hoc campaign list" | **Built** (campaign upload wizard, or a dataset) |
| JMD input row: `contact_number, owner_name, society_name, crm_society_name, city, unit` | **Built**: `society_name` becomes the society facet; `owner_name` / `society_name` are the JMD agent's variables |
| `unit` never spoken | **Built**: only the phone and the agent's variables go to Bolna (`build_batch_csv`); `unit` stays in the stored row |
| Owner list per society, 1–4k rows per batch (MCG/RWA/registry) | **Built**: upload, society facet, segment selection, row limits |
| Lead mapped to the **CRM society name** (`crm_society_name`) in DI | **Different**: the DI push sends `society_name` (the spoken name); `crm_society_name` is kept in the row but not sent |
| Strip Mr/Smt/Dr, dedupe against already-leads | **Not built** (see Batch management) |

## Batch management (§4)

| Document | Console |
|---|---|
| Upload CSV/XLSX, map columns to agent variables, validate numbers | **Built** |
| Dedupe against do-not-call | **Built** (global `dnc_phone`) |
| Dedupe against **already-leads** (CRM) | **Not built** |
| Strip name prefixes | **Not built** |
| Concurrency per batch | **Different**: Bolna can't cap a batch; the console plans with each department's line share (as the original) |
| Wallet balance, estimated cost (≈₹1.6–1.9/dial, ≈₹4.1/connect) | **Not built** (the original showed the wallet on the Home page, which was removed) |
| Live status per call | **Different**: statuses arrive with the 5-minute sync (polling only, no webhook — by design) |
| Pause / resume / stop | **Built** |

## Call records (§4)

| Document | Console |
|---|---|
| Full execution JSON, recording, transcript, duration, hangup, cost, extraction | **Built** (`call.raw`; recordings archived to R2) |
| "Spoke" = completed ∧ duration ≥ 15 s ∧ ≥ 1 user turn | **Different**: "connected" = at least one human turn, with a short-call voicemail check |
| Strip banned phrases before any CRM | **Not built**: the DI push sends summary and transcript as they are |

## Reporting (§4)

| Document | Console |
|---|---|
| Per department, range 1/7/30 days, by use case, by agent; tag distribution; good-tag (hot-lead) count; outcome split | **Built** |
| Per batch | **Partly**: per-campaign counters on the campaign page |
| Per day | **Not built** |
| By city | **Not built** |
| Dialled → connected → **spoke** funnel | **Different**: dialled → connected only |
| Avg / median talk time, calls ≥ 60 s | **Not built** (total talk-minutes shown) |
| "Asked if AI" count, issue count, CP churn-reason distribution and "on us" share | **Not built** |
| Hangup split recipient / agent / voicemail / inactivity | **Different**: outcome split (connected / not picked / carrier blocked / voicemail / failed) |

## Agent ops (§4)

| Document | Console |
|---|---|
| Prompt versioning with diff and rollback | **Partly**: each prompt edit keeps the full previous prompt in the audit trail; no diff view or rollback button |
| Guardrail checklist before publishing a prompt | **Not built** |
| Test-call button | **Built** |

## Integrations (§4)

| Document | Console |
|---|---|
| Bolna agents, batches, executions, phone numbers | **Built** |
| Bolna wallet | **Not built** |
| DI `/api/ai-calls/ingest` | **Built** |
| DI `/convert`, societies lookup | **Not built** |
| Google Sheet "RM hot-buyer leads" | **Not built** |
| WhatsApp (Interakt) | **Built** (template sends from rules; needs `INTERAKT_*` set) |
