# Reused UI

`ultrafast.css` is copied from `jev-ultrafast/jev_ultrafast/static/style.css`.
The browser-panel / sidebar / execution-trail markup, DOM helpers, escaping,
rendering pattern and loopback HTTP-server protection are adapted from its
`index.html`, `app.js` and `demo.py`.

Upstream: https://github.com/browser-use/jev-ultrafast
Copyright (c) 2026 Browser Use. MIT license in `ULTRAFAST-LICENSE.txt`.

LongSeq-specific additions: run discovery, typed trajectory inspection, archived
Playwright screencast playback, continuous opt-in preview, verified result metrics
and an offline rule-demo launcher. The optional original Ultrafast view imports
the local source project in an isolated worker without modifying it. Its DOM
overlays and native decision ranking in `original.js` reuse the upstream
rendering pattern and existing `ultrafast.css`, adding recorded decision context
and linked element highlighting. No account credentials are copied into the UI.
