# What in this engine is hardcoded to Insight Software

**Audited 05/08/2026 against the full backend (15 files, ~4,350 lines) and the
CRM's scraper-facing routes. Items 1 and 2 were FIXED on 04/09/2026 — their
sections below now describe what was done and what it cost.**

The product is multi-tenant: every organisation using it sells something
different, to different people, in different markets. Parts of this engine are
built for that. Other parts silently encode **one company's idea of a good
lead** — ours — and apply it to every customer.

This file is the inventory. It is not a to-do list anybody has committed to;
it exists so that the next person to touch scoring, combos or actor input
knows what they are standing on, and so that a customer complaint like *"why
are all my leads warm?"* has a written answer.

Where a fix would re-score or re-filter existing data, that is called out,
because those changes are visible to SDRs who already know their leads.

---

## Summary

| # | What | Where | Severity |
|---|---|---|---|
| 1 | ~~ICP scorer encodes a single ideal customer — ours~~ | `scraper/icp_scorer.py` | ✅ **Fixed 04/09/2026** |
| 2 | ~~Combos are a global catalogue; orgs can only toggle, never add~~ | `scraper_combos_master` + CRM | ✅ **Fixed 04/09/2026** |
| 3 | `posted_on_linkedin` forced true on the legacy actor path | `scraper/apify_scraper.py` | **Medium** |
| 4 | Bridge partnership titles are a fixed list | `scraper/apify_scraper.py` | **Medium** |
| 5 | Supported output languages are a fixed list with real gaps | `api/message_generator.py` | **Medium** |
| 6 | Plan names are business logic living inside the engine | `api/message_generator.py` | **Low** |

And, importantly, [what is **not** a problem](#what-is-already-multi-tenant) —
so nobody spends a day "fixing" something that already works.

---

## 1. ✅ The ICP scorer encoded one ideal customer, and it was ours

**Files:** `scraper/icp_scorer.py`, `scraper/bridge_icp_scorer.py`,
`scraper/text_match.py` (new)

Two hardcoded lists decided the score — `PRIORITY_TITLES` (technical/exec
buyers) and `AI_SIGNAL_KEYWORDS` — which is a precise description of AI Token
Sales' own buyer and of nobody else's. The sharpest way to see it: **the product
already knew each customer targets different people.** `org_combos` existed
precisely so an org could choose which titles to SEARCH for. The scraper
searched the customer's titles and then scored the results against a completely
different, hardcoded profile. The two halves did not talk.

It was also mechanically broken — three of five components were constants, and
both lists were matched with `in`, so `"ai"` fired on av**ai**lable and `"cto"`
on dire**cto**r while "Chief Technology Officer" scored zero.

### What was done

The scorer now takes an `IcpProfile` built per run from **the org's own data**:

| Component | Max | Source |
|---|---|---|
| Target title | 50 | the run's own combos (`title_keywords`), expanded to variants |
| Seniority | 20 | the title itself, via multilingual seniority vocabulary |
| Buying signal | 30 | terms from the org's `company_context`, matched in the lead's `about` |

The three constants are gone. Matching moved to `scraper/text_match.py`, which
is phrase-based and script-aware — substring for CJK (which has no word
boundaries), contiguous-token for everything else — so 資訊主管 matches inside
資深資訊主管 and `Director` no longer reads as a CTO.

What is still hardcoded is the **seniority** vocabulary, and that is deliberate:
"how senior is this person" is a fact about the world, the same question in
Taipei and Buenos Aires, not an opinion about who to sell to. Same reasoning as
the actor's own enums (see [what is not a finding](#not-findings-despite-looking-like-them)).

A component the ORG has not configured is excluded from the denominator instead
of being scored as zero, so an org that never filled in `company_context` still
gets a full 0-100 scale rather than an unreachable ceiling nobody told them
about.

### What it cost

⚠️ **Leads scored before the change keep their old numbers, and those numbers
mean something else.** They are deliberately NOT backfilled: `scraper_leads` has
no `about` column, so the buying-signal component cannot be recomputed for a
past lead — a backfill would invent a third scale rather than restore the
second. SDRs will see new leads score differently from ones they already know.
Tell them before a run, not after.

---

## 2. ✅ Combos were a global catalogue; an org could only toggle, not add

**Files:** `api/config_generator.py` (`get_combo_definitions`),
`scraper_combos_master`, CRM `src/app/api/scraper-combos/route.ts`,
CRM `src/components/scraper/CustomComboModal.tsx`,
CRM `supabase/migrations/20260904_org_owned_combos.sql`

*Which* combos an org used was per-org, but the combos themselves were a single
shared catalogue only we could edit — no endpoint anywhere created one. So a
customer whose buyers were not already in our catalogue could not be served
without a database change on our side, and `create-org` seeded every new org
with our full catalogue, including combos aimed at buyers they will never sell
to (which still get searched, and still get billed).

### What was done

`scraper_combos_master` grew an `organization_id`: NULL is the shared catalogue
(unchanged, still ours), a real id is a combo that customer wrote themselves in
Settings → Scraper. RLS scopes reads and writes; codes are minted server-side as
`custom_<12 hex>` so they can never collide with a global code. "Duplicate"
copies a catalogue combo into the org so it can be edited without touching the
shared row — which is the common case, since ours is usually close rather than
wrong.

**`get_combo_definitions` re-filters on ownership, and that filter is a security
boundary, not tidiness.** `org_combos.combo_code` is an FK to
`scraper_combos_master(code)` and nothing more, so an org admin can toggle on
any code that exists — including another customer's private one. Their own
Settings list would never show it, but the run would search LinkedIn with that
customer's title keywords. This backend holds the service role key, so RLS does
not constrain it: the `WHERE` clause is the whole defence. The CRM checks at its
end too, so the failure is a 404 rather than a run that silently finds nothing.

Same reason `create-org`'s seeding now filters `.is('organization_id', null)`:
it uses the admin client, which bypasses the RLS that would otherwise have
scoped it.

## 3. `posted_on_linkedin` is forced true on the legacy actor path

**File:** `scraper/apify_scraper.py:1098`

```python
init_input = {
    ...
    "posted_on_linkedin": "true",   # hardcoded
    ...
}
```

This says: *a lead is only worth having if they post on LinkedIn.* That is a
defensible view when selling to tech buyers, who are active there. It is a bad
assumption for plenty of real markets — plant managers, clinic owners and
logistics directors are often on LinkedIn without ever posting, and this
filters them out before anyone sees them.

Two things make it worse than a lone bad default:

- **It is inconsistent.** The HarvestAPI path at line 463 respects the combo's
  own `posted_on_linkedin` flag. The same product therefore applies two
  different rules depending on which actor is configured.
- **It used to silently prop up the ICP score.** `_score_linkedin_activity`
  returned a constant 15 points, justified in its comment by exactly this input
  filter. That entanglement is gone — the constant components were deleted in
  fix 1 — so `posted_on_linkedin` is now free to be fixed on its own merits,
  which is simpler than it was.

---

## 4. Bridge partnership titles are a fixed list

**File:** `scraper/apify_scraper.py:1175` (`BRIDGE_TITLE_KEYWORDS`)

17 hardcoded titles — "Head of Partnerships", "Director de Alianzas",
"夥伴關係經理" and so on. Credit where due: it is thoughtfully trilingual, and
covers the three markets the product actually sells into.

But it is the same shape of problem as the ICP scorer, one level down. An org
whose partner ecosystem lives under "Ecosystem Lead", "Reseller Manager" or
"Alliance Architect" cannot say so. Bridge has no equivalent of `org_combos` —
there is no per-org title configuration for partnerships at all.

Lower severity than #1 only because Bridge is newer and has fewer customers,
not because the problem is smaller.

Half-done as of 04/09/2026: the list is still fixed, but `bridge_icp_scorer` no
longer imports it — `import_bridge_candidates` passes it in. The day partnership
titles become per-org, that one call site is what changes.

---

## 5. Supported output languages are a fixed list with real gaps

**File:** `api/message_generator.py:70` (`_LANGUAGE_NAMES`)

15 languages plus Chinese variants, which are resolved separately: ar, de, en,
es, fr, id, it, ja, ko, nl, pl, pt, pt-br, th, vi.

The `markets` table covers ~49 countries across Asia, Latin America, Europe
and the USA. Europe is only partly served: **Russian, Turkish, Greek, Swedish
and Romanian are missing** (Hindi too, on the Asia side), and a market whose
language isn't listed falls back to `DEFAULT_MARKET_LANGUAGE = "en"`.

So we can sell a European market whose messages will quietly be written in
English. That is not a wrong-by-construction bug — English outreach in Poland
is survivable — but it is a silent downgrade of the headline promise
("five languages, natively generated"), and nothing surfaces it to the admin
who activated that market.

Cheap to fix: extend the list. Worth doing before selling into any market not
on it.

---

## 6. Plan names are business logic living inside the engine

**File:** `api/message_generator.py:159`

```python
if plan == "basic" or sender_profile is None:
    return "Write as a generic representative of the company..."
```

The scraper backend knows what a "basic" plan is and what it may not have.
That is our commercial model compiled into the message generator.

It works, and it is a small amount of code. But it means a pricing change —
renaming a tier, or deciding Basic gets sender profiles after all — requires a
backend deploy, and the rule lives far away from the `PLAN_DEFAULTS` and
`ADDON_LIST` in the CRM that define everything else about plans. Worth folding
into a capability the CRM passes in ("use a sender profile: yes/no") rather
than a plan name the engine interprets.

---

## What is already multi-tenant

Listed deliberately, so nobody rebuilds what works:

- **Message generation** (`api/message_generator.py`) reads `sender_profile`
  and `company_context` from the org. The prompt contains nothing about what
  we sell. This is the model the rest of the engine should follow.
- **Markets** live in the `markets` table with per-org activation via
  `organization_markets`. `apify_scraper.py:178` notes they used to be a
  hardcoded dict and no longer are — this exact migration is the precedent for
  fixing #1 and #2.
- **Anthropic credentials and model** are per-org.
- **Apify token** is per-org, so each customer's scraping is billed to their
  own account.
- **Dedup** (`api/dedup.py`, `api/bridge_job_runner.py`) is org-scoped, and
  Bridge deliberately never consults `scraper_leads` or `prospects` — a person
  can be both a sales lead and a partnership contact without the two pipelines
  filtering each other.

### Not findings, despite looking like them

`ALLOWED_SENIORITY_LEVELS`, `SENIORITY_LEVEL_ALIASES`, `HARVEST_HEADCOUNT`,
`COMPANY_HEADCOUNT_CODE_MAP` and `REGION_LABELS` are all hardcoded, but they
encode **the actor's and the database's own schemas**, not an opinion about who
a good lead is. LinkedIn defines those seniority bands; the `markets` table's
CHECK constraint defines those four regions. They are correct as constants and
should not be made configurable.

---

## What is left

#2 then #1 was the right order and is what happened — an org can define its own
target titles, and the scorer reads them. What remains:

1. **#5 (languages)** any time; additive, breaks nothing, and worth doing before
   selling into any market not on the list.
2. **#3 (`posted_on_linkedin`)** — now standalone, since the ICP constant it
   propped up is gone.
3. **#4 (Bridge titles)** — the scorer already takes them as a parameter, so
   this is now "give Bridge an `org_combos` equivalent", not a refactor.
4. **#6 (plan logic)** last: real, but not costing a customer anything.
