import asyncio, base64, json, os, shutil, subprocess, sys
os.environ["REPROLOG_DB"] = os.path.abspath(".reprolog/test.db")
shutil.rmtree(".reprolog", ignore_errors=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

import demo_app
from reprolog import storage, replay
from reprolog.export import to_curl, to_pytest

async def call(app, method, path, body=b"", headers=()):
    msgs = []
    sent = False
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(10); return {"type": "http.disconnect"}
    async def send(m): msgs.append(m)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"authorization", b"Bearer user-token"), *headers],
             "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8000)}
    try:
        await app(scope, receive, send)
    except Exception as e:
        return 500, repr(e)
    start = next(m for m in msgs if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body").decode()

app = demo_app.app
enc = base64.b64encode(json.dumps({"sku": "BOLT-M8", "password": "hunter2"}).encode())

s, b = asyncio.run(call(app, "GET", "/ok")); assert s == 200, (s, b)
s, b = asyncio.run(call(app, "POST", "/api/order", enc)); print("order ->", s, b); assert s == 500
s, b = asyncio.run(call(app, "GET", "/handled")); assert s == 500

rows = storage.list_captures()
print("captures:", [(r["path"], r["status"], r["error_type"]) for r in rows])
assert len(rows) == 2, "only the two failures should be saved"
order = storage.get(next(r["id"] for r in rows if r["path"] == "/api/order"))
assert order["error"]["type"] == "IndexError"
body = json.loads(order["request"]["body"]["data"])
assert body == {"sku": "BOLT-M8", "password": "[REDACTED]"}, body
assert dict(order["request"]["headers"])["authorization"] == "[REDACTED]"
assert len(order["outbound"]) == 1 and order["outbound"][0]["kind"] == "http"
ob = order["outbound"][0]
assert dict(ob["request"]["headers"]).get("Authorization") == "[REDACTED]"
assert any("creating order" in l["message"] for l in order["logs"]), order["logs"]
handled = storage.get(next(r["id"] for r in rows if r["path"] == "/handled"))
assert handled["error"]["type"] == "ZeroDivisionError" and handled["error"]["source"] == "logged"
print("capture checks passed")

# The fake LLM is now "fixed". Mocked replay must still reproduce the bug from the recording.
os.environ["DEMO_LLM"] = "good"
res = asyncio.run(replay.replay_record(order, app, header_overrides=["Authorization: Bearer fresh"]))
print("mocked replay:", res["verdict"])
assert res["verdict"]["reproduced"] and res["replayed"]["replay"]["mocked_outbound"] == 1
assert storage.count() == 2, "replay must not save new captures"

res = asyncio.run(replay.replay_record(order, app, live_outbound=True))
print("live-outbound replay:", res["verdict"])
assert not res["verdict"]["reproduced"] and res["verdict"]["replayed_status"] == 200

curl = to_curl(order); print(curl); assert "curl -X POST" in curl and "BOLT-M8" in curl
t, f = to_pytest(order, "demo_app:app", ".reprolog/tests"); print("pytest file:", t)
os.environ["DEMO_LLM"] = "bad"
print("ALL CHECKS PASSED")

# ---- spans ----
os.environ["DEMO_LLM"] = "bad"
s, b = asyncio.run(call(app, "POST", "/api/checkout", base64.b64encode(b'{"cart": 1}')))
assert s == 500
cap = storage.get(storage.list_captures(limit=1)[0]["id"])
kinds = sorted(sp["kind"] for sp in cap["spans"])
print("span kinds:", kinds)
assert kinds == ["db", "http", "internal", "redis", "request"], kinds
by = {sp["name"]: sp for sp in cap["spans"]}
assert by["SELECT carts"]["parent_id"] == by["load cart"]["id"]
assert any(l["message"] == "cart loaded" and l["span_id"] == by["load cart"]["id"] for l in cap["logs"])
assert any(l["message"].startswith("price missing") and l["span_id"] == "root" for l in cap["logs"])
print("SPAN CHECKS PASSED", cap["id"])
