# A settings page: configure models, switches, notifications and traders in the browser

## Why

Configuring the system meant editing `.env` and `traders/*.yaml` by hand, and
the one question that matters — *does this model actually answer?* — could
only be answered by starting the scheduler and reading its log. The failure
this project keeps having is a pipeline that runs with an error string for
content (`Unsupported model`), so the page's most important control is a
**test button that shows the model's reply text**.

## Decisions (user, 2026-09-24)

- No access protection on the settings API (the rest of the web API has none
  either). Secrets are still write-only: the API returns `sk-…a3f2`, never
  the value. That is not access control — it keeps keys out of screenshots
  and page source.
- `.env` stays the single source of truth. The page rewrites it in place,
  keeping comments and order, with a rolling backup `.env.bak-settings`.
- Scope of v1: model roles with a live test; trading switches; notification
  webhooks with a test send; data sources and timeouts; trader YAML editing;
  `MEMORY.md` viewing, editing and version diff; first-run guidance when no
  decision model is configured.

## Constraints

- Settings are read at process start (`config` freezes at import). The page
  says so after every save, and `/api/settings` reports `changed_since_boot`.
- A trader file that `trader._coerce` rejects is not written.
- A trader id must match `^[a-z][a-z0-9_]{0,31}$` and is never `default`.
- No trader delete from the page: a trader with open positions must be
  wound down by setting `enabled: false`, which the loader already handles.

## Acceptance

1. `env_file.update_env` replaces an existing key in place, removes a key
   given `None`, appends a new key, keeps every comment line byte-for-byte,
   and leaves the file mode unchanged (tests).
2. `GET /api/settings` never contains a secret's value (test with a known key).
3. A placeholder key from `.env.example` (`sk-xxx`) counts as not configured
   (test).
4. The LLM test endpoint returns the model's reply text on success and the
   provider's error text on failure (tests with a stubbed client).
5. Saving a trader with a malformed field is refused with 400 and the file is
   unchanged (test).
6. pytest, lint_harness, lint_docs pass; `npm run build` succeeds; the page
   renders in a browser with a real `.env`.

## Decision log

- 2026-09-24: `ALPHAAGENTS_ENV_FILE` names the file both processes read and
  the page writes. Docker's `env_file` leaves no file in the container, so
  without it a save would land in a file nobody reads.
- 2026-09-24: the LLM test allows 1024 tokens. With 40, the configured
  reasoning model spent 54 on thinking and returned empty content.

- 2026-09-24: routes in `server/settings_api.py` (an `APIRouter`), not in
  `app.py`, to keep `app.py` well under the 1200-line limit.
