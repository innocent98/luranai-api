-- READ-ONLY. Run on PROD before deploying migration cl10conversationlimits.
-- Shows, per tenant, this month's conversation count against the OLD plan limit, the NEW plan limit,
-- and what the tenant will hold after the migration, so you can see who ends up over the new limit.
--
-- Safe by construction: one SELECT, no writes, no functions with side effects, no secrets
-- (tenant id, name, tier, status and numbers only). To be extra sure, run it inside:
--     BEGIN READ ONLY;  <this file>  ROLLBACK;
--
-- "This month" = the same definition the app uses: conversation_sessions rows created on or after
-- the first instant of the current calendar month, UTC (app/services/conversation_counting.py).
--
-- New plan limits (approved 2026-10-29): trial 100, lite 1000, professional 5000, growth 10000.
-- Enterprise is unlimited and is not changed.
--
-- How to read the columns
--   stored_limit          what tenant_capabilities holds today (what the app enforces today)
--   has_override          true when an admin edited max_conversations_per_month by hand
--                         (override_flags has that key). The migration NEVER touches these.
--   limit_after_migration the stored number if the tenant is overridden, or is not on the old plan
--                         number (the migration leaves those alone); otherwise the new plan limit
--   over_new_limit_by     > 0 means: after the migration this tenant has used more than its limit
--                         this month. Existing conversations stay; NEW ones are refused until the
--                         1st of next month. Decide per tenant before deploying (see the PR).

WITH new_limits(tier, old_limit, new_limit) AS (
    VALUES ('trial', 200, 100),
           ('lite', 2000, 1000),
           ('professional', 10000, 5000),
           ('growth', 25000, 10000)
),
usage AS (
    SELECT tenant_id, count(*) AS used_this_month
    FROM conversation_sessions
    WHERE created_at >= (date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
    GROUP BY tenant_id
)
SELECT
    t.id                                   AS tenant_id,
    t.name                                 AS tenant_name,
    t.subscription_tier                    AS tier,
    t.subscription_status                  AS status,
    COALESCE(u.used_this_month, 0)         AS used_this_month,
    c.max_conversations_per_month          AS stored_limit,
    n.old_limit                            AS old_plan_limit,
    n.new_limit                            AS new_plan_limit,
    COALESCE(
        jsonb_typeof(CAST(c.override_flags AS jsonb)) = 'object'
        AND jsonb_exists(CAST(c.override_flags AS jsonb), 'max_conversations_per_month'),
        FALSE
    )                                      AS has_override,
    CASE
        WHEN n.tier IS NULL THEN c.max_conversations_per_month      -- enterprise / unknown tier
        WHEN COALESCE(
                 jsonb_typeof(CAST(c.override_flags AS jsonb)) = 'object'
                 AND jsonb_exists(CAST(c.override_flags AS jsonb), 'max_conversations_per_month'),
                 FALSE) THEN c.max_conversations_per_month          -- hand-set: kept
        WHEN c.max_conversations_per_month = n.old_limit THEN n.new_limit  -- moves with the plan
        ELSE c.max_conversations_per_month                          -- neither old nor new: kept
    END                                    AS limit_after_migration,
    GREATEST(
        COALESCE(u.used_this_month, 0) - CASE
            WHEN n.tier IS NULL THEN c.max_conversations_per_month
            WHEN COALESCE(
                     jsonb_typeof(CAST(c.override_flags AS jsonb)) = 'object'
                     AND jsonb_exists(CAST(c.override_flags AS jsonb), 'max_conversations_per_month'),
                     FALSE) THEN c.max_conversations_per_month
            WHEN c.max_conversations_per_month = n.old_limit THEN n.new_limit
            ELSE c.max_conversations_per_month
        END,
        0
    )                                      AS over_new_limit_by
FROM tenants t
JOIN tenant_capabilities c ON c.tenant_id = t.id
LEFT JOIN new_limits n ON n.tier = t.subscription_tier
LEFT JOIN usage u ON u.tenant_id = t.id
ORDER BY over_new_limit_by DESC, used_this_month DESC, t.created_at;
