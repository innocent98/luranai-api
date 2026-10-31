#!/usr/bin/env python
"""A tiny fake of the two Paystack lookup endpoints, for running the bank picker locally.

NOT Paystack: the shapes follow Paystack's public docs, so a capture taken against this server
proves Luran's code and wire contract, never Paystack's real behaviour. Point the app at it with
``PAYSTACK_BASE_URL=http://127.0.0.1:18790``.

* ``GET /bank``: two cursor pages (``meta.next``), including inactive / deleted / mobile-money
  rows that the app must drop. Answers 503 while the mode is ``down``.
* ``GET /bank/resolve``: the account number picks the answer (all placeholders):
  ``0000000000`` ok, ``0000000001`` unresolvable (422), ``0000000002`` Paystack error (500),
  ``0000000003`` throttled (429), anything else ok.
* ``POST /__mode`` with ``{"mode": "up" | "down"}`` flips the bank list.

It never prints request URLs or headers.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def bank(id_, name, code, **over):
    row = {
        "id": id_,
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "code": code,
        "longcode": f"{code}150010",
        "gateway": None,
        "pay_with_bank": False,
        "supports_transfer": True,
        "active": True,
        "country": "Nigeria",
        "currency": "NGN",
        "type": "nuban",
        "is_deleted": False,
    }
    row.update(over)
    return row


PAGES = [
    [
        bank(1, "Zenith Bank", "057"),
        bank(2, "Access Bank", "044"),
        bank(3, "Old Placeholder Bank", "999", active=False),
        bank(4, "Guaranty Trust Bank", "058"),
    ],
    [
        bank(5, "First Bank of Nigeria", "011"),
        bank(6, "Wallet Placeholder", "998", type="mobile_money"),
        bank(7, "Gone Placeholder Bank", "997", is_deleted=True),
        bank(8, "Kuda Microfinance Bank", "50211"),
    ],
]
STATE = {"mode": "up"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # no access log: request URLs carry account numbers
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802 (http.server hook name)
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if urlsplit(self.path).path == "/__mode":
            STATE["mode"] = body["mode"]
            return self._send(200, STATE)
        self._send(404, {"status": False, "message": "not found"})

    def do_GET(self):  # noqa: N802 (http.server hook name)
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not (self.headers.get("authorization") or "").startswith("Bearer sk_"):
            return self._send(401, {"status": False, "message": "Invalid key"})
        if url.path == "/bank":
            if STATE["mode"] == "down":
                return self._send(503, {"status": False, "message": "Service unavailable"})
            index = int(query["next"]) if query.get("next") else 0
            nxt = str(index + 1) if index + 1 < len(PAGES) else None
            return self._send(
                200,
                {
                    "status": True,
                    "message": "Banks retrieved",
                    "data": PAGES[index],
                    "meta": {"next": nxt, "previous": None, "perPage": 100},
                },
            )
        if url.path == "/bank/resolve":
            number = query.get("account_number", "")
            if number == "0000000001":
                return self._send(
                    422,
                    {
                        "status": False,
                        "message": "Could not resolve account name. Check parameters or try again.",
                    },
                )
            if number == "0000000002":
                return self._send(500, {"status": False, "message": "Internal error"})
            if number == "0000000003":
                return self._send(429, {"status": False, "message": "Too many requests"})
            return self._send(
                200,
                {
                    "status": True,
                    "message": "Account number resolved",
                    "data": {
                        "account_number": number,
                        "account_name": "ADA PLACEHOLDER",
                        "bank_id": 9,
                    },
                },
            )
        self._send(404, {"status": False, "message": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18790
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
