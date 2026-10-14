# Cookie-consent policy

The local-browser route defaults to `necessary_only`. This means reject
optional cookies or accept strictly necessary cookies only. It never means
accept optional, analytics, advertising, personalization, or all cookies.

## Semantic decision contract

Use the current accessibility snapshot and isolate one `dialog` or
`alertdialog` whose bounded name/text clearly identifies Cookie or tracking
consent. Send only that container summary and its semantic controls to
`scripts/cookie_consent.py`.

The classifier uses exact normalized action labels for the initial market
languages. Examples include `Just Necessary`, `Accept Strictly Necessary`,
`Reject Cookies`,
`Nur notwendige Cookies akzeptieren`, and `拒绝非必要 Cookie`. Labels are
generic language mappings, not site-specific selectors. Simplified and
Traditional Chinese are separate exact-label sets; for example, `拒絕全部` is
accepted only inside a bounded Cookie-consent dialog.

Every language in `references/languages.json` has its labels: French
(`Tout refuser`), Spanish (`Rechazar todo`), Portuguese (`Rejeitar todos`),
Italian (`Rifiuta tutti`), Dutch (`Alles weigeren`), Polish (`Odrzuć
wszystkie`), Japanese (`すべて拒否`) and Korean (`모두 거부`), each with its
necessary-only and continue-without-accepting forms. Until 2026-10-14 the set
stopped at English, German and Chinese, and the browser channel paused on the
banner of every local job board in the markets added after them; a live round
paused on Talent.com Italy's `Rifiuta tutti`. A label is only ever a refusal or
a necessary-only choice -- no accept form in any language is listed.

The dialog itself is recognized by a Cookie or tracking word in its name or
text, in each language as well: Talent.com's Italian banner never says
"cookie", only "tecnologie di tracciamento". A banner offering nothing but
acceptance, such as Jobs in Milan's `Accept cookies` / `More information`,
still pauses.

The result is fail-closed:

- one safe button: `auto_select_necessary_only` with one `target_ref`;
- zero or multiple safe buttons: `pause`;
- `ask_every_time`: always `pause`;
- a non-dialog or non-Cookie context: `pause`.

Click the returned ref only while it belongs to the same current snapshot. If
the page re-renders, take a new snapshot and classify again. After the click,
verify only that the dialog is gone and navigation remains within the task's
allowed host policy. Do not inspect Cookie storage to prove the result.

Never click `Accept All`, infer an unlabeled control, toggle consent categories,
solve a challenge, or continue when the meaning is ambiguous. Publish
`needs_user_action/consent_required` and preserve the dedicated tab instead.

## Classifier input

```json
{
  "policy": "necessary_only",
  "container": {
    "role": "dialog",
    "name": "Cookie consent dialog",
    "text": "We use cookies"
  },
  "controls": [
    {"ref": "e1", "role": "button", "name": "Accept All"},
    {"ref": "e2", "role": "button", "name": "Just Necessary"}
  ]
}
```

The CLI output contains decision metadata and, when safe, the current semantic
ref. It stores nothing and performs no browser action.

## Live smoke evidence

The 2026-09-21 BrowserOS Neo smoke observed two job-search sites in the same
persistent browser profile. One site was already non-blocking because the
profile retained an earlier choice. On the other site, the classifier selected
the one exact `ACCEPT STRICTLY NECESSARY` button; a fresh snapshot supplied the
action ref, and the dialog was absent after the click. The run made zero
`Accept All` clicks, zero category-toggle clicks, and zero Cookie-store reads.

Only aggregate counters are retained in
`docs/performance/cookie-consent-necessary-only-smoke-2026-09-21.json`; no URL,
page text, Cookie value, account detail, or candidate content is stored.

A later three-wave discovery run exercised additional Traditional-Chinese and
generic English exact labels. Three unambiguous dialogs completed under
`necessary_only`; one ambiguous control paused and was skipped rather than
guessed. The run again made zero accept-all clicks, zero category-toggle clicks,
and zero Cookie-store reads. Its count-only evidence is
`docs/performance/discovery-wave-complete-smoke-2026-09-21.json`.
