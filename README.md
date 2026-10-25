# reprolog

See every failed request in your FastAPI app: what was sent, what your code logged,
which outside APIs and LLMs it called, and the traceback. Then replay that exact
request on your laptop to reproduce the bug.

No dependencies. Data stays in a local SQLite file (`.reprolog/`, ignored by git).

## Add it to an app

Right after `app = FastAPI()`, before any other `app.add_middleware(...)`:

```python
if os.getenv("REPROLOG_ENABLED") == "1":   # opt-in, local only
    import reprolog
    reprolog.init(app)
```

It must be the innermost middleware so it sees the request the way your routes do
(after payload decryption, path stripping and similar outer layers).

Install into the app's environment, from the app folder:

```bash
uv pip install -e ~/Desktop/projects/personal/reprolog     # or: pip install -e ...
REPROLOG_ENABLED=1 uvicorn main:app --reload --port 8000
```

## What gets saved

A request is saved when it raises, returns status >= 500, or logs an exception
(`logging.exception` or `logfire.exception`). For each one:

- method, path, query, headers and body (secrets redacted)
- status and response body
- the error and traceback
- log lines written during the request (stdlib logging and Logfire)
- outbound HTTP calls made with `httpx` or `requests`, including OpenAI, Gemini,
  Groq and Anthropic SDK calls, with request, response and token usage

Settings (environment variables): `REPROLOG_MIN_STATUS=400` to also save 4xx,
`REPROLOG_CAPTURE_ALL=1` to save everything, `REPROLOG_KEEP_AUTH=1` to keep auth
headers locally so replays authenticate, `REPROLOG_DB=path` to move the database.

## Live viewer

```bash
REPROLOG_ENABLED=1 REPROLOG_CAPTURE_ALL=1 uvicorn main:app --reload --port 8000
reprolog ui --app main:app          # http://127.0.0.1:7999
```

- New requests appear as they happen (`REPROLOG_CAPTURE_ALL=1` shows every request,
  without it only failures).
- Each request opens as a span waterfall: database, Redis and HTTP spans from
  OpenTelemetry/Logfire, plus every outbound call. Small ticks on a bar mark log lines.
- Click a span to see the log lines written inside it, its attributes, exceptions,
  and for outbound calls the full request and response (with LLM token usage).
- **Replay** re-runs the request in a separate process with outbound calls served
  from the recording, shows whether the error came back, and saves the replay as a
  new capture so you can open its spans next to the original.

Only the last 5,000 captures are kept (`REPROLOG_MAX_CAPTURES`).

## Commands

Run from the app folder:

```bash
reprolog list                       # newest failures first
reprolog show <id>                  # request, error, logs, outbound calls
reprolog ui --app main:app          # live viewer at http://127.0.0.1:7999
reprolog replay <id> --app main:app # reproduce it in-process
reprolog replay <id> -H "Authorization: Bearer <fresh>"
reprolog replay <id> --live-outbound   # call outside APIs for real
reprolog curl <id>                  # curl command
reprolog pytest <id> --app main:app    # regression test in .reprolog/tests
```

## How replay works

`replay` imports your app and sends the captured request to the layer where
reprolog sits, so outer middleware (payload encryption, origin checks, rate limits)
is skipped and the decrypted body goes straight to your routes. Outbound HTTP and
LLM calls are answered from the recording, so the replay sees the same data that
caused the bug, costs no tokens, and is repeatable.

Not mocked: database, Redis, SMTP email, file system. Replays run those for real,
so point the app at a local database. Bugs that depend on database state may not
reproduce if your local data differs.

`--target http://localhost:8000` sends the request to a running server instead.
That goes through all middleware, so endpoints that expect an encrypted body will
reject the decrypted body that reprolog stored.
