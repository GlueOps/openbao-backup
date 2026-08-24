# OpenBao KV v2 dump / restore

Exports and imports **all KV v2 secrets** of an OpenBao instance. Everything
runs inside a docker image (`openbao/openbao` + python3) — the only local
dependency is the docker CLI. The `bao` CLI inside the container makes all
API calls; the oauth2-proxy cookie is injected on every request via
`bao -header="Cookie=..."`.

## Build

```bash
docker build -t baokv .
```

Rebuild after any change to `baokv.py` or the `Dockerfile`.

## Configuration (env vars)

| Variable     | Value                                                                  |
|--------------|------------------------------------------------------------------------|
| `BAO_ADDR`   | Server URL, e.g. `https://foobar.example.com`                          |
| `BAO_TOKEN`  | Your OpenBao token (e.g. from the OIDC login)                          |
| `BAO_COOKIE` | The `_oauth2_proxy` cookie value from your browser dev tools, with or without the `_oauth2_proxy=` prefix |

Pass them with `-e` flags, or copy `baokv.env.example` to `baokv.env`
(git-ignored), fill it in, and use `--env-file baokv.env`. The tool exits
with a clear error if any variable is missing, and tells you when the cookie
has expired (the proxy answers with an OAuth redirect).

```bash
export BAO_ADDR=https://foobar.example.com
export BAO_TOKEN=s.XXXXXXXXXXXXXXXXXXXXXXXX
export BAO_COOKIE='PASTE_COOKIE_VALUE_HERE'
```

## Run commands

Every command mounts the current directory at `/work` (where dump files are
read/written) and runs as your uid so files on the host belong to you.

```bash
# Audit: every secret path, version, and key names — no values printed
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  baokv list

# Export everything to a file (file is created with mode 600)
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  baokv dump -o secrets-export-$(date +%F).json

# Preview a restore without changing anything
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  baokv restore -i secrets-export-2026-08-24.json --dry-run

# DESTRUCTIVE restore: makes the server exactly match the file
#  - every secret in the file is imported (old version history is wiped first)
#  - every secret on the server that is NOT in the file is permanently deleted
#    (kv metadata delete = all versions gone)
# -it is required so you can type "yes" at the confirmation prompt
docker run --rm -it -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  baokv restore -i secrets-export-2026-08-24.json

# Same, without the prompt (for scripts/cron)
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  baokv restore -i secrets-export-2026-08-24.json --yes
```

Using an env file instead of exported variables: replace the three `-e` flags
with `--env-file baokv.env`.

Restore always audits the current server state first and prints the full plan
(creates / overwrites / deletes) before touching anything.

## Tests

```bash
test/run-tests.sh
```

Hermetic e2e regression suite — needs only the docker CLI. It builds the
image, starts a disposable OpenBao dev server in a private docker network,
runs 89 checks against it, and tears everything down. Coverage: CLI/config
guards, kv-v2 mount discovery (v1 and system mounts excluded), nesting
depths 1–10, special-character paths (spaces, unicode/emoji, quotes, shell
metacharacters, `% # ?`, leading dashes, backslashes, dot-segments,
300-char segments, reserved-looking names like `data`/`metadata`, case
sensitivity, leaf-and-folder same-name, five stacked leaves on one chain
at different depths with different key counts), value edge cases
(multiline, 100KB and 1MB, empty keys/values/data, 12-level nested JSON,
non-string types, `"42"` vs `42` typing, unicode key names, control
characters), custom metadata round-trip, soft-deleted/destroyed version
skipping, restore semantics (create/overwrite/delete, dry-run,
confirmation prompt, history wipe, idempotence, missing-mount abort),
`cas_required` mounts, dotted and nested mount names, 147-secret volume,
limited-token partial dumps, dump file properties, and full
wipe-and-restore round trips. Your real server is never touched.

## Notes / limitations

- Only KV v2 mounts are handled (all of them are auto-discovered via
  `sys/internal/ui/mounts`; the oauth proxy blocks `/v1/sys/mounts`).
- The dump stores the **current version** of each secret plus its
  `custom_metadata`. Older versions are not exported, and restore resets each
  secret's history to version 1.
- Secrets whose current version is (soft-)deleted are skipped with a warning
  during dump — and will therefore be **removed** by a later restore.
- The same applies to secrets your token cannot read or list: dump warns
  about unreadable secrets and un-listable subtrees, and both would be
  **deleted** if you restored from that partial dump. Always dump with a
  token that can read and list everything you intend to keep.
- Numbers at or above 2^53 (e.g. int64 max) are rounded to float64 precision
  by OpenBao itself — even when written through the raw HTTP API, the exact
  value is never observable again, so dump/restore cannot make it worse.
  Dump prints a warning when it sees such numbers; store huge numbers as
  strings if you need them exact.
- If the file references a KV v2 mount that doesn't exist on the server,
  restore aborts before making changes (mount creation is blocked by the
  proxy — create the mount manually first).
- The dump file contains all secret values in plaintext. Treat it like a
  credential. Dump/restore filenames are required to end in `.json` so they
  always stay covered by the `*.json` rule in `.gitignore`. Note that env vars are
  visible in `docker inspect` on a running container and in your shell
  history if set inline — prefer `--env-file` with a mode-600 `baokv.env`.
