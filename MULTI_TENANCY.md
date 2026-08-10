# What in this engine is hardcoded to Insight Software

**Audited 05/08/2026 against the full backend (15 files, ~4,350 lines) and the
CRM's scraper-facing routes.**

The product is multi-tenant: every organisation using it sells something
different, to different people, in different markets. Parts of this engine are
built for that. Other parts silently encode **one company's idea of a good
lead** — ours — and apply it to every customer.

This file is the inventory. It is not a to-do list anybody has committed to;
it exists so that the next person to touch scoring, combos or actor input
knows what they are standing on, and so that a customer complaint like *"why
are all my leads warm?"* has a written answer.

Nothing here is currently being fixed. Where a fix would re-score or re-filter
existing data, that is called out, because those changes are visible to SDRs
who already know their leads.

---

## Summary

| # | What | Where | Severity |
|---|---|---|---|
| 1 | ICP scorer encodes a single ideal customer — ours | `scraper/icp_scorer.py` | **High** |
| 2 | Combos are a global catalogue; orgs can only toggle, never add | `scraper_combos_master` + CRM | **High** |
| 3 | `posted_on_linkedin` forced true on the legacy actor path | `scraper/apify_scraper.py:1098` | **Medium** |
| 4 | Bridge partnership titles are a fixed list | `scraper/apify_scraper.py:1175` | **Medium** |
| 5 | Supported output languages are a fixed list with real gaps | `api/message_generator.py:70` | **Medium** |
| 6 | Plan names are business logic living inside the engine | `api/message_generator.py:159` | **Low** |

And, importantly, [what is **not** a problem](#what-is-already-multi-tenant) —
so nobody spends a day "fixing" something that already works.

---

## 1. The ICP scorer encodes one ideal customer, and it is ours

**Files:** `scraper/icp_scorer.py`, inherited by `scraper/bridge_icp_scorer.py`

Two lists decide the score:

```python
PRIORITY_TITLES   = ["cto", "cio", "ceo", "founder", ...]        # technical/exec buyers
AI_SIGNAL_KEYWORDS = ["chatgpt", "openai", "claude", "ai", ...]  # AI interest
```

That is a precise description of **AI Token Sales' own buyer**. A customer
selling logistics software wants a Head of Supply Chain who mentions
"warehouse" or "3PL"; a customer selling clinic management software wants a
Practice Manager. Neither gets any signal from this scorer, and neither can
change it.

The sharpest way to see the problem: **the product already knows each customer
targets different people.** `org_combos` exists precisely so an org can choose
which titles to search for. So the scraper searches the customer's chosen
titles and then scores the results against a completely different, hardcoded
profile. The two halves of the pipeline do not talk to each other.

### It is also mechanically broken

Separately from the multi-tenancy problem, the scorer does not work. Full
detail is in the docstring at the top of `icp_scorer.py`; in brief:

- **Three of five components are constants** — 40 of 100 points, awarded to
  everyone.
- **`"ai"` is matched as a substring** — it fires on av**ai**lable,
  m**ai**ntain, em**ai**l, det**ai**l, ret**ail**, T**ai**pei. Effectively a
  fourth constant worth 20 more.
- **Titles are matched as substrings too**, failing in both directions:
  `"Director of Marketing"` scores as a CTO (dire**cto**r),
  `"Recruiting Coordinator"` as a COO (**coo**rdinator), while
  `"Chief Technology Officer"`, `"VP of Engineering"` and `"Head of Product"`
  score **zero**.

Net: 85 of 100 points measure nothing, and the remaining 30 are wrong both
ways. Nearly every lead with a bio lands on 60 (WARM), rising to 90 (HOT) only
on a substring accident.

### If this gets fixed

Do it in this order, because the reverse wastes work:

1. **Decide where per-org target titles come from.** The obvious answer is the
   org's own combos, which already exist and are already chosen per customer.
2. **Then** fix the matching (word boundaries, spelled-out variants) and drop
   or replace the components that measure nothing.

Tuning the current lists first means carefully calibrating something that is
about to be replaced.

⚠️ Fixing this **re-scores every existing lead**. Temperatures SDRs already
know will change. It needs to be a scheduled change with a heads-up, not a
quiet deploy.

---

## 2. Combos are a global catalogue; an org can only toggle, not add

**Files:** `api/config_generator.py:177` (`get_combo_definitions`),
`scraper_combos_master` table, CRM `src/app/api/scraper-combos/route.ts`,
CRM `src/app/api/global-admin/create-org/route.ts:148`

`get_combo_definitions` reads the org's enabled combo codes from `org_combos`,
then loads the definitions from `scraper_combos_master`. So *which* combos an
org uses is per-org — but the combos themselves are a **single shared
catalogue that only we can edit**. There is no endpoint anywhere that creates
a combo; the CRM only ever reads `scraper_combos_master`.

Practical consequence: a customer whose buyers are not already in our
catalogue **cannot be served without a database change on our side**. Onboarding
a logistics customer means someone at Insight hand-inserting a "Supply Chain"
combo into a global table that every other customer also sees.

`create-org` seeds every active master combo onto each new org, so every
customer starts with our full catalogue enabled — including combos aimed at
buyers they will never sell to. That is also where each run's cost goes: a
combo the customer doesn't need still gets searched and still gets billed.

Fixing this means either per-org combos (`organization_id` on the combo
definition itself) or an admin UI for the master catalogue. The first is
correct; the second is cheaper and might be enough while there are few
customers.

---

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
- **It silently props up the ICP score.** `_score_linkedin_activity` returns a
  constant 15 points, justified in its comment by exactly this input filter. So
  the hardcoded filter is what makes the constant defensible; remove one and
  the other needs revisiting.

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

## Suggested order, if this is ever picked up

1. **#2 (combos)** first. It is the foundation: once an org can define its own
   target titles, #1 has somewhere to read them from.
2. **#1 (scorer)** second, both halves at once — per-org sources *and* the
   matching fix — so leads are re-scored once rather than twice.
3. **#5 (languages)** any time; it is additive and breaks nothing.
4. **#3 (`posted_on_linkedin`)** alongside #1, since the two are entangled
   through `_score_linkedin_activity`.
5. **#4 (Bridge titles)** and **#6 (plan logic)** last — real, but neither is
   currently costing a customer anything.
