"""
Seed database with pre-built flow templates.

Creates 10 comprehensive flow templates covering common use cases:
- Sales: Welcome & Lead Qualification, Product Demo Request, Appointment Booking, Upgrade Inquiry
- Support: FAQ Handler, Billing Inquiry, Bug Report, Refund Request
- Engagement: Feature Request, Customer Feedback
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.db.session import SessionLocal
from app.db.models.flow import FlowTemplate, FlowTriggerType
from app.schemas.flow import FlowTemplateCreate
from loguru import logger


def get_template_data():
    """Get all flow template definitions."""

    templates = [
        # 1. Welcome & Lead Qualification (Sales)
        {
            "name": "Welcome & Lead Qualification",
            "description": "Greet new visitors, understand their needs, and qualify them as potential leads",
            "category": "sales",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["hello", "hi", "hey", "start", "info"],
                "intents": ["greeting", "information_request"]
            },
            "template_nodes": {
                "start_node_id": "welcome",
                "nodes": [
                    {
                        "id": "welcome",
                        "type": "message",
                        "config": {
                            "message": "Welcome! I'm here to help you learn more about our products and services. Let me ask you a few quick questions to better assist you.",
                            "next_node_id": "ask_name"
                        }
                    },
                    {
                        "id": "ask_name",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your name?",
                            "variable": "customer_name",
                            "validation": {"required": True, "min_length": 2},
                            "next_node_id": "ask_company"
                        }
                    },
                    {
                        "id": "ask_company",
                        "type": "text_input",
                        "config": {
                            "prompt": "What company do you represent?",
                            "variable": "company_name",
                            "validation": {"required": False},
                            "next_node_id": "ask_need"
                        }
                    },
                    {
                        "id": "ask_need",
                        "type": "question",
                        "config": {
                            "question": "What brings you here today?",
                            "options": [
                                {"label": "Looking for a solution", "value": "solution"},
                                {"label": "Just browsing", "value": "browsing"},
                                {"label": "Need pricing info", "value": "pricing"},
                                {"label": "Other", "value": "other"}
                            ],
                            "variable": "visit_reason",
                            "next_node_id": "webhook_crm"
                        }
                    },
                    {
                        "id": "webhook_crm",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-crm.com/api/leads",
                            "method": "POST",
                            "headers": {"Content-Type": "application/json"},
                            "body": {
                                "name": "{{customer_name}}",
                                "company": "{{company_name}}",
                                "reason": "{{visit_reason}}",
                                "source": "chatbot"
                            },
                            "next_node_id": "thank_you"
                        }
                    },
                    {
                        "id": "thank_you",
                        "type": "message",
                        "config": {
                            "message": "Thanks {{customer_name}}! I've captured your information. How can I help you today?",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "welcome", "to": "ask_name"},
                    {"from": "ask_name", "to": "ask_company"},
                    {"from": "ask_company", "to": "ask_need"},
                    {"from": "ask_need", "to": "webhook_crm"},
                    {"from": "webhook_crm", "to": "thank_you"},
                    {"from": "thank_you", "to": "end"}
                ]
            }
        },

        # 2. FAQ Handler (Support)
        {
            "name": "FAQ Handler",
            "description": "Automatically answer frequently asked questions using RAG-powered knowledge base search",
            "category": "support",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["help", "faq", "question", "how to"],
                "intents": ["question", "help_request"]
            },
            "template_nodes": {
                "start_node_id": "greet",
                "nodes": [
                    {
                        "id": "greet",
                        "type": "message",
                        "config": {
                            "message": "I can help answer your questions! What would you like to know?",
                            "next_node_id": "get_question"
                        }
                    },
                    {
                        "id": "get_question",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please type your question:",
                            "variable": "user_question",
                            "validation": {"required": True, "min_length": 5},
                            "next_node_id": "search_knowledge"
                        }
                    },
                    {
                        "id": "search_knowledge",
                        "type": "rag_query",
                        "config": {
                            "query": "{{user_question}}",
                            "result_variable": "answer",
                            "confidence_threshold": 0.7,
                            "next_node_id": "provide_answer"
                        }
                    },
                    {
                        "id": "provide_answer",
                        "type": "message",
                        "config": {
                            "message": "{{answer}}\n\nDid this answer your question?",
                            "next_node_id": "satisfaction_check"
                        }
                    },
                    {
                        "id": "satisfaction_check",
                        "type": "question",
                        "config": {
                            "question": "Was this helpful?",
                            "options": [
                                {"label": "Yes, thank you!", "value": "yes", "next_node_id": "end"},
                                {"label": "No, I need more help", "value": "no", "next_node_id": "escalate"}
                            ],
                            "variable": "is_satisfied"
                        }
                    },
                    {
                        "id": "escalate",
                        "type": "escalate",
                        "config": {
                            "reason": "Customer needs additional help beyond FAQ",
                            "context": {
                                "question": "{{user_question}}",
                                "attempted_answer": "{{answer}}"
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
                    {"from": "greet", "to": "get_question"},
                    {"from": "get_question", "to": "search_knowledge"},
                    {"from": "search_knowledge", "to": "provide_answer"},
                    {"from": "provide_answer", "to": "satisfaction_check"},
                    {"from": "satisfaction_check", "to": "end", "condition": {"variable": "is_satisfied", "equals": "yes"}},
                    {"from": "satisfaction_check", "to": "escalate", "condition": {"variable": "is_satisfied", "equals": "no"}},
                    {"from": "escalate", "to": "end"}
                ]
            }
        },

        # 3. Product Demo Request (Sales)
        {
            "name": "Product Demo Request",
            "description": "Capture demo requests and schedule product demonstrations with qualified prospects",
            "category": "sales",
            "min_tier": "professional",
            "trigger_config": {
                "keywords": ["demo", "demonstration", "show me", "trial"],
                "intents": ["demo_request", "product_interest"]
            },
            "template_nodes": {
                "start_node_id": "intro",
                "nodes": [
                    {
                        "id": "intro",
                        "type": "message",
                        "config": {
                            "message": "Great! I'd be happy to set up a product demo for you. Let me gather some details.",
                            "next_node_id": "get_name"
                        }
                    },
                    {
                        "id": "get_name",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your full name?",
                            "variable": "full_name",
                            "validation": {"required": True},
                            "next_node_id": "get_email"
                        }
                    },
                    {
                        "id": "get_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your email address?",
                            "variable": "email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "get_company_size"
                        }
                    },
                    {
                        "id": "get_company_size",
                        "type": "question",
                        "config": {
                            "question": "How many employees does your company have?",
                            "options": [
                                {"label": "1-10", "value": "small"},
                                {"label": "11-50", "value": "medium"},
                                {"label": "51-200", "value": "large"},
                                {"label": "201+", "value": "enterprise"}
                            ],
                            "variable": "company_size",
                            "next_node_id": "get_timeline"
                        }
                    },
                    {
                        "id": "get_timeline",
                        "type": "question",
                        "config": {
                            "question": "When are you looking to implement a solution?",
                            "options": [
                                {"label": "Immediately", "value": "immediate"},
                                {"label": "Within 1 month", "value": "1_month"},
                                {"label": "1-3 months", "value": "3_months"},
                                {"label": "Just exploring", "value": "exploring"}
                            ],
                            "variable": "timeline",
                            "next_node_id": "create_lead"
                        }
                    },
                    {
                        "id": "create_lead",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/demo-requests",
                            "method": "POST",
                            "body": {
                                "name": "{{full_name}}",
                                "email": "{{email}}",
                                "company_size": "{{company_size}}",
                                "timeline": "{{timeline}}",
                                "status": "demo_requested"
                            },
                            "next_node_id": "confirmation"
                        }
                    },
                    {
                        "id": "confirmation",
                        "type": "message",
                        "config": {
                            "message": "Perfect! I've scheduled your demo request. Our team will contact you at {{email}} within 24 hours to arrange a convenient time.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "intro", "to": "get_name"},
                    {"from": "get_name", "to": "get_email"},
                    {"from": "get_email", "to": "get_company_size"},
                    {"from": "get_company_size", "to": "get_timeline"},
                    {"from": "get_timeline", "to": "create_lead"},
                    {"from": "create_lead", "to": "confirmation"},
                    {"from": "confirmation", "to": "end"}
                ]
            }
        },

        # 4. Billing Inquiry (Support)
        {
            "name": "Billing Inquiry",
            "description": "Handle billing questions, payment issues, and invoice requests",
            "category": "support",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["billing", "payment", "invoice", "charge", "subscription"],
                "intents": ["billing_inquiry", "payment_issue"]
            },
            "template_nodes": {
                "start_node_id": "greeting",
                "nodes": [
                    {
                        "id": "greeting",
                        "type": "message",
                        "config": {
                            "message": "I can help with billing questions. What specific issue can I assist you with?",
                            "next_node_id": "inquiry_type"
                        }
                    },
                    {
                        "id": "inquiry_type",
                        "type": "question",
                        "config": {
                            "question": "What type of billing inquiry do you have?",
                            "options": [
                                {"label": "View/Download Invoice", "value": "invoice"},
                                {"label": "Payment Failed", "value": "payment_failed"},
                                {"label": "Update Payment Method", "value": "update_payment"},
                                {"label": "Billing Amount Question", "value": "amount_question"},
                                {"label": "Other", "value": "other"}
                            ],
                            "variable": "inquiry_type",
                            "next_node_id": "get_account_email"
                        }
                    },
                    {
                        "id": "get_account_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please provide the email associated with your account:",
                            "variable": "account_email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "rag_search"
                        }
                    },
                    {
                        "id": "rag_search",
                        "type": "rag_query",
                        "config": {
                            "query": "billing {{inquiry_type}}",
                            "result_variable": "billing_info",
                            "confidence_threshold": 0.6,
                            "next_node_id": "provide_info"
                        }
                    },
                    {
                        "id": "provide_info",
                        "type": "message",
                        "config": {
                            "message": "{{billing_info}}\n\nDoes this resolve your issue?",
                            "next_node_id": "resolved_check"
                        }
                    },
                    {
                        "id": "resolved_check",
                        "type": "question",
                        "config": {
                            "question": "Is your billing question resolved?",
                            "options": [
                                {"label": "Yes, thank you", "value": "yes", "next_node_id": "end"},
                                {"label": "No, need human help", "value": "no", "next_node_id": "escalate_billing"}
                            ],
                            "variable": "is_resolved"
                        }
                    },
                    {
                        "id": "escalate_billing",
                        "type": "escalate",
                        "config": {
                            "reason": "Complex billing issue requires human support",
                            "context": {
                                "inquiry_type": "{{inquiry_type}}",
                                "account_email": "{{account_email}}"
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
                    {"from": "greeting", "to": "inquiry_type"},
                    {"from": "inquiry_type", "to": "get_account_email"},
                    {"from": "get_account_email", "to": "rag_search"},
                    {"from": "rag_search", "to": "provide_info"},
                    {"from": "provide_info", "to": "resolved_check"},
                    {"from": "resolved_check", "to": "end", "condition": {"variable": "is_resolved", "equals": "yes"}},
                    {"from": "resolved_check", "to": "escalate_billing", "condition": {"variable": "is_resolved", "equals": "no"}},
                    {"from": "escalate_billing", "to": "end"}
                ]
            }
        },

        # 5. Feature Request (Engagement)
        {
            "name": "Feature Request",
            "description": "Collect and organize customer feature requests and product feedback",
            "category": "engagement",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["feature", "suggestion", "request", "would be nice", "can you add"],
                "intents": ["feature_request", "product_feedback"]
            },
            "template_nodes": {
                "start_node_id": "welcome",
                "nodes": [
                    {
                        "id": "welcome",
                        "type": "message",
                        "config": {
                            "message": "We love hearing from our users! Please share your feature request or suggestion.",
                            "next_node_id": "get_feature"
                        }
                    },
                    {
                        "id": "get_feature",
                        "type": "text_input",
                        "config": {
                            "prompt": "What feature would you like to see?",
                            "variable": "feature_description",
                            "validation": {"required": True, "min_length": 10},
                            "next_node_id": "get_use_case"
                        }
                    },
                    {
                        "id": "get_use_case",
                        "type": "text_input",
                        "config": {
                            "prompt": "How would this feature help you? (Describe your use case)",
                            "variable": "use_case",
                            "validation": {"required": True},
                            "next_node_id": "priority_level"
                        }
                    },
                    {
                        "id": "priority_level",
                        "type": "question",
                        "config": {
                            "question": "How important is this feature to you?",
                            "options": [
                                {"label": "Critical - blocking my work", "value": "critical"},
                                {"label": "High - would really help", "value": "high"},
                                {"label": "Medium - nice to have", "value": "medium"},
                                {"label": "Low - just a thought", "value": "low"}
                            ],
                            "variable": "priority",
                            "next_node_id": "get_email"
                        }
                    },
                    {
                        "id": "get_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "Email (optional - to follow up on this request):",
                            "variable": "contact_email",
                            "validation": {"required": False, "type": "email"},
                            "next_node_id": "submit_request"
                        }
                    },
                    {
                        "id": "submit_request",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/feature-requests",
                            "method": "POST",
                            "body": {
                                "feature": "{{feature_description}}",
                                "use_case": "{{use_case}}",
                                "priority": "{{priority}}",
                                "email": "{{contact_email}}",
                                "source": "chatbot"
                            },
                            "next_node_id": "thank_you"
                        }
                    },
                    {
                        "id": "thank_you",
                        "type": "message",
                        "config": {
                            "message": "Thank you for your feedback! We've recorded your feature request and our product team will review it.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "welcome", "to": "get_feature"},
                    {"from": "get_feature", "to": "get_use_case"},
                    {"from": "get_use_case", "to": "priority_level"},
                    {"from": "priority_level", "to": "get_email"},
                    {"from": "get_email", "to": "submit_request"},
                    {"from": "submit_request", "to": "thank_you"},
                    {"from": "thank_you", "to": "end"}
                ]
            }
        },

        # 6. Bug Report (Support)
        {
            "name": "Bug Report",
            "description": "Systematic bug reporting workflow to collect all necessary information for technical support",
            "category": "support",
            "min_tier": "professional",
            "trigger_config": {
                "keywords": ["bug", "error", "not working", "broken", "issue"],
                "intents": ["bug_report", "technical_issue"]
            },
            "template_nodes": {
                "start_node_id": "intro",
                "nodes": [
                    {
                        "id": "intro",
                        "type": "message",
                        "config": {
                            "message": "I'm sorry you're experiencing an issue. Let me help you report this bug so our team can fix it quickly.",
                            "next_node_id": "describe_issue"
                        }
                    },
                    {
                        "id": "describe_issue",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please describe what's happening:",
                            "variable": "issue_description",
                            "validation": {"required": True, "min_length": 10},
                            "next_node_id": "expected_behavior"
                        }
                    },
                    {
                        "id": "expected_behavior",
                        "type": "text_input",
                        "config": {
                            "prompt": "What did you expect to happen instead?",
                            "variable": "expected_behavior",
                            "validation": {"required": True},
                            "next_node_id": "steps_to_reproduce"
                        }
                    },
                    {
                        "id": "steps_to_reproduce",
                        "type": "text_input",
                        "config": {
                            "prompt": "What steps led to this issue? (Be as specific as possible)",
                            "variable": "reproduction_steps",
                            "validation": {"required": True},
                            "next_node_id": "severity"
                        }
                    },
                    {
                        "id": "severity",
                        "type": "question",
                        "config": {
                            "question": "How severe is this issue?",
                            "options": [
                                {"label": "Critical - can't use product", "value": "critical"},
                                {"label": "High - major feature broken", "value": "high"},
                                {"label": "Medium - workaround exists", "value": "medium"},
                                {"label": "Low - minor annoyance", "value": "low"}
                            ],
                            "variable": "severity",
                            "next_node_id": "get_contact"
                        }
                    },
                    {
                        "id": "get_contact",
                        "type": "text_input",
                        "config": {
                            "prompt": "Your email (for updates on this bug):",
                            "variable": "reporter_email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "create_ticket"
                        }
                    },
                    {
                        "id": "create_ticket",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/bug-reports",
                            "method": "POST",
                            "body": {
                                "description": "{{issue_description}}",
                                "expected": "{{expected_behavior}}",
                                "steps": "{{reproduction_steps}}",
                                "severity": "{{severity}}",
                                "reporter": "{{reporter_email}}",
                                "source": "chatbot"
                            },
                            "next_node_id": "confirmation"
                        }
                    },
                    {
                        "id": "confirmation",
                        "type": "message",
                        "config": {
                            "message": "Thank you for reporting this issue. We've created a bug ticket and will update you at {{reporter_email}} as we investigate.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "urgent_escalate",
                        "type": "escalate",
                        "config": {
                            "reason": "Critical bug requires immediate attention",
                            "context": {
                                "severity": "{{severity}}",
                                "description": "{{issue_description}}"
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
                    {"from": "intro", "to": "describe_issue"},
                    {"from": "describe_issue", "to": "expected_behavior"},
                    {"from": "expected_behavior", "to": "steps_to_reproduce"},
                    {"from": "steps_to_reproduce", "to": "severity"},
                    {"from": "severity", "to": "get_contact"},
                    {"from": "get_contact", "to": "create_ticket"},
                    {"from": "create_ticket", "to": "confirmation"},
                    {"from": "confirmation", "to": "urgent_escalate", "condition": {"variable": "severity", "equals": "critical"}},
                    {"from": "confirmation", "to": "end", "condition": {"variable": "severity", "not_equals": "critical"}},
                    {"from": "urgent_escalate", "to": "end"}
                ]
            }
        },

        # 7. Appointment Booking (Sales)
        {
            "name": "Appointment Booking",
            "description": "Schedule appointments, meetings, or consultations with appropriate team members",
            "category": "sales",
            "min_tier": "professional",
            "trigger_config": {
                "keywords": ["appointment", "schedule", "meeting", "book", "calendar"],
                "intents": ["appointment_request", "schedule_meeting"]
            },
            "template_nodes": {
                "start_node_id": "welcome",
                "nodes": [
                    {
                        "id": "welcome",
                        "type": "message",
                        "config": {
                            "message": "I can help you schedule an appointment. Let me gather some information.",
                            "next_node_id": "appointment_type"
                        }
                    },
                    {
                        "id": "appointment_type",
                        "type": "question",
                        "config": {
                            "question": "What type of appointment do you need?",
                            "options": [
                                {"label": "Sales Consultation", "value": "sales"},
                                {"label": "Technical Support", "value": "support"},
                                {"label": "Product Demo", "value": "demo"},
                                {"label": "General Meeting", "value": "general"}
                            ],
                            "variable": "appointment_type",
                            "next_node_id": "get_name"
                        }
                    },
                    {
                        "id": "get_name",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your name?",
                            "variable": "customer_name",
                            "validation": {"required": True},
                            "next_node_id": "get_email"
                        }
                    },
                    {
                        "id": "get_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your email address?",
                            "variable": "customer_email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "get_phone"
                        }
                    },
                    {
                        "id": "get_phone",
                        "type": "text_input",
                        "config": {
                            "prompt": "Phone number (optional):",
                            "variable": "customer_phone",
                            "validation": {"required": False},
                            "next_node_id": "preferred_time"
                        }
                    },
                    {
                        "id": "preferred_time",
                        "type": "question",
                        "config": {
                            "question": "When would you prefer to meet?",
                            "options": [
                                {"label": "Morning (9am-12pm)", "value": "morning"},
                                {"label": "Afternoon (12pm-5pm)", "value": "afternoon"},
                                {"label": "Evening (5pm-7pm)", "value": "evening"}
                            ],
                            "variable": "time_preference",
                            "next_node_id": "additional_notes"
                        }
                    },
                    {
                        "id": "additional_notes",
                        "type": "text_input",
                        "config": {
                            "prompt": "Any additional information or specific topics to discuss? (optional)",
                            "variable": "notes",
                            "validation": {"required": False},
                            "next_node_id": "book_appointment"
                        }
                    },
                    {
                        "id": "book_appointment",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/appointments",
                            "method": "POST",
                            "body": {
                                "type": "{{appointment_type}}",
                                "name": "{{customer_name}}",
                                "email": "{{customer_email}}",
                                "phone": "{{customer_phone}}",
                                "time_preference": "{{time_preference}}",
                                "notes": "{{notes}}",
                                "status": "pending"
                            },
                            "next_node_id": "confirmation"
                        }
                    },
                    {
                        "id": "confirmation",
                        "type": "message",
                        "config": {
                            "message": "Perfect! I've submitted your appointment request. You'll receive a calendar invite at {{customer_email}} within 24 hours with the confirmed time.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "welcome", "to": "appointment_type"},
                    {"from": "appointment_type", "to": "get_name"},
                    {"from": "get_name", "to": "get_email"},
                    {"from": "get_email", "to": "get_phone"},
                    {"from": "get_phone", "to": "preferred_time"},
                    {"from": "preferred_time", "to": "additional_notes"},
                    {"from": "additional_notes", "to": "book_appointment"},
                    {"from": "book_appointment", "to": "confirmation"},
                    {"from": "confirmation", "to": "end"}
                ]
            }
        },

        # 8. Customer Feedback (Engagement)
        {
            "name": "Customer Feedback",
            "description": "Collect structured customer feedback, satisfaction ratings, and improvement suggestions",
            "category": "engagement",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["feedback", "review", "rating", "experience", "satisfaction"],
                "intents": ["provide_feedback", "satisfaction_survey"]
            },
            "template_nodes": {
                "start_node_id": "intro",
                "nodes": [
                    {
                        "id": "intro",
                        "type": "message",
                        "config": {
                            "message": "We value your feedback! Please take a moment to share your experience with us.",
                            "next_node_id": "overall_satisfaction"
                        }
                    },
                    {
                        "id": "overall_satisfaction",
                        "type": "question",
                        "config": {
                            "question": "How satisfied are you with our product/service?",
                            "options": [
                                {"label": "Very Satisfied", "value": "5"},
                                {"label": "Satisfied", "value": "4"},
                                {"label": "Neutral", "value": "3"},
                                {"label": "Dissatisfied", "value": "2"},
                                {"label": "Very Dissatisfied", "value": "1"}
                            ],
                            "variable": "satisfaction_rating",
                            "next_node_id": "what_working"
                        }
                    },
                    {
                        "id": "what_working",
                        "type": "text_input",
                        "config": {
                            "prompt": "What do you like most about our product/service?",
                            "variable": "positive_feedback",
                            "validation": {"required": False},
                            "next_node_id": "what_improve"
                        }
                    },
                    {
                        "id": "what_improve",
                        "type": "text_input",
                        "config": {
                            "prompt": "What could we improve?",
                            "variable": "improvement_suggestions",
                            "validation": {"required": False},
                            "next_node_id": "nps_score"
                        }
                    },
                    {
                        "id": "nps_score",
                        "type": "question",
                        "config": {
                            "question": "How likely are you to recommend us to a friend or colleague? (0-10)",
                            "options": [
                                {"label": "10 - Extremely likely", "value": "10"},
                                {"label": "9", "value": "9"},
                                {"label": "8", "value": "8"},
                                {"label": "7", "value": "7"},
                                {"label": "6", "value": "6"},
                                {"label": "5", "value": "5"},
                                {"label": "4", "value": "4"},
                                {"label": "3", "value": "3"},
                                {"label": "2", "value": "2"},
                                {"label": "1", "value": "1"},
                                {"label": "0 - Not at all likely", "value": "0"}
                            ],
                            "variable": "nps_score",
                            "next_node_id": "get_email"
                        }
                    },
                    {
                        "id": "get_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "Email (optional - for follow-up):",
                            "variable": "feedback_email",
                            "validation": {"required": False, "type": "email"},
                            "next_node_id": "submit_feedback"
                        }
                    },
                    {
                        "id": "submit_feedback",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/feedback",
                            "method": "POST",
                            "body": {
                                "satisfaction": "{{satisfaction_rating}}",
                                "positive": "{{positive_feedback}}",
                                "improvements": "{{improvement_suggestions}}",
                                "nps": "{{nps_score}}",
                                "email": "{{feedback_email}}",
                                "source": "chatbot"
                            },
                            "next_node_id": "thank_you"
                        }
                    },
                    {
                        "id": "thank_you",
                        "type": "message",
                        "config": {
                            "message": "Thank you for your valuable feedback! We truly appreciate you taking the time to help us improve.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "intro", "to": "overall_satisfaction"},
                    {"from": "overall_satisfaction", "to": "what_working"},
                    {"from": "what_working", "to": "what_improve"},
                    {"from": "what_improve", "to": "nps_score"},
                    {"from": "nps_score", "to": "get_email"},
                    {"from": "get_email", "to": "submit_feedback"},
                    {"from": "submit_feedback", "to": "thank_you"},
                    {"from": "thank_you", "to": "end"}
                ]
            }
        },

        # 9. Refund Request (Support)
        {
            "name": "Refund Request",
            "description": "Handle refund requests with proper validation, reason collection, and approval workflow",
            "category": "support",
            "min_tier": "professional",
            "trigger_config": {
                "keywords": ["refund", "money back", "cancel subscription", "return"],
                "intents": ["refund_request", "cancellation"]
            },
            "template_nodes": {
                "start_node_id": "greeting",
                "nodes": [
                    {
                        "id": "greeting",
                        "type": "message",
                        "config": {
                            "message": "I understand you'd like to request a refund. Let me help you with that process.",
                            "next_node_id": "get_email"
                        }
                    },
                    {
                        "id": "get_email",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please provide your account email:",
                            "variable": "account_email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "get_order_id"
                        }
                    },
                    {
                        "id": "get_order_id",
                        "type": "text_input",
                        "config": {
                            "prompt": "What's your order/transaction ID? (Found in your receipt email)",
                            "variable": "order_id",
                            "validation": {"required": True},
                            "next_node_id": "refund_reason"
                        }
                    },
                    {
                        "id": "refund_reason",
                        "type": "question",
                        "config": {
                            "question": "What's the reason for your refund request?",
                            "options": [
                                {"label": "Product didn't meet expectations", "value": "expectations"},
                                {"label": "Technical issues", "value": "technical"},
                                {"label": "Purchased by mistake", "value": "mistake"},
                                {"label": "Found better alternative", "value": "alternative"},
                                {"label": "Too expensive", "value": "pricing"},
                                {"label": "Other", "value": "other"}
                            ],
                            "variable": "refund_reason",
                            "next_node_id": "additional_details"
                        }
                    },
                    {
                        "id": "additional_details",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please provide any additional details about your refund request:",
                            "variable": "refund_details",
                            "validation": {"required": False},
                            "next_node_id": "check_policy"
                        }
                    },
                    {
                        "id": "check_policy",
                        "type": "rag_query",
                        "config": {
                            "query": "refund policy eligibility",
                            "result_variable": "policy_info",
                            "confidence_threshold": 0.7,
                            "next_node_id": "show_policy"
                        }
                    },
                    {
                        "id": "show_policy",
                        "type": "message",
                        "config": {
                            "message": "Here's our refund policy:\n\n{{policy_info}}",
                            "next_node_id": "confirm_request"
                        }
                    },
                    {
                        "id": "confirm_request",
                        "type": "question",
                        "config": {
                            "question": "Would you like to proceed with the refund request?",
                            "options": [
                                {"label": "Yes, proceed with refund", "value": "yes", "next_node_id": "submit_refund"},
                                {"label": "No, I changed my mind", "value": "no", "next_node_id": "cancelled"}
                            ],
                            "variable": "proceed_refund"
                        }
                    },
                    {
                        "id": "submit_refund",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/refund-requests",
                            "method": "POST",
                            "body": {
                                "email": "{{account_email}}",
                                "order_id": "{{order_id}}",
                                "reason": "{{refund_reason}}",
                                "details": "{{refund_details}}",
                                "status": "pending_review"
                            },
                            "next_node_id": "confirmation"
                        }
                    },
                    {
                        "id": "confirmation",
                        "type": "message",
                        "config": {
                            "message": "Your refund request has been submitted. Our team will review it within 24-48 hours and contact you at {{account_email}}.",
                            "next_node_id": "escalate_urgent"
                        }
                    },
                    {
                        "id": "escalate_urgent",
                        "type": "escalate",
                        "config": {
                            "reason": "Refund request requires human review",
                            "context": {
                                "email": "{{account_email}}",
                                "reason": "{{refund_reason}}"
                            }
                        }
                    },
                    {
                        "id": "cancelled",
                        "type": "message",
                        "config": {
                            "message": "No problem! Is there anything else I can help you with?",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "greeting", "to": "get_email"},
                    {"from": "get_email", "to": "get_order_id"},
                    {"from": "get_order_id", "to": "refund_reason"},
                    {"from": "refund_reason", "to": "additional_details"},
                    {"from": "additional_details", "to": "check_policy"},
                    {"from": "check_policy", "to": "show_policy"},
                    {"from": "show_policy", "to": "confirm_request"},
                    {"from": "confirm_request", "to": "submit_refund", "condition": {"variable": "proceed_refund", "equals": "yes"}},
                    {"from": "submit_refund", "to": "confirmation"},
                    {"from": "confirmation", "to": "escalate_urgent"},
                    {"from": "escalate_urgent", "to": "end"},
                    {"from": "confirm_request", "to": "cancelled", "condition": {"variable": "proceed_refund", "equals": "no"}},
                    {"from": "cancelled", "to": "end"}
                ]
            }
        },

        # 10. Upgrade Inquiry (Sales)
        {
            "name": "Upgrade Inquiry",
            "description": "Handle subscription upgrade inquiries, compare plans, and facilitate upsells",
            "category": "sales",
            "min_tier": "lite",
            "trigger_config": {
                "keywords": ["upgrade", "pricing", "plans", "premium", "enterprise"],
                "intents": ["upgrade_inquiry", "pricing_question"]
            },
            "template_nodes": {
                "start_node_id": "welcome",
                "nodes": [
                    {
                        "id": "welcome",
                        "type": "message",
                        "config": {
                            "message": "Great to hear you're interested in upgrading! Let me help you find the perfect plan.",
                            "next_node_id": "current_plan"
                        }
                    },
                    {
                        "id": "current_plan",
                        "type": "question",
                        "config": {
                            "question": "What's your current plan?",
                            "options": [
                                {"label": "Trial", "value": "trial"},
                                {"label": "Lite", "value": "lite"},
                                {"label": "Professional", "value": "professional"},
                                {"label": "Not a customer yet", "value": "new"}
                            ],
                            "variable": "current_plan",
                            "next_node_id": "needs_assessment"
                        }
                    },
                    {
                        "id": "needs_assessment",
                        "type": "question",
                        "config": {
                            "question": "What's driving your interest in upgrading?",
                            "options": [
                                {"label": "Need more conversations", "value": "volume"},
                                {"label": "Need more team members", "value": "team"},
                                {"label": "Need advanced features", "value": "features"},
                                {"label": "Want better support", "value": "support"},
                                {"label": "All of the above", "value": "all"}
                            ],
                            "variable": "upgrade_reason",
                            "next_node_id": "team_size"
                        }
                    },
                    {
                        "id": "team_size",
                        "type": "question",
                        "config": {
                            "question": "How many team members need access?",
                            "options": [
                                {"label": "1-3", "value": "small"},
                                {"label": "4-10", "value": "medium"},
                                {"label": "11-20", "value": "large"},
                                {"label": "20+", "value": "enterprise"}
                            ],
                            "variable": "team_size",
                            "next_node_id": "get_plan_info"
                        }
                    },
                    {
                        "id": "get_plan_info",
                        "type": "rag_query",
                        "config": {
                            "query": "subscription plans pricing features comparison",
                            "result_variable": "plan_details",
                            "confidence_threshold": 0.7,
                            "next_node_id": "show_plans"
                        }
                    },
                    {
                        "id": "show_plans",
                        "type": "message",
                        "config": {
                            "message": "Based on your needs, here's what we recommend:\n\n{{plan_details}}",
                            "next_node_id": "interest_level"
                        }
                    },
                    {
                        "id": "interest_level",
                        "type": "question",
                        "config": {
                            "question": "What would you like to do next?",
                            "options": [
                                {"label": "Upgrade now", "value": "upgrade_now", "next_node_id": "get_contact"},
                                {"label": "Talk to sales", "value": "talk_sales", "next_node_id": "get_contact"},
                                {"label": "Need more time to think", "value": "think", "next_node_id": "browsing_response"},
                                {"label": "Just browsing", "value": "browsing", "next_node_id": "browsing_response"}
                            ],
                            "variable": "next_action"
                        }
                    },
                    {
                        "id": "get_contact",
                        "type": "text_input",
                        "config": {
                            "prompt": "Please provide your email so our team can reach out:",
                            "variable": "contact_email",
                            "validation": {"required": True, "type": "email"},
                            "next_node_id": "create_lead"
                        }
                    },
                    {
                        "id": "create_lead",
                        "type": "webhook",
                        "config": {
                            "url": "https://your-system.com/api/upgrade-leads",
                            "method": "POST",
                            "body": {
                                "current_plan": "{{current_plan}}",
                                "upgrade_reason": "{{upgrade_reason}}",
                                "team_size": "{{team_size}}",
                                "next_action": "{{next_action}}",
                                "email": "{{contact_email}}",
                                "status": "qualified"
                            },
                            "next_node_id": "sales_escalate"
                        }
                    },
                    {
                        "id": "sales_escalate",
                        "type": "escalate",
                        "config": {
                            "reason": "High-value upgrade inquiry - immediate sales attention needed",
                            "context": {
                                "current_plan": "{{current_plan}}",
                                "team_size": "{{team_size}}",
                                "email": "{{contact_email}}"
                            }
                        }
                    },
                    {
                        "id": "confirmation",
                        "type": "message",
                        "config": {
                            "message": "Perfect! Our sales team will contact you at {{contact_email}} within a few hours to help with your upgrade.",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "browsing_response",
                        "type": "message",
                        "config": {
                            "message": "No problem! Feel free to reach out when you're ready. Is there anything else I can help you with?",
                            "next_node_id": "end"
                        }
                    },
                    {
                        "id": "end",
                        "type": "end",
                        "config": {}
                    }
                ],
                "edges": [
                    {"from": "welcome", "to": "current_plan"},
                    {"from": "current_plan", "to": "needs_assessment"},
                    {"from": "needs_assessment", "to": "team_size"},
                    {"from": "team_size", "to": "get_plan_info"},
                    {"from": "get_plan_info", "to": "show_plans"},
                    {"from": "show_plans", "to": "interest_level"},
                    {"from": "interest_level", "to": "get_contact", "condition": {"variable": "next_action", "in": ["upgrade_now", "talk_sales"]}},
                    {"from": "get_contact", "to": "create_lead"},
                    {"from": "create_lead", "to": "sales_escalate"},
                    {"from": "sales_escalate", "to": "confirmation"},
                    {"from": "confirmation", "to": "end"},
                    {"from": "interest_level", "to": "browsing_response", "condition": {"variable": "next_action", "in": ["think", "browsing"]}},
                    {"from": "browsing_response", "to": "end"}
                ]
            }
        }
    ]

    return prune_unreachable_template_nodes(refine_template_data(normalize_template_data(templates)))


def normalize_template_data(templates: list[dict]) -> list[dict]:
    """
    Normalize legacy template node keys to current backend schema.

    - `variable` -> `save_to`
    - `result_variable` -> `save_to` (for rag_query)
    - Question `config.next_node_id` fallback copied into each option's `next_node_id`
    """
    normalized_templates: list[dict] = []

    for template in templates:
        template_copy = dict(template)
        template_nodes = dict(template_copy.get("template_nodes", {}) or {})
        nodes = template_nodes.get("nodes", [])

        normalized_nodes = []
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            node_copy = dict(node)
            config = dict(node_copy.get("config", {}) or {})
            node_type = node_copy.get("type")

            if "save_to" not in config and "variable" in config:
                config["save_to"] = config["variable"]
                config.pop("variable", None)

            if node_type == "rag_query" and "save_to" not in config and "result_variable" in config:
                config["save_to"] = config["result_variable"]
                config.pop("result_variable", None)

            if node_type == "question":
                options = config.get("options", [])
                fallback_next = config.get("next_node_id")
                if isinstance(options, list):
                    updated_options = []
                    for option in options:
                        if isinstance(option, dict):
                            option_copy = dict(option)
                            if fallback_next and not option_copy.get("next_node_id"):
                                option_copy["next_node_id"] = fallback_next
                            updated_options.append(option_copy)
                        else:
                            updated_options.append(option)
                    config["options"] = updated_options

            node_copy["config"] = config
            normalized_nodes.append(node_copy)

        template_nodes["nodes"] = normalized_nodes
        template_copy["template_nodes"] = template_nodes
        normalized_templates.append(template_copy)

    return normalized_templates


def refine_template_data(templates: list[dict]) -> list[dict]:
    """Refine templates to match current product direction and UX."""
    refined_templates: list[dict] = []

    for template in templates:
        template_copy = dict(template)
        trigger_config = dict(template_copy.get("trigger_config", {}) or {})
        keywords = trigger_config.get("keywords", [])
        if isinstance(keywords, list):
            trigger_config["keywords"] = [k for k in keywords if str(k).lower() != "start"]
        trigger_config.setdefault("match_mode", "word")
        trigger_config.setdefault("menu_label", template_copy.get("name", "Support"))
        trigger_config.setdefault("menu_description", template_copy.get("description", ""))
        trigger_config.setdefault("exclude_from_router", False)
        template_copy["trigger_config"] = trigger_config

        template_nodes = dict(template_copy.get("template_nodes", {}) or {})
        nodes = template_nodes.get("nodes", [])
        if not isinstance(nodes, list):
            refined_templates.append(template_copy)
            continue

        node_map = {node.get("id"): node for node in nodes if isinstance(node, dict)}
        template_name = str(template_copy.get("name", ""))

        # FAQ / Billing / Upgrade: use AI action after retrieval for natural responses.
        if template_name == "FAQ Handler" and isinstance(node_map.get("provide_answer"), dict):
            node_map["provide_answer"]["type"] = "ai_action"
            node_map["provide_answer"]["config"] = {
                "prompt": (
                    "Answer the customer's question using the retrieved knowledge. "
                    "Question: {{user_question}}"
                ),
                "use_rag": True,
                "rag_key": "answer",
                "save_to": "faq_ai_answer",
                "next_node_id": "satisfaction_check",
                "max_tokens": 350,
                "max_history_turns": 8,
            }

        if template_name == "Billing Inquiry" and isinstance(node_map.get("provide_info"), dict):
            node_map["provide_info"]["type"] = "ai_action"
            node_map["provide_info"]["config"] = {
                "prompt": (
                    "Give a clear, concise billing answer using retrieved policy/context. "
                    "Issue type: {{inquiry_type}}"
                ),
                "use_rag": True,
                "rag_key": "billing_info",
                "save_to": "billing_ai_answer",
                "next_node_id": "resolved_check",
                "max_tokens": 350,
                "max_history_turns": 8,
            }

        if template_name == "Upgrade Inquiry":
            if isinstance(node_map.get("show_plans"), dict):
                node_map["show_plans"]["type"] = "ai_action"
                node_map["show_plans"]["config"] = {
                    "prompt": (
                        "Summarize the best plan recommendation for this customer based on "
                        "current plan {{current_plan}}, reason {{upgrade_reason}}, team size {{team_size}}."
                    ),
                    "use_rag": True,
                    "rag_key": "plan_details",
                    "save_to": "upgrade_ai_summary",
                    "next_node_id": "interest_level",
                    "max_tokens": 350,
                    "max_history_turns": 8,
                }

            if isinstance(node_map.get("create_lead"), dict):
                node_map["create_lead"]["config"]["next_node_id"] = "confirmation"
            if isinstance(node_map.get("confirmation"), dict):
                node_map["confirmation"]["config"]["next_node_id"] = "end"

        # Bug report: add explicit condition branching instead of edge-only condition.
        if template_name == "Bug Report":
            confirmation_node = node_map.get("confirmation")
            if isinstance(confirmation_node, dict):
                confirmation_node["config"]["next_node_id"] = "check_critical"
            if "check_critical" not in node_map:
                nodes.append(
                    {
                        "id": "check_critical",
                        "type": "condition",
                        "config": {
                            "conditions": [
                                {
                                    "variable": "severity",
                                    "operator": "equals",
                                    "value": "critical",
                                    "next_node_id": "urgent_escalate",
                                },
                                {
                                    "variable": "severity",
                                    "operator": "not_equals",
                                    "value": "critical",
                                    "next_node_id": "end",
                                },
                            ],
                            "default_node_id": "end",
                        },
                    }
                )

        template_nodes["nodes"] = nodes
        template_copy["template_nodes"] = template_nodes
        refined_templates.append(template_copy)

    return refined_templates


def prune_unreachable_template_nodes(templates: list[dict]) -> list[dict]:
    """Prune unreachable nodes from each template using config-based routing."""
    pruned_templates: list[dict] = []

    for template in templates:
        template_copy = dict(template)
        template_nodes = dict(template_copy.get("template_nodes", {}) or {})
        nodes = template_nodes.get("nodes", [])
        start_node_id = template_nodes.get("start_node_id")

        if not isinstance(nodes, list) or not start_node_id:
            pruned_templates.append(template_copy)
            continue

        node_map = {
            node.get("id"): node
            for node in nodes
            if isinstance(node, dict) and node.get("id")
        }
        if start_node_id not in node_map:
            pruned_templates.append(template_copy)
            continue

        adjacency: dict[str, set[str]] = {}
        for node_id, node in node_map.items():
            config = node.get("config", {}) or {}
            targets: set[str] = set()

            next_node_id = config.get("next_node_id")
            if isinstance(next_node_id, str) and next_node_id:
                targets.add(next_node_id)

            default_node_id = config.get("default_node_id")
            if isinstance(default_node_id, str) and default_node_id:
                targets.add(default_node_id)

            options = config.get("options", [])
            if isinstance(options, list):
                for option in options:
                    if isinstance(option, dict):
                        target = option.get("next_node_id")
                        if isinstance(target, str) and target:
                            targets.add(target)

            conditions = config.get("conditions", [])
            if isinstance(conditions, list):
                for condition in conditions:
                    if isinstance(condition, dict):
                        target = condition.get("next_node_id")
                        if isinstance(target, str) and target:
                            targets.add(target)

            adjacency[node_id] = targets

        visited: set[str] = set()
        queue: list[str] = [start_node_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for nxt in adjacency.get(current, set()):
                if nxt not in visited and nxt in node_map:
                    queue.append(nxt)

        template_nodes["nodes"] = [
            node
            for node in nodes
            if isinstance(node, dict) and node.get("id") in visited
        ]
        template_copy["template_nodes"] = template_nodes
        pruned_templates.append(template_copy)

    return pruned_templates


def create_flow_templates(db):
    """Create flow templates in database."""

    templates_data = get_template_data()
    created_count = 0
    skipped_count = 0

    for template_data in templates_data:
        # Check if template already exists
        existing = db.query(FlowTemplate).filter(
            FlowTemplate.name == template_data["name"]
        ).first()

        if existing:
            logger.info(f"Skipped existing template: {template_data['name']}")
            skipped_count += 1
            continue

        # Create new template
        template = FlowTemplate(**template_data)
        db.add(template)
        created_count += 1
        logger.info(f"Created template: {template_data['name']} (category: {template_data['category']}, tier: {template_data['min_tier']})")

    db.commit()
    return created_count, skipped_count


def seed_flow_templates():
    """Main seeding function for flow templates."""
    logger.info("Starting flow template seeding...")

    db = SessionLocal()
    try:
        created, skipped = create_flow_templates(db)

        # Summary
        logger.success("\n" + "=" * 70)
        logger.success("Flow Template Seeding Completed Successfully!")
        logger.success("=" * 70)
        logger.success(f"\nResults:")
        logger.success(f"  - {created} templates created")
        logger.success(f"  - {skipped} templates skipped (already exist)")
        logger.success("\nTemplate Categories:")
        logger.success("  - Sales: 4 templates")
        logger.success("  - Support: 4 templates")
        logger.success("  - Engagement: 2 templates")
        logger.success("\nNode Types Included:")
        logger.success("  - message, question, text_input, webhook, rag_query, escalate, end")
        logger.success("=" * 70 + "\n")

        return True

    except Exception as e:
        logger.error(f"Error seeding flow templates: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_flow_templates()
