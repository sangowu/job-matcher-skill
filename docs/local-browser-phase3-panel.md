# Local-browser Phase 3: settings and attention panel

Phase 3 adds an optional localhost control plane for the current job-search
run. It is portable infrastructure, not a BrowserOS-specific client: the Agent
continues to call whichever browser tools its runtime exposes.

## Start and stop

Run `python scripts/local_browser_panel.py serve`. The server binds only to an
ephemeral `127.0.0.1` port, opens the panel, and stays alive for the current
search. Stop the server when the run ends. If the runtime cannot keep a local
server alive, continue discovery and deliver the same action-required warning
in chat; the panel is an enhancement, not a search dependency.

The panel stores only these non-sensitive settings in
`data/local_browser_settings.json`:

- `discovery_mode`: `coverage`, `auto`, `model_only`, `browser_only`, or
  `combined`; `coverage` is the default and runs the preferred browser provider
  alongside model search when both are ready;
- `cookie_consent_policy`: `necessary_only` (default) or `ask_every_time`;
- `flash_attention`: whether the panel beacon and browser-tab title flash.

Read the effective values with `python scripts/local_browser_panel.py settings`
before calling `discovery_mode.py plan`.

## Event bridge

The Agent publishes low-cardinality status after it recognizes browser state:

```text
python scripts/local_browser_panel.py event --state detecting --provider browseros_neo
python scripts/local_browser_panel.py event --state running --provider browseros_neo
python scripts/local_browser_panel.py event --state needs_user_action --provider browseros_neo --issue captcha_required
python scripts/local_browser_panel.py event --state rate_limited --provider user_browser
python scripts/local_browser_panel.py event --state completed --provider browseros_neo
```

Allowed issues are `none`, `login_required`, `captcha_required`,
`consent_required`, `rate_limited`, and `connection_lost`. Never put a URL,
query, company, account, tab/session ID, CV/JD text, or free-form message into a
panel event.

The page polls `/api/status`. For `needs_user_action` and `rate_limited`, its
beacon and tab title flash and it exposes an “I’m done · resume” button. That
button changes the state to `resume_requested`; it does not directly control
the browser. The Agent reads `python scripts/local_browser_panel.py status`,
confirms `resume_requested`, performs the next safe browser observation, and
then publishes `running` or another bounded event.

## Security boundary

- Loopback-only HTTP server with an ephemeral port.
- CSRF token on every mutation.
- Strict CSP, no external assets, `no-store`, framing denied, referrer disabled.
- Atomic local JSON writes under the ignored `data/` directory.
- No cookies, credentials, browser profiles, URLs, page content, or account
  identifiers enter the panel.
- The panel cannot submit applications, solve challenges, click consent, or
  resume a browser by itself. It stores the selected consent policy only; the
  Agent applies the fail-closed semantic contract in
  [`cookie-consent.md`](cookie-consent.md).

## Verification boundary

Unit tests cover settings/status validation, loopback binding, CSRF, resume
requests, and low-cardinality persistence. Browser UI validation should verify
the rendered state transition and title/beacon alert with a local headless
browser. A real BrowserOS Neo smoke remains separate and must not be inferred
from the panel tests.

The first bounded fallback smoke is recorded in
[`local-browser-phase4-smoke.md`](local-browser-phase4-smoke.md): an authorized
user-browser route passed dedicated-tab create/navigate/read/close, while Neo
remained unavailable and therefore unverified.
