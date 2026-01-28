-- Create test data for downgrade testing
-- Run this inside the database container

-- First, check if test tenant exists
DO $$
DECLARE
    v_tenant_id UUID;
    v_user_id UUID;
    v_plan_id UUID;
BEGIN
    -- Check/Create Professional plan
    SELECT id INTO v_plan_id FROM subscription_plans WHERE tier = 'professional' LIMIT 1;

    IF v_plan_id IS NULL THEN
        RAISE NOTICE 'Subscription plans table may not be seeded yet';
    END IF;

    -- Create test tenant if not exists
    INSERT INTO tenants (
        id, name, display_name, description, status, api_key_hash,
        subscription_tier, subscription_status, billing_cycle,
        subscription_starts_at, created_at, updated_at
    )
    VALUES (
        gen_random_uuid(), 'test_downgrade_company', 'Test Downgrade Company',
        'Test tenant for downgrade functionality', 'active',
        '$2b$12$test_hash_' || repeat('x', 50),
        'professional', 'active', 'monthly',
        NOW() - INTERVAL '15 days', NOW(), NOW()
    )
    ON CONFLICT (name) DO UPDATE SET
        subscription_tier = 'professional',
        subscription_status = 'active'
    RETURNING id INTO v_tenant_id;

    RAISE NOTICE 'Tenant ID: %', v_tenant_id;

    -- Create capabilities for the tenant
    INSERT INTO tenant_capabilities (
        id, tenant_id,
        can_access_analytics, max_knowledge_sources, can_use_url_sources,
        max_knowledge_mb, max_conversations_per_month, max_team_members,
        max_response_tokens, can_upload_knowledge,
        created_at, updated_at
    )
    VALUES (
        gen_random_uuid(), v_tenant_id,
        true, 30, true,
        500, 10000, 20,
        2000, true,
        NOW(), NOW()
    )
    ON CONFLICT (tenant_id) DO UPDATE SET
        max_knowledge_sources = 30,
        max_knowledge_mb = 500,
        max_team_members = 20;

    -- Create admin user (password: TestPass123!)
    -- Password hash for "TestPass123!"
    INSERT INTO users (
        id, email, hashed_password, full_name, role, tenant_id,
        is_active, created_at, updated_at
    )
    VALUES (
        gen_random_uuid(),
        'testadmin@downgrade.test',
        '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5koYV.LgA',  -- TestPass123!
        'Test Admin User',
        'tenant_admin',
        v_tenant_id,
        true,
        NOW(),
        NOW()
    )
    ON CONFLICT (email) DO UPDATE SET
        tenant_id = v_tenant_id,
        role = 'tenant_admin',
        is_active = true;

    RAISE NOTICE 'Test user created: testadmin@downgrade.test (password: TestPass123!)';
    RAISE NOTICE 'Test setup complete!';

EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Error: %', SQLERRM;
        RAISE;
END $$;
