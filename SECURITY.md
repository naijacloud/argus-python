# Security Policy

## Reporting a vulnerability

Email **security@naijacloud.com**. Don't open a public issue, discussion or
pull request for a security problem.

Include the version, the Python version, a minimal snippet and what you
expected instead. We acknowledge reports within two working days (Nigeria,
WAT), tell you our assessment and a fix timeline, and credit you in the
changelog unless you would rather we didn't.

## Supported versions

The latest minor release receives security fixes. Before 1.0.0 that is the
latest release.

## What this SDK guarantees

These are enforced in code and covered by tests. A regression in any of them is
a security bug worth reporting:

- **The DSN's public key is the only credential the SDK holds.** It can send
  data to one Argus source and read nothing.
- **Personal data is filtered before anything leaves your process.**
  - The values of keys such as password, token, secret, authorization,
    cookie, bvn and card are replaced with `[Filtered]`, at any depth.
  - Card numbers, bearer tokens, JWTs and `password=…` pairs are filtered
    inside strings, including exception messages, source lines and logs.
  - Query strings are removed from URLs in breadcrumbs, requests and spans.
  - Request bodies and cookies are never sent.
  - The user's IP address is not sent unless you set `send_default_pii=True`.
- **No redirects are followed,** so the key is never replayed to a host a
  response names.
- **The SDK never raises into your code,** and a network failure never blocks
  it: every send happens on a background thread.
- **Zero runtime dependencies,** so installing this package adds exactly one
  supply chain: ours.

## What it cannot do for you

- Filter a secret under a key name or in a format it doesn't know. Use
  `before_send` for your own.
- Keep personal data out of an event if you put it there under an innocent
  name, such as `extra={"note": user.address}`.
