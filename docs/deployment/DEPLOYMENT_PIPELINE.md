## The Full Deployment Pipeline
                                                                                                                                                                     
  Push to main/develop
         │                                                                                                                                                           
         ▼        
    ┌─────────┐                                                                                                                                                      
    │   CI     │  ci.yml
    │          │                                                                                                                                                     
    │ 1. Lint  │  (ruff + black)
    │ 2. Test  │  (pytest tests/ — unit + integration via TestClient)                                                                                                
    │ 3. Regression │ (flow/automation specific tests)                                                                                                               
    │ 4. Build │  (Docker image builds)                                                                                                                              
    │ 5. Security │ (Trivy scan)                                                                                                                                     
    │          │                                                                                                                                                     
    └────┬─────┘                                                                                                                                                     
         │ CI passes ✓                                                                                                                                               
         ▼
    ┌─────────────────┐                                                                                                                                              
    │  staging-deploy  │  cd.yml — deploys to your VPS
    │                  │                                                                                                                                             
    │ • Build image    │                                                                                                                                             
    │ • SCP to server  │                                                                                                                                             
    │ • alembic upgrade│                                                                                                                                             
    │ • seed_data.py   │
    │ • seed_staging_e2e.py │  ← seeds test accounts for E2E                                                                                                         
    │ • docker up      │                                                                                                                                             
    │ • Health check   │                                                                                                                                             
    │                  │                                                                                                                                             
    └────┬─────────────┘
         │ Staging healthy ✓                                                                                                                                         
         ▼
    ┌─────────────────┐     ┌──────────────────────────┐                                                                                                             
    │ staging-api-e2e  │     │ dashboard-staging-e2e     │
    │                  │     │                          │                                                                                                            
    │ • Flush rate     │     │ • Flush rate limits      │
    │   limit keys     │     │ • Dispatch Playwright    │                                                                                                            
    │ • LURAN_E2E_     │     │   to dashboard repo      │
    │   BASE_URL set!  │     │ • Wait for completion    │                                                                                                            
    │ • pytest tests/  │     │                          │                                                                                                            
    │   e2e -m e2e     │     │ (cross-repo workflow)    │                                                                                                            
    │                  │     │                          │                                                                                                            
    │ REAL HTTP calls  │     │ REAL browser tests       │
    │ against staging  │     │ against staging          │                                                                                                            
    └────┬─────────────┘     └────┬─────────────────────┘
         │ E2E passes ✓           │ Dashboard E2E passes ✓                                                                                                           
         └───────┬────────────────┘                                                                                                                                  
                 ▼                                                                                                                                                   
    ┌─────────────────┐                                                                                                                                              
    │ production-deploy│  ONLY if BOTH e2e jobs pass
    │                  │  AND branch is main                                                                                                                         
    │ • Build image    │
    │ • SCP to server  │                                                                                                                                             
    │ • alembic upgrade│
    │ • docker up      │                                                                                                                                             
    │ • Health check   │
    └──────────────────┘
                                                                                                                                                                     
  Key details:
                                                                                                                                                                     
  The E2E tests DO run in CI — but only in the staging-api-e2e job (line 111), NOT in the test job. The staging-api-e2e job:                                         
  1. Waits for staging deployment to succeed (needs: staging-deploy)
  2. Flushes Redis rate limit keys on the staging server (lines 137-162) — this is why I said the rate limiter issue is local-only                                   
  3. Sets LURAN_E2E_BASE_URL (line 166) — this is the env var that the conftest checks, so the tests actually RUN here instead of skipping
  4. Runs pytest tests/e2e -m e2e making real HTTP calls against the staging server                                                                                  
                                                                                                                                                                     
  Production deploy (line 289) has this gate:                                                                                                                        
  needs: [staging-api-e2e, dashboard-staging-e2e]                                                                                                                    
  if: needs.staging-api-e2e.result == 'success' && needs.dashboard-staging-e2e.result == 'success'                                                                   
                                                                                                                                                                     
  Both the API E2E AND the dashboard Playwright tests must pass before production deploys.                                                                           
                                                                                                                                                                     
  ★ Insight ─────────────────────────────────────                                                                                                                    
  This is a textbook progressive delivery pipeline:                                                                                                                  
  1. Fast feedback — CI runs unit/integration tests in ~2 min (no live server needed)                                                                                
  2. Staging gate — deploy to staging, run real E2E against it                                                                                                       
  3. Cross-repo coordination — the backend CD triggers a Playwright workflow in the dashboard repo via workflow_dispatch, then polls for its completion              
  4. Production protection — main branch only + both E2E suites must pass                                                                                            
  5. Rate limiter awareness — the CD flushes Redis rate limit keys before E2E to prevent 429s (exactly the issue you hit locally)                                    
                                                                                                                                                                     
  The seed_staging_e2e.py script (line 86) creates the test accounts (staging-e2e-pro@luranai.com, etc.) that the E2E tests log in with. This is a clean pattern —   
  deterministic test fixtures seeded on every deploy.                                                                                                                
  ─────────────────────────────────────────────────                                                                                                                  
                                                                                                                                                                     
  The full flow is: CI passes → deploy to staging → E2E passes against staging → deploy to production. It's fully automated  
  and properly gated.

## How the CI test execution works

  The CI has two separate test jobs in ci.yml:

  Job 1: automation-flow-regression (line 46)

  Runs only 2 specific files:
  poetry run pytest \
    tests/api/v1/test_chat_flow_router.py \
    tests/api/v1/test_flow_action_call_regressions.py
  - Uses DATABASE_URL: "sqlite:///:memory:" — no real database
  - No live server needed — these use FastAPI's TestClient which calls the app directly in-process

  Job 2: test (line 96)

  Runs everything recursively:
  poetry run pytest tests/ -v --cov=app

  This collects all 26 test files. Here's what happens to each:

  Tests that ACTUALLY RUN (use TestClient — no real HTTP calls):

  ┌───────────────────────────────────────────────────┬───────────────────────────────────────────────────────┐
  │                       File                        │                     How it works                      │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_customer_auth.py                │ FastAPI TestClient → calls app in-process, no network │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_customer_engagement.py          │ Same — TestClient                                     │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_customer_conversations.py       │ Same — TestClient                                     │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_analytics.py                    │ Same — TestClient                                     │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_chat_flow_router.py             │ Same — TestClient                                     │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/api/v1/test_flow_action_call_regressions.py │ Same — TestClient                                     │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/services/test_*.py (3 files)                │ Direct service calls, no HTTP                         │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/unit/test_*.py (3 files)                    │ Pure unit tests, no HTTP                              │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/test_health.py                              │ TestClient                                            │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/test_polish_improvements.py                 │ TestClient                                            │
  ├───────────────────────────────────────────────────┼───────────────────────────────────────────────────────┤
  │ tests/test_openai_integration.py                  │ Mocked — patches OpenAI client                        │
  └───────────────────────────────────────────────────┴───────────────────────────────────────────────────────┘

  ★ Insight ─────────────────────────────────────
  TestClient is NOT making real HTTP calls. FastAPI's TestClient (from Starlette) calls your ASGI app directly in-process — no TCP socket, no network, no running
  server. It simulates HTTP requests by calling the app's request handler function. That's why these tests work with sqlite:///:memory: and no server running. The
  URL paths like /api/v1/customer/conversations are matched by FastAPI's router internally, not via network.
  ─────────────────────────────────────────────────

  Tests that are SKIPPED (need a live server but env var is missing):

  ┌───────────────────────────────────────┬──────────────────────────────────────────────────────────────────────────────┐
  │                 File                  │                                 Why skipped                                  │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_auth_live.py           │ e2e_config fixture calls pytest.skip() because LURAN_E2E_BASE_URL is not set │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_automation_live.py     │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_chat_live.py           │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_flows_live.py          │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_health_live.py         │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_platform_admin_live.py │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_subscription_live.py   │ Same                                                                         │
  ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e/test_tenant_live.py         │ Same                                                                         │
  └───────────────────────────────────────┴──────────────────────────────────────────────────────────────────────────────┘

  These tests use requests.Session() to make real HTTP calls to a live server at LURAN_E2E_BASE_URL. They're designed to run against a staging deployment — not in
  CI. Since the CI workflow never sets that env var, the e2e_config fixture (conftest.py line 87) does:

  if not base_url:
      pytest.skip("LURAN_E2E_BASE_URL is not set; skipping live E2E tests.")

  So pytest collects them, sees they need e2e_config, calls the fixture, hits the skip, and reports them as "skipped" — not "failed".

  Test that is COLLECTED but has 0 test functions:

  ┌─────────────────────────────────┬─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │              File               │                                                        Why harmless                                                         │
  ├─────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ tests/e2e_test_complete_flow.py │ It's a standalone script with no def test_*() functions. Pytest finds the file, scans for test functions, finds none, moves │
  │                                 │  on.                                                                                                                        │
  └─────────────────────────────────┴─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

  Summary: What makes actual API calls in CI?

  Nothing makes real HTTP calls in CI. Every test that runs uses either:
  - TestClient (in-process ASGI simulation)
  - Direct Python function calls (service/unit tests)
  - Mocked external services (OpenAI)

  The only tests that make real HTTP calls are the e2e/ tests, and those are always skipped in CI because LURAN_E2E_BASE_URL is never set.

  To run the e2e tests, you'd need to:

  1. Deploy to staging
  2. Set the env var: LURAN_E2E_BASE_URL=https://staging.luranai.com/api/v1
  3. Run: pytest tests/e2e/ -v 