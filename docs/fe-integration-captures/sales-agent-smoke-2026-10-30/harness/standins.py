"""Local stand-ins for the sales-agent smoke run. Nothing here talks to the internet.

    python standins.py --llm-port 58090 --hook-port 58091 --out <dir>

* LLM stand-in (OpenAI-compatible, /v1/chat/completions + /v1/embeddings). The app's real
  OpenAI client is pointed at it with OPENAI_BASE_URL, so `generate_sales_response`, the JSON
  schema request, the response parsing and the token accounting all run for real. Only the
  *model's decision* is scripted: the stand-in reads the visitor's message out of the prompt and
  returns the JSON a competent model would. It also records every request (system prompt
  included) so we can prove the app built the directive correctly.
* Receiver (POST /crm, POST /slack, POST /crm-5xx): records the exact body the worker POSTs.
  The CRM stand-in answers with {"id": "crm-<n>"} so the Lead.crm_id write-back is exercised.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT: Path
_lock = threading.Lock()
_crm_counter = {"n": 0}


def _append(name: str, record: dict) -> None:
    with _lock:
        with (OUT / name).open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- scripted model

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _visitor_message(prompt_text: str) -> str:
    m = re.search(r"User message:\n(.*?)\n\nInstructions:", prompt_text, re.S)
    return (m.group(1) if m else prompt_text).strip()


def _first_question(system_text: str) -> str:
    m = re.search(r'Your qualifying question is: "(.*?)"', system_text)
    return m.group(1) if m else "What are you hoping to achieve?"


def _has(system_text: str, needle: str) -> bool:
    return needle in system_text


def _sales_json(system_text: str, visitor: str) -> dict:
    """What a competent model returns for the scripted visitor, given what the directive says."""
    v = visitor.lower()
    meeting_ok = _has(system_text, "Meeting scheduling IS available")
    plans_present = _has(system_text, "## Available Plans")
    signals = dict(
        asked_features=False,
        requested_demo=False,
        mentioned_competitor=False,
        ready_to_buy=False,
        offer_meeting=False,
        offer_quote=False,
    )
    captured = dict(
        name=None, email=None, company=None, team_size=None, industry=None, plan_interest=None
    )
    intent = "SUPPORT"
    reply = "Happy to help. What would you like to know?"

    if "how much" in v or "pricing" in v or "cost" in v:
        intent = "PRICING_INQUIRY"
        reply = (
            "Our plans start from the Starter tier and scale with your team. "
            + _first_question(system_text)
        )
        if not plans_present:
            reply = "I can help with pricing. " + _first_question(system_text)
    elif re.search(r"about (\d+) people", v):
        n = re.search(r"about (\d+) people", v).group(1)
        intent = "PRICING_INQUIRY"
        captured.update(team_size=n, industry="logistics")
        reply = (
            f"With {n} people the Professional plan gives you the most headroom. "
            "Could I get your name and email so I can prepare it for you?"
        )
    elif _EMAIL.search(visitor):
        intent = "PURCHASE_INTENT"
        email = _EMAIL.search(visitor).group(0)
        n = re.search(r"team of (\d+)", v)
        captured.update(
            name="Ada Okafor",
            email=email,
            company="Acme Group Logistics",
            team_size=n.group(1) if n else "40",
            industry="logistics",
            plan_interest="Professional",
        )
        signals.update(
            asked_features=True, mentioned_competitor=True, ready_to_buy=True, offer_quote=True
        )
        reply = "Thanks Ada. Here is your quotation for the Professional plan."
    elif "ready to buy" in v:
        intent = "PURCHASE_INTENT"
        signals.update(asked_features=True, mentioned_competitor=True, ready_to_buy=True)
        reply = "Wonderful, we will get you set up."
    elif "book" in v or "demo" in v or "call" in v:
        intent = "DEMO_REQUEST"
        signals.update(requested_demo=True, offer_meeting=meeting_ok)
        reply = (
            "Pick a time that suits you below."
            if meeting_ok
            else "I can connect you with our team to arrange a demo."
        )
    elif "refund" in v or "broken" in v or "not working" in v:
        intent = "SUPPORT"
        reply = "Sorry about that. Can you tell me what you are seeing?"
    return {"reply": reply, "intent": intent, "signals": signals, "captured": captured}


class LLMHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default stderr logging
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            inputs = payload.get("input")
            inputs = inputs if isinstance(inputs, list) else [inputs]
            vec = [1.0 / (1536**0.5)] * 1536
            self._send(
                200,
                {
                    "object": "list",
                    "model": payload.get("model", "embed-standin"),
                    "data": [
                        {"object": "embedding", "index": i, "embedding": vec}
                        for i, _ in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
            return

        messages = payload.get("messages", [])
        system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user_text = "\n".join(
            m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
            for m in messages
            if m["role"] == "user"
        )
        fmt = (payload.get("response_format") or {}).get("json_schema", {}).get("name")
        visitor = _visitor_message(user_text)
        if fmt == "sales_response":
            content = json.dumps(_sales_json(system_text, visitor))
        else:
            content = "Happy to help."
        _append(
            "llm-requests.jsonl",
            {
                "path": self.path,
                "model": payload.get("model"),
                "response_format_name": fmt,
                "visitor_message": visitor,
                "system_prompt": system_text,
                "returned": content,
            },
        )
        self._send(
            200,
            {
                "id": "chatcmpl-standin",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model", "standin-llm"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )


class HookHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode(errors="replace")
        _append(
            "hook-received.jsonl",
            {
                "path": self.path,
                "content_type": self.headers.get("content-type"),
                "body": body,
            },
        )
        if self.path.startswith("/crm-5xx"):
            self.send_response(503)
            self.end_headers()
            return
        reply: dict = {"ok": True}
        if self.path.startswith("/crm"):
            with _lock:
                _crm_counter["n"] += 1
                reply = {"id": f"crm-standin-{_crm_counter['n']}"}
        raw_out = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw_out)))
        self.end_headers()
        self.wfile.write(raw_out)


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-port", type=int, default=58090)
    ap.add_argument("--hook-port", type=int, default=58091)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    llm = ThreadingHTTPServer(("127.0.0.1", args.llm_port), LLMHandler)
    hook = ThreadingHTTPServer(("127.0.0.1", args.hook_port), HookHandler)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    print(f"standins up: llm={args.llm_port} hook={args.hook_port}", flush=True)
    llm.serve_forever()


if __name__ == "__main__":
    main()
