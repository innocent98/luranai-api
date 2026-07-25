1. Public auditor — anti-abuse constraints (all live)

┌──────────────────────────┬────────────────────────────────────────────────────────────┬──────────────────────────────┐
│        Constraint        │                           Value                            │            Where             │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ Audits per IP            │ 3 / day (public_audit_limiter)                             │ public_auditor.py:24         │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ Report emails per IP     │ 5 / hour (capture_lead_limiter)                            │ public_auditor.py:32         │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ Pages crawled            │ 10 hard cap, regardless of user (AUDITOR_PUBLIC_MAX_PAGES) │ pipeline.py:38               │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ Crawl politeness         │ 1 req/sec, 10s/page timeout                                │ config                       │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ Unclaimed reports        │ purged after 30 days                                       │ AUDITOR_UNCLAIMED_PURGE_DAYS │
├──────────────────────────┼────────────────────────────────────────────────────────────┼──────────────────────────────┤
│ SSRF / DNS-rebind guards │ on seeds + every discovered link                           │ (prior hardening)            │
└──────────────────────────┴────────────────────────────────────────────────────────────┴──────────────────────────────┘

So a public visitor is boxed in on frequency (3/day/IP), depth (10 pages), and speed (1 rps) — the three abuse vectors. Cost per public audit is ~½–1¢, and 3/day/IP caps a single abuser at ~2¢/day.

2. Dashboard auditor — it IS already plan-constrained (two axes)

You do have per-plan limits — they were added in the add_auditor_capability_columns migration, which is probably why they didn't come to mind. Two separate caps enforced:

A. Monthly audit runs (_check_monthly_audit_limit, auditor.py:47 → 403 AUDIT_LIMIT_REACHED):

┌──────────────┬──────────────┬─────────────┐
│     Tier     │ Audits/month │ Pages/crawl │
├──────────────┼──────────────┼─────────────┤
│ trial        │ 2            │ 20          │
├──────────────┼──────────────┼─────────────┤
│ lite         │ 5            │ 100         │
├──────────────┼──────────────┼─────────────┤
│ professional │ 15           │ 500         │
├──────────────┼──────────────┼─────────────┤
│ growth       │ 30           │ 500         │
├──────────────┼──────────────┼─────────────┤
│ enterprise   │ unlimited    │ unlimited   │
└──────────────┴──────────────┴─────────────┘

┌──────────────┬─────────────────┬──────────────┬─────────────────────┬───────────┐
│     Plan     │   Pages/crawl   │  Audits/mo   │     KB sources      │   KB MB   │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Public       │ 10              │ 3/day per IP │ —                   │ —         │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Trial        │ 50 → 20         │ 2            │ 2 (page copy 3 → 2) │ 10        │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Lite         │ 200 → 100       │ 5            │ 15 → 20             │ 50        │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Professional │ 500             │ 15           │ 50                  │ 200       │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Growth       │ 500             │ 30           │ 50 → 100            │ 500       │
├──────────────┼─────────────────┼──────────────┼─────────────────────┼───────────┤
│ Enterprise   │ 500 → unlimited │ unlimited    │ unlimited           │ unlimited │
└──────────────┴─────────────────┴──────────────┴─────────────────────┴───────────┘

B. Knowledge sources (separate — knowledge_service.py:435): max_knowledge_sources combined files+URLs, and can_use_url_sources = False on trial (trial can't scrape URLs as knowledge). Activating audit findings as knowledge is capped by the same max_knowledge_sources (that's the cap-enforcement fix from PR #48).

- Knowledge cap limits what they can keep and use (retrieval/storage cost).
- Monthly audit cap limits what they can run (LLM + Playwright compute — ~2¢ and real CPU per 500-page crawl). Even a trial user who can only activate 2 sources could otherwise trigger unlimited 50-page crawls. So the run-cap is justified purely on compute, independent of knowledge.
