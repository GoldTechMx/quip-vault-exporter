# Contributing

Thanks for helping improve Quip Vault Exporter.

## Development setup

```bash
python -m pip install -e ".[dev]"   # Python 3.11+
```

## Before opening a PR

The CI gate must pass locally:

```bash
ruff check .          # lint
ruff format --check . # formatting
mypy src              # types (strict)
pytest                # tests (incl. the in-process CLI e2e)
```

## Guidelines

- **Keep the tool read-only.** Never add a code path that creates, modifies, moves, or
  deletes Quip content. Network calls go through `quip_client.py`'s allowlist.
- **Never log or persist tokens.** If you touch logging or anything that writes API
  responses to disk, route it through the redaction helpers in `logging_setup.py` and add a
  test asserting the token does not leak.
- **Respect the rate limits.** New API calls must go through the shared client (and thus the
  adaptive rate limiter). Don't bypass it.
- **Validate against a live tenant** any change to the async-export or Admin-API field
  shapes - these vary by tenant and are centralized in `quip_client.py` / `org.py`.
- Add tests for new behavior; prefer the in-process mock server (`tests/test_cli_e2e.py`)
  for end-to-end coverage and `pytest-httpx` for client-level tests.
- Update `CHANGELOG.md` under "Unreleased".

## Releasing

Tag a version (`vX.Y.Z`) on `main`; the release workflow builds and publishes the wheel and
the Docker image. Regenerate `requirements.lock` if runtime dependencies changed.
