# Reused UI

`ultrafast.css` is copied from `jev-ultrafast/jev_ultrafast/static/style.css`.
The browser-panel / sidebar / execution-trail markup, DOM helpers, escaping,
rendering pattern and loopback HTTP-server protection are adapted from its
`index.html`, `app.js` and `demo.py`.

Upstream: https://github.com/browser-use/jev-ultrafast
Copyright (c) 2026 Browser Use. MIT license in `ULTRAFAST-LICENSE.txt`.

LongSeq-specific additions: run discovery, typed trajectory inspection, archived
Playwright screencast playback, continuous opt-in preview, verified result metrics
and an offline rule-demo launcher. The source project is not modified or imported
at runtime. No account credentials are copied into the UI.
