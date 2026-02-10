How Nested Flows Work
                                                                                                                                                                                   
  Your flow system uses nodes and edges to create decision trees. Each option can lead to a different path.                                                                      
                                                                                                                                                                                   
  Your Example: Connection Issue Flow

  Here's how you'd build that exact scenario:

  {
    "name": "Connection Issue Support",
    "trigger_type": "KEYWORD",
    "trigger_config": {
      "keywords": ["connection", "can't connect", "not connecting"]
    },
    "nodes": {
      "start_node_id": "greeting",
      "nodes": [
        {
          "id": "greeting",
          "type": "message",
          "config": {
            "message": "I can help with connection issues. Let me narrow this down."
          }
        },
        {
          "id": "main_issue_type",
          "type": "question",
          "config": {
            "question": "What type of connection issue are you experiencing?",
            "options": [
              {"label": "Network problems", "value": "network", "next_node_id": "network_troubleshoot"},
              {"label": "Can't login", "value": "login", "next_node_id": "login_issue"},
              {"label": "Error messages", "value": "error", "next_node_id": "error_type"}
            ],
            "variable": "issue_type"
          }
        },

        // ===== NETWORK PATH =====
        {
          "id": "network_troubleshoot",
          "type": "question",
          "config": {
            "question": "What network issue are you seeing?",
            "options": [
              {"label": "No internet connection", "value": "no_internet", "next_node_id": "check_modem"},
              {"label": "Slow connection", "value": "slow", "next_node_id": "speed_test"},
              {"label": "Keeps disconnecting", "value": "unstable", "next_node_id": "stability_check"}
            ],
            "variable": "network_type"
          }
        },
        {
          "id": "check_modem",
          "type": "message",
          "config": {
            "message": "Let's check your modem. Please verify:\n1. Modem is plugged in\n2. Power light is green\n3. Internet light is solid (not blinking)\n\nIs everything lit up
  correctly?"
          }
        },

        // ===== LOGIN PATH =====
        {
          "id": "login_issue",
          "type": "question",
          "config": {
            "question": "What happens when you try to login?",
            "options": [
              {"label": "Wrong password", "value": "wrong_password", "next_node_id": "password_reset"},
              {"label": "Account locked", "value": "locked", "next_node_id": "unlock_account"},
              {"label": "Page won't load", "value": "page_error", "next_node_id": "browser_check"},
              {"label": "Other login problem", "value": "other", "next_node_id": "escalate_login"}
            ],
            "variable": "login_problem"
          }
        },
        {
          "id": "password_reset",
          "type": "question",
          "config": {
            "question": "I can help you reset your password. Would you like me to send a reset link to your email?",
            "options": [
              {"label": "Yes, send reset link", "value": "yes", "next_node_id": "send_reset"},
              {"label": "No, I'll try again", "value": "no", "next_node_id": "end"}
            ],
            "variable": "reset_consent"
          }
        },
        {
          "id": "unlock_account",
          "type": "message",
          "config": {
            "message": "I see your account is locked. This usually happens after 5 failed login attempts. I can unlock it for you right now."
          }
        },
        {
          "id": "unlock_confirm",
          "type": "question",
          "config": {
            "question": "Would you like me to unlock your account?",
            "options": [
              {"label": "Yes, unlock my account", "value": "yes", "next_node_id": "perform_unlock"},
              {"label": "Talk to support instead", "value": "support", "next_node_id": "escalate_login"}
            ],
            "variable": "unlock_consent"
          }
        },
        {
          "id": "perform_unlock",
          "type": "webhook",
          "config": {
            "url": "https://your-api.com/unlock-account",
            "method": "POST",
            "body": {
              "user_email": "{{user_email}}",
              "reason": "automated_unlock"
            }
          }
        },

        // ===== ERROR PATH =====
        {
          "id": "error_type",
          "type": "question",
          "config": {
            "question": "What error message are you seeing?",
            "options": [
              {"label": "Error 404", "value": "404", "next_node_id": "error_404_help"},
              {"label": "Error 500", "value": "500", "next_node_id": "error_500_help"},
              {"label": "Connection timeout", "value": "timeout", "next_node_id": "timeout_help"},
              {"label": "Other error", "value": "other", "next_node_id": "get_error_details"}
            ],
            "variable": "error_code"
          }
        },

        // ... more nodes for each path ...

        {
          "id": "escalate_login",
          "type": "escalate",
          "config": {
            "reason": "Complex login issue requires human support",
            "context": {
              "issue_type": "{{issue_type}}",
              "login_problem": "{{login_problem}}"
            }
          }
        },
        {
          "id": "end",
          "type": "end",
          "config": {}
        }
      ],
      "edges": [
        {"from": "greeting", "to": "main_issue_type"},
        {"from": "unlock_account", "to": "unlock_confirm"},
        {"from": "perform_unlock", "to": "end"}
        // Note: edges for question nodes are handled by next_node_id in options
      ]
    }
  }

  ---
  Visual Flow Diagram

  Here's what the user experiences:

  User: "I have a connection issue"
    ↓
  AI: "What type of connection issue?"
      [Network problems] [Can't login] [Error messages]
    ↓
  User clicks: "Can't login"
    ↓
  AI: "What happens when you try to login?"
      [Wrong password] [Account locked] [Page won't load] [Other]
    ↓
  User clicks: "Account locked"
    ↓
  AI: "I see your account is locked. I can unlock it for you."
  AI: "Would you like me to unlock your account?"
      [Yes, unlock my account] [Talk to support instead]
    ↓
  User clicks: "Yes, unlock my account"
    ↓
  AI: [Calls webhook to unlock account]
  AI: "✓ Your account has been unlocked! You can now login."

  ---
  Key Features That Support This

  1. Question Nodes with next_node_id

  Each option can point to a different node:

  {
    "type": "question",
    "config": {
      "question": "What's the issue?",
      "options": [
        {"label": "Option A", "value": "a", "next_node_id": "path_a"},
        {"label": "Option B", "value": "b", "next_node_id": "path_b"},
        {"label": "Option C", "value": "c", "next_node_id": "path_c"}
      ]
    }
  }

  Each option leads to a completely different branch!

  2. Conditional Edges

  You can also use conditions for more complex routing:

  {
    "edges": [
      {
        "from": "check_issue",
        "to": "simple_fix",
        "condition": {
          "variable": "severity",
          "equals": "low"
        }
      },
      {
        "from": "check_issue",
        "to": "escalate",
        "condition": {
          "variable": "severity",
          "equals": "high"
        }
      }
    ]
  }

  3. Variable Storage

  Each answer is saved and can be used later:

  {
    "type": "question",
    "config": {
      "variable": "issue_type"  // Saves user's choice
    }
  }

  // Later, use it in escalation context
  {
    "type": "escalate",
    "config": {
      "context": {
        "issue": "{{issue_type}}",
        "sub_issue": "{{login_problem}}"
      }
    }
  }

  ---
  Real-World Example: MTN-Style Support

  Here's a complete MTN-style customer support flow:

  {
    "name": "MTN Customer Support",
    "nodes": {
      "start_node_id": "welcome",
      "nodes": [
        {
          "id": "welcome",
          "type": "message",
          "config": {
            "message": "Welcome to MTN Support! I'm here to help."
          }
        },
        {
          "id": "main_menu",
          "type": "question",
          "config": {
            "question": "What can I help you with today?",
            "options": [
              {"label": "Data & Internet", "value": "data", "next_node_id": "data_issues"},
              {"label": "Airtime & Billing", "value": "billing", "next_node_id": "billing_issues"},
              {"label": "Network Coverage", "value": "network", "next_node_id": "network_issues"},
              {"label": "Account Services", "value": "account", "next_node_id": "account_services"}
            ]
          }
        },

        // DATA BRANCH
        {
          "id": "data_issues",
          "type": "question",
          "config": {
            "question": "What data issue are you experiencing?",
            "options": [
              {"label": "No internet", "value": "no_internet", "next_node_id": "data_troubleshoot"},
              {"label": "Slow internet", "value": "slow", "next_node_id": "speed_check"},
              {"label": "Can't activate data", "value": "activate", "next_node_id": "activation_help"},
              {"label": "Check data balance", "value": "balance", "next_node_id": "check_balance"}
            ]
          }
        },
        {
          "id": "data_troubleshoot",
          "type": "question",
          "config": {
            "question": "Have you tried turning mobile data off and on?",
            "options": [
              {"label": "Yes, still not working", "value": "yes", "next_node_id": "advanced_troubleshoot"},
              {"label": "No, let me try that", "value": "no", "next_node_id": "basic_steps"}
            ]
          }
        },
        {
          "id": "advanced_troubleshoot",
          "type": "question",
          "config": {
            "question": "Let's try resetting your network settings. Would you like step-by-step instructions?",
            "options": [
              {"label": "Yes, show me how", "value": "yes", "next_node_id": "reset_instructions"},
              {"label": "I need an agent", "value": "agent", "next_node_id": "escalate_data"}
            ]
          }
        },

        // BILLING BRANCH
        {
          "id": "billing_issues",
          "type": "question",
          "config": {
            "question": "How can I help with billing?",
            "options": [
              {"label": "Check airtime balance", "value": "balance", "next_node_id": "airtime_balance"},
              {"label": "Dispute a charge", "value": "dispute", "next_node_id": "charge_dispute"},
              {"label": "Payment didn't go through", "value": "payment", "next_node_id": "payment_issue"},
              {"label": "Request refund", "value": "refund", "next_node_id": "refund_request"}
            ]
          }
        },
        {
          "id": "charge_dispute",
          "type": "question",
          "config": {
            "question": "What type of charge are you disputing?",
            "options": [
              {"label": "Data charge", "value": "data", "next_node_id": "data_charge_dispute"},
              {"label": "Subscription charge", "value": "subscription", "next_node_id": "sub_charge_dispute"},
              {"label": "Unknown charge", "value": "unknown", "next_node_id": "unknown_charge"}
            ]
          }
        },

        // ... etc ...
      ]
    }
  }

  ---
  Flow Depth: How Deep Can You Go?

  Answer: As deep as you need!

  You can have:
  - Level 1: Main categories (Data, Billing, Network)
  - Level 2: Sub-issues (No internet, Slow internet, Can't activate)
  - Level 3: Troubleshooting steps (Tried restart? Reset settings?)
  - Level 4: Advanced options (Show instructions? Contact agent?)
  - Level 5+: Even deeper if needed!

  There's no limit to how many levels you can nest.

  ---
  Best Practices for Nested Flows

  1. Limit Depth to 3-4 Levels

  Too many levels = confused users
  ✅ Good: Issue → Sub-issue → Solution
  ❌ Bad: Issue → Category → Subcategory → Sub-subcategory → Type → Subtype

  2. Provide Escape Hatch

  Always offer "Talk to agent" or "None of these":
  {
    "options": [
      {"label": "Option A", "value": "a"},
      {"label": "Option B", "value": "b"},
      {"label": "None of these - talk to agent", "value": "escalate"}
    ]
  }

  3. Use Variables for Context

  Save answers at each level:
  {"variable": "main_issue"}      // Level 1
  {"variable": "sub_issue"}        // Level 2
  {"variable": "specific_problem"} // Level 3

  // Then use in escalation:
  "context": {
    "main": "{{main_issue}}",
    "sub": "{{sub_issue}}",
    "specific": "{{specific_problem}}"
  }

  4. Show Progress

  Let users know where they are:
  {
    "message": "Step 2 of 3: Let's narrow this down..."
  }

  ---
  Summary

  Your exact scenario is fully supported!

  ✅ Connection issues → [Network | Login | Error]
    ✅ Can't login → [Wrong password | Account locked | Won't load]
      ✅ Account locked → [Unlock now | Talk to agent]

  How it works:
  - Each question node = one level of choices
  - Each option = can go to different next node
  - Each path = independent troubleshooting flow
  - Variables = remember choices for context

  Your flow system is just as powerful as MTN's or any enterprise customer support! 🚀