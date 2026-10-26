# Contributing

## Layout

```
src/naijacloud_argus/          the package
src/naijacloud_argus/integrations/   Django, Flask, FastAPI, WSGI, ASGI, logging
tests/                         unittest suite, runs against a local stand-in for ingest
tests/contract/run.py          the contract test against a real Argus
scripts/smoke.py               checks an installed wheel
examples/                      runnable scripts, DSN read from the environment
```

## Running the tests

The suite has no third-party dependencies and needs no network. It binds a real
`ThreadingHTTPServer` on `127.0.0.1:0` and speaks real HTTP to it, so the parts
most likely to break (headers, gzip, the retry loop, the hand-off to the
sending threads, the excepthooks in a real interpreter) are actually exercised.

```bash
python -m unittest discover -s tests -v
```

From a checkout, `tests/_support.py` puts `src/` on `sys.path`, so this works
without installing anything. Against an installed wheel, the installed package
wins.

The Flask, Django and FastAPI tests skip themselves unless those are
installed:

```bash
pip install -e ".[dev,frameworks]"
pytest
ruff check .
mypy
```

## The contract test

Against staging, with a test source's DSN (a public key, safe to share):

```bash
ARGUS_CONTRACT_DSN='https://<key>@argus-in.naijacloud-staging.com/<id>' \
  python tests/contract/run.py
```

It sends an error, a log, a span, a metric, a cron check-in and a flag
evaluation and checks each got a 2xx. The cron check-in needs a monitor with
slug `argus-python-contract`; without one it reports the 404 and passes. Set
`ARGUS_CONTRACT_API` and `ARGUS_CONTRACT_TOKEN` to also wait for the error to
appear as an issue.

## Supported Python versions

3.9 and newer, and CI runs all of them. That means:

- `from __future__ import annotations` at the top of every module.
- `typing.Optional` / `typing.Union`, never `X | Y` at runtime.
- No `match`, no `tomllib`, no `ExceptionGroup`.

## House rules

- **No runtime dependencies.** This package loads first in every process it
  watches. Dev and test tooling is unrestricted.
- **Never raise into the app.** Every public function and every hook catches
  its own errors. A user's exception inside `trace()` or `cron.monitor()`
  still propagates; only SDK failures are swallowed.
- **Never import a framework at module level.** An integration imports Django,
  Flask or Starlette inside the function that needs it.
- **Comments explain why.** A comment restating the code is noise.
- **The contract is the spec.** `nc-argus/spec/README.md` (`argus.sdk/1`)
  fixes the wire format, transport rules and privacy defaults for every Argus
  SDK. If this SDK and that document disagree, one of them is a bug.
- **Never commit a real key.** Test DSNs carry a run of zeros.

## Adding a feature

1. Check it against the contract. If the contract does not cover it, change the
   contract first: every language SDK has to end up with the same behaviour.
2. Write the test against the stand-in before the code.
3. Add a `CHANGELOG.md` entry under `Unreleased`.

## Releasing

Releases come from CI, on a tag. Nobody uploads from a laptop, and there is no
PyPI token anywhere: `.github/workflows/release.yml` publishes with PyPI
trusted publishing.

1. Bump `__version__` in `src/naijacloud_argus/_version.py` (the build, the
   envelope's `sdk.version` and the User-Agent all read it from there).
2. Move the `Unreleased` changelog entries under `## [x.y.z] - YYYY-MM-DD` and
   update the link definitions at the foot of the file.
3. Tag and push: `git tag v<version> && git push origin v<version>`.

The workflow runs the CI matrix, refuses a tag that disagrees with
`__version__` or has no changelog section, builds the sdist and wheel, uploads
them to PyPI, then creates the GitHub release from the changelog section.

### One-time setup

- **pypi.org → Your account → Publishing → Add a pending publisher** (before the
  first upload; afterwards it is under the project's Settings → Publishing):
  PyPI project `naijacloud-argus`, owner `naijacloud`, repository
  `argus-python`, workflow `release.yml`, environment `pypi`.
- **GitHub → Settings → Environments → New environment `pypi`.** Add required
  reviewers to make an upload wait for a human.
