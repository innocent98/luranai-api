"""Run the real FastAPI app with ONLY the language model and vector search stood in.

Everything else is the real thing: HTTP route, Postgres, Redis, the lead/quote code, the ARQ
queue and (in a separate process) the worker. The stand-in 'model' is deterministic: it always
offers the quote, captures a name/team size/plan, pulls an e-mail address out of the visitor's
message when there is one, and writes the exact reply the production model wrote in the failed
prod test ('should I email it to you?'), so the guard is exercised for real.
"""
import re
import sys

import uvicorn

from app.services.chat_automation_orchestrator import AutomationTurnResult
from app.services.rag_service import RAGService

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


async def generate_sales_response(self, prompt, config, *, model_name=None, known_facts=None,
                                  company_website=None, image_urls=None):
    text = prompt.full_prompt
    last = text.rsplit("User message:", 1)[-1].split("Instructions:", 1)[0]
    found = EMAIL.search(last)
    captured = {"name": "Alice Obi", "email": found.group(0).rstrip(".,;") if found else None,
                "company": "Northwind", "team_size": "58", "industry": None,
                "plan_interest": "Growth"}
    signals = {"asked_features": False, "requested_demo": False, "mentioned_competitor": False,
               "ready_to_buy": True, "offer_meeting": False,
               "offer_quote": bool(re.search(r"how much|quote|people", last, re.I))}
    reply = ("Thanks! For 58 people the Growth plan fits. Your quote is below. "
             "Should I email it to you?")
    if re.search(r"email me", last, re.I):
        # What the production model improvised on a second "Yes, email me" (staging, 2026-10-31):
        # it repeated the full address from the conversation.
        known = EMAIL.findall(text)
        who = known[0].rstrip(".,;") if known else "the address you gave"
        reply = (f"Your Growth quote for \u20a662,050 was previously marked as sent to {who}. "
                 "Please check your inbox and spam folder. I can't send another email from here.")
        signals = {**signals, "offer_quote": False}
    return reply, {"intent": "PRICING_INQUIRY", "captured": captured, "signals": signals}, 50, False


async def retrieve(self, *, tenant_id, query, max_chunks, score_threshold):
    return [], []


async def handle_turn(self, **kwargs):
    return AutomationTurnResult(handled=False)


RAGService.generate_sales_response = generate_sales_response
RAGService.retrieve_relevant_chunks = retrieve
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator  # noqa: E402

ChatAutomationOrchestrator.handle_turn = handle_turn

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
