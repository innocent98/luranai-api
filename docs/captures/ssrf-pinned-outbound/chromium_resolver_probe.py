"""Proves the Chromium launch rule used by app/auditor/crawler.py.

Run: python chromium_resolver_probe.py [path-to-chrome-headless-shell]
Starts a "root" server and a canary server on 127.0.0.1 (both listening), then
navigates with `--host-resolver-rules=MAP root.test 127.0.0.1, MAP * ~NOTFOUND`.
Each case prints the navigation result and how many times the canary was hit.
A control case first maps canary.test too, to show the canary IS reachable when
Chromium can resolve it (so "0 hits" in the other cases is meaningful).
"""
import asyncio, http.server, socketserver, sys, threading
from playwright.async_api import async_playwright

ROOT, CANARY = 8099, 8098
canary_hits = []


class Root(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        targets = {"/to-ip": f"http://127.0.0.1:{CANARY}/canary", "/to-name": f"http://canary.test:{CANARY}/canary"}
        if self.path in targets:
            self.send_response(302); self.send_header("Location", targets[self.path]); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html>root</html>")


class Canary(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        canary_hits.append(self.path)
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html>CANARY</html>")


def serve(port, handler):
    s = socketserver.TCPServer(("127.0.0.1", port), handler)
    s.allow_reuse_address = True
    threading.Thread(target=s.serve_forever, daemon=True).start()


async def run_case(p, exe, name, rule, path, ws=False):
    canary_hits.clear()
    b = await p.chromium.launch(headless=True, args=[f"--host-resolver-rules={rule}"], **({"executable_path": exe} if exe else {}))
    page = await b.new_page()
    result = ""
    try:
        if ws:
            await page.goto(f"http://root.test:{ROOT}/ok", timeout=8000)
            result = await page.evaluate(
                """()=>new Promise(r=>{try{const w=new WebSocket('ws://127.0.0.1:%d/ws');
                w.onerror=()=>r('websocket error');w.onopen=()=>r('websocket OPEN');setTimeout(()=>r('websocket timeout'),3000)}catch(e){r('throw')}})""" % CANARY)
        else:
            r = await page.goto(f"http://root.test:{ROOT}{path}", timeout=8000)
            result = f"HTTP {r.status} landed on {page.url}"
    except Exception as e:
        result = "navigation failed: " + str(e).split("\n")[0][:70]
    await b.close()
    print(f"{name:34s} | {result:90s} | canary hits: {len(canary_hits)}")


async def main(exe):
    serve(ROOT, Root); serve(CANARY, Canary)
    prod = f"MAP root.test 127.0.0.1, MAP * ~NOTFOUND"
    control = f"MAP root.test 127.0.0.1, MAP canary.test 127.0.0.1, MAP * ~NOTFOUND"
    async with async_playwright() as p:
        await run_case(p, exe, "CONTROL root 302 -> mapped name", control, "/to-name")
        await run_case(p, exe, "root 302 -> IP literal (canary)", prod, "/to-ip")
        await run_case(p, exe, "root 302 -> other name (canary)", prod, "/to-name")
        await run_case(p, exe, "WebSocket to IP literal (canary)", prod, "/ok", ws=True)

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
