"""Command line: reprolog list | show | replay | curl | pytest | ui | clear"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import textwrap

from . import storage
from .config import CONFIG


_FRAMEWORK_MARKERS = ("site-packages", "dist-packages", "/reprolog/reprolog/", "<frozen ", "/lib/python3")


def short_traceback(tb: str) -> str:
    """Drop framework and library frames so your own code is what you see."""
    lines = (tb or "").rstrip().splitlines()
    out, skip_next = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('File "'):
            skip_next = any(m in stripped for m in _FRAMEWORK_MARKERS)
            if not skip_next:
                out.append(line)
            continue
        if skip_next and line.startswith("    "):
            continue
        skip_next = False
        out.append(line)
    return "\n".join(out)


def _fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _get(cid: str) -> dict:
    try:
        rec = storage.get(cid)
    except LookupError as err:
        sys.exit(str(err))
    if rec is None:
        sys.exit(f"No capture found for '{cid}'. Run `reprolog list` to see ids.")
    return rec


def cmd_list(args):
    rows = storage.list_captures(limit=args.limit)
    if not rows:
        print(f"No captures yet in {storage.db_path()}.")
        print("Trigger an error in your app; failed requests are saved automatically.")
        return
    print(f"{'ID':<13}{'TIME':<21}{'STATUS':<8}{'METHOD':<8}{'PATH':<38}ERROR")
    for r in rows:
        err = f"{r['error_type']}: {r['error_message']}" if r["error_type"] else ""
        print(f"{r['id']:<13}{_fmt_time(r['created_at']):<21}{str(r['status'] or '-'):<8}{(r['method'] or ''):<8}"
              f"{(r['path'] or '')[:37]:<38}{err[:70]}")


def _print_body(label: str, body: dict | None, limit: int = 1500):
    body = body or {}
    note = f" ({body['note']})" if body.get("note") else ""
    if body.get("encoding") == "empty":
        print(f"  {label}: (empty){note}")
        return
    data = body.get("data", "")
    try:
        data = json.dumps(json.loads(data), indent=2, ensure_ascii=False)
    except Exception:
        pass
    if len(data) > limit:
        data = data[:limit] + f"\n  ... ({len(data) - limit} more characters, see --json)"
    print(f"  {label}{' [secrets redacted]' if body.get('redacted') else ''}{note}:")
    print(textwrap.indent(data, "    "))


def cmd_show(args):
    rec = _get(args.id)
    if args.json:
        print(json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        return
    req, resp, err = rec["request"], rec["response"], rec.get("error")
    qs = f"?{req['query_string']}" if req.get("query_string") else ""
    print(f"Capture {rec['id']}  {_fmt_time(rec['created_at'])}  {rec.get('duration_ms')} ms")
    if rec.get("trace_id"):
        print(f"Trace id: {rec['trace_id']}  (search this in Logfire)")
    print(f"\nREQUEST  {req['method']} {req['path']}{qs}")
    for n, v in req["headers"]:
        print(f"  {n}: {v}")
    _print_body("body", req.get("body"))
    print(f"\nRESPONSE  {resp.get('status')}")
    _print_body("body", resp.get("body"), limit=600)
    if err:
        print(f"\nERROR  {err['type']}: {err['message']}  ({err.get('source')})")
        tb = err.get("traceback") or ""
        print(textwrap.indent(tb.rstrip() if args.full else short_traceback(tb), "  "))
    logs = rec.get("logs") or []
    print(f"\nLOGS  ({len(logs)})")
    for log in logs[-40:]:
        print(f"  {_fmt_time(log['time'])[11:]} {log['level']:<8} {log['logger']}: {log['message'][:200]}")
    outbound = rec.get("outbound") or []
    print(f"\nOUTBOUND CALLS  ({len(outbound)})")
    for o in outbound:
        tag = " [LLM]" if o.get("kind") == "llm" else ""
        usage = f"  tokens: {o['usage']}" if o.get("usage") else ""
        print(f"  {o['method']} {o['url'][:90]} -> {o['response'].get('status')} ({o['duration_ms']} ms){tag}{usage}")
        if o.get("error"):
            print(f"    error: {o['error']}")
    print(f"\nReplay it:  reprolog replay {rec['id']} --app main:app")


def _print_replay_result(result: dict):
    v, new = result["verdict"], result["replayed"]
    print(f"\n{v['summary']}")
    print(f"  status: original {v['original_status']} -> replay {v['replayed_status']}  ({new.get('duration_ms')} ms)")
    info = new.get("replay") or {}
    print(f"  outbound calls served from recording: {info.get('mocked_outbound', 0)} of {info.get('recorded_outbound', 0)}")
    for miss in info.get("unmatched_outbound") or []:
        print(f"  not in recording, sent live: {miss['method']} {miss['url'][:100]}")
    err = new.get("error")
    if err:
        print(f"\nERROR  {err['type']}: {err['message']}")
        if err.get("traceback"):
            print(textwrap.indent(short_traceback(err["traceback"]), "  "))
    logs = new.get("logs") or []
    if logs:
        print(f"\nLOGS DURING REPLAY ({len(logs)})")
        for log in logs[-25:]:
            print(f"  {log['level']:<8} {log['logger']}: {log['message'][:200]}")


def cmd_replay(args):
    from . import replay

    rec = _get(args.id)
    missing = [n for n in replay.redacted_headers_in(rec)
               if not any(o.split(":", 1)[0].strip().lower() == n.lower() for o in (args.header or []))]
    if missing:
        print(f"Note: these headers were redacted when captured and will be sent as '[REDACTED]': {', '.join(missing)}.")
        print("      Pass fresh values with -H 'Name: value' if the endpoint needs them.")
    if args.target:
        result = replay.replay_live(rec, args.target, header_overrides=args.header)
        print(f"Sent to {result['url']} -> {result['status']} ({result['duration_ms']} ms)")
        print(result["body"])
        return
    print(f"Replaying {rec['request']['method']} {rec['request']['path']} in-process using {args.app} ...")
    print("Outbound HTTP and LLM calls come from the recording.")
    print("WARNING: database writes, Redis, SMTP email and other non-HTTP calls run for real. Use a local database.")
    os.environ["REPROLOG_REPLAY"] = "1"
    os.environ["REPROLOG_ENABLED"] = "1"  # so an opt-in `if REPROLOG_ENABLED` hook in the app installs the middleware
    CONFIG.enabled = True
    result = replay.run(rec, args.app, header_overrides=args.header, live_outbound=args.live_outbound,
                        strict=args.strict, full_stack=args.full_stack, run_lifespan=not args.no_lifespan)
    new = result["replayed"]
    if args.save:
        import time as _time
        import uuid as _uuid

        new["id"] = new.get("id") or _uuid.uuid4().hex[:12]
        new["replay_of"] = rec["id"]
        new["created_at"] = new.get("created_at") or _time.time()
        new.setdefault("spans", [])
        storage.save(new)
    payload = {"verdict": result["verdict"], "replayed": new, "saved_id": new.get("id") if args.save else None}
    if args.out_json:
        with open(args.out_json, "w") as fh:
            fh.write(replay.dumps(payload))
    if args.json:
        print(replay.dumps(payload))
    else:
        _print_replay_result(result)
        if args.save:
            print(f"\nSaved replay as {new['id']} (reprolog show {new['id']})")
    sys.exit(0 if result["verdict"]["reproduced"] else 1)


def cmd_curl(args):
    from .export import to_curl

    print(to_curl(_get(args.id), args.base_url))


def cmd_pytest(args):
    from .export import to_pytest

    test_file, fixture = to_pytest(_get(args.id), args.app, args.out)
    print(f"Wrote {test_file}\n  and {fixture}")
    print("The fixture holds the captured request data. Review it before moving it anywhere tracked by git.")
    print(f"Run it: python -m pytest {test_file}")


def cmd_ui(args):
    from .ui import serve

    serve(args.port, app_path=args.app, db=storage.db_path(), open_browser=not args.no_browser)


def cmd_clear(args):
    if not args.yes:
        sys.exit("This deletes every capture. Run again with --yes to confirm.")
    print(f"Deleted {storage.clear()} captures.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="reprolog", description="See failed requests and replay them locally.")
    parser.add_argument("--db", help="SQLite file (default .reprolog/reprolog.db or $REPROLOG_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list captured requests, newest first")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show request, response, error, logs and outbound calls")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--full", action="store_true", help="show the full traceback including library frames")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("replay", help="re-run a captured request to reproduce the error")
    p.add_argument("id")
    p.add_argument("--app", default="main:app", help="import path of your ASGI app (default main:app)")
    p.add_argument("-H", "--header", action="append", help="override a header, e.g. -H 'Authorization: Bearer xyz'")
    p.add_argument("--live-outbound", action="store_true", help="make outbound HTTP/LLM calls for real")
    p.add_argument("--strict", action="store_true", help="fail if the app makes an outbound call not in the recording")
    p.add_argument("--full-stack", action="store_true", help="send through all middleware (body must match what outer middleware expects)")
    p.add_argument("--no-lifespan", action="store_true", help="skip the app's startup/shutdown events")
    p.add_argument("--target", help="send to a running server instead, e.g. http://localhost:8000")
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true", help="store the replay as a new capture linked to the original")
    p.add_argument("--out-json", help="write the result as JSON to this file")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("curl", help="print a curl command for the captured request")
    p.add_argument("id")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.set_defaults(func=cmd_curl)

    p = sub.add_parser("pytest", help="write a regression test that replays the capture")
    p.add_argument("id")
    p.add_argument("--app", default="main:app")
    p.add_argument("--out", default=".reprolog/tests", help="folder for the test (default .reprolog/tests, ignored by git)")
    p.set_defaults(func=cmd_pytest)

    p = sub.add_parser("ui", help="open the live web viewer")
    p.add_argument("--port", type=int, default=7999)
    p.add_argument("--app", default="main:app", help="app import path used by the Replay button")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("clear", help="delete all captures")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_clear)

    args = parser.parse_args(argv)
    if args.db:
        CONFIG.db_path = args.db
    args.func(args)


if __name__ == "__main__":
    main()
