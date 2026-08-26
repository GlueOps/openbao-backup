# OpenBao KV v2 dump / restore

Exports and imports **all KV v2 secrets** of an OpenBao instance. Everything
runs inside a docker image (`openbao/openbao` + python3) — the only local
dependency is the docker CLI. The `bao` CLI inside the container makes all
API calls; the oauth2-proxy cookie is injected on every request via
`bao -header="Cookie=..."`.

## Get the image

Every release publishes the image to GHCR. Pull the current release (the
version below is rewritten automatically by release-please on every release):

```bash
docker pull ghcr.io/glueops/openbao-backup:v0.2.0 # x-release-please-version
```

For local development, build with `docker build -t baokv .` and use `baokv`
in place of the image reference below (the test suites always build locally).

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
# Latest released image (this line is kept current by release-please)
IMAGE=ghcr.io/glueops/openbao-backup:v0.2.0 # x-release-please-version

# Audit: every secret path, version, and key names — no values printed
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  "$IMAGE" list

# Export everything to a file (mode 600; refuses to overwrite an existing path)
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  "$IMAGE" dump -o secrets-export-$(date +%F-%H%M%S).json

# Preview a restore without changing anything
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  "$IMAGE" restore -i secrets-export-2026-08-24-213045.json --dry-run

# DESTRUCTIVE restore: makes the server exactly match the file
#  - every secret in the file is imported (old version history is wiped first)
#  - every secret on the server that is NOT in the file is permanently deleted
#    (kv metadata delete = all versions gone)
# -it is required so you can type "yes" at the confirmation prompt
docker run --rm -it -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  "$IMAGE" restore -i secrets-export-2026-08-24-213045.json

# Same, without the prompt (for scripts/cron)
docker run --rm -e BAO_ADDR -e BAO_TOKEN -e BAO_COOKIE \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" \
  "$IMAGE" restore -i secrets-export-2026-08-24-213045.json --yes
```

Using an env file instead of exported variables: replace the three `-e` flags
with `--env-file baokv.env`.

Restore always audits the current server state first and prints the full plan
(creates / overwrites / deletes) before touching anything. It refuses to
act on a dump it cannot trust to be a full picture, and each refusal has an
explicit opt-out:

| Restore refuses when | Why | Opt out with |
|----------------------|-----|--------------|
| the recorded `address` is not the server you are pointing at | a staging dump applied to production would wipe it to match | `--allow-address-mismatch` |
| a kv-v2 mount exists on the server but the file never mentions it | mount discovery is filtered by the *dumping* token's policy, so an absent mount may just be one that token could not see — and every secret in it would be permanently deleted | `--allow-mount-deletion` |
| the file is marked `"complete": false` | something could not be read when the dump was made, and restoring would delete exactly those secrets | `--allow-incomplete` |
| the token cannot carry out every operation in the plan | the restore would stop part-way through | — (use a token that can) |

Writes run before deletions, so an interrupted restore — an expired cookie
part-way through a long run is the usual cause — leaves extra secrets behind
rather than missing ones. Everything the write phase touches is in the file, so
re-running the same restore recovers it; deletions remove data that is *not* in
the file and nothing can bring it back, which is why they go last.

## Tests

```bash
test/run-tests.sh
```

Hermetic e2e regression suite — needs only the docker CLI. It builds the
image, starts a disposable OpenBao dev server in a private docker network,
runs 95 checks against it, and tears everything down. Coverage: CLI/config
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

Run the same suite against a different server version with
`SERVER_IMAGE=openbao/openbao:2.6.1 test/run-tests.sh`.

## Cross-version baseline

```bash
test/cross-version.sh                    # default matrix: 2.4.4 2.5.5 2.6.1
VERSIONS="2.4.4 2.6.1" test/cross-version.sh
```

Guards against server upgrades changing dump/restore behavior. A checked-in
golden fixture (`test/fixtures/golden.json`, fake data covering nesting,
unicode paths, custom metadata, and type edge cases) is restored into a
fresh dev server of each version and dumped back; every dump must be
content-identical to the golden (paths, data, custom_metadata — timestamps
legitimately differ). Each version restores the dump produced by the
previous version, which simulates restoring a pre-upgrade backup after a
server upgrade. Run this before bumping the pinned OpenBao version.

## Notes / limitations

- Only KV v2 mounts are handled (all of them are auto-discovered via
  `sys/internal/ui/mounts`; the oauth proxy blocks `/v1/sys/mounts`).
- The dump stores the **current version** of each secret plus its
  `custom_metadata`. Older versions are not exported, and restore resets each
  secret's history to version 1.
- Secrets whose current version is (soft-)deleted are skipped with a warning
  during dump — and will therefore be **removed** by a later restore. This is
  an ordinary KV state rather than a gap in what the token could see, so it
  does **not** mark the dump incomplete; if it did, any store holding one
  soft-deleted secret would need `--allow-incomplete` forever, and a flag you
  always pass protects nothing.
- Secrets your token cannot **read**, and subtrees it cannot **list**, are a
  different matter: dump warns, records `"complete": false` in the file along
  with the reasons, and restore then refuses that file. Absence in a dump means
  "delete this" to a restore, so the fact that something was missed has to
  travel in the file — a warning on stderr is gone by the time a cron-produced
  dump is restored months later.
- A mount your token cannot see at all is the one case dump **cannot** detect:
  `sys/internal/ui/mounts` is filtered by policy, so the mount is simply not
  there, and the dump is honestly complete as far as it can tell. That gap is
  closed on the restore side instead — restore refuses to empty a mount the
  file never mentions. Still, dump with a token that can read and list
  everything you intend to keep.
- Numbers at or above 2^53 (e.g. int64 max) are rounded to float64 precision
  by OpenBao itself — even when written through the raw HTTP API, the exact
  value is never observable again, so dump/restore cannot make it worse.
  Dump prints a warning when it sees such numbers; store huge numbers as
  strings if you need them exact.
- If the file references a KV v2 mount that doesn't exist on the server,
  restore aborts before making changes (mount creation is blocked by the
  proxy — create the mount manually first).
- The dump file contains all secret values in plaintext. Treat it like a
  credential. It is written with `O_EXCL`, so dump refuses to write onto any
  path that already exists — including a symlink someone else placed there.
  Give each dump a fresh name; the examples above timestamp it.
- Dump/restore filenames must end in `.json`, which matches this repo's
  `*.json` ignore rule, but a suffix cannot prove a path is ignored — other
  repos have other rules, and negation rules exist. Dump warns when the output
  lands inside a git working tree; confirm the path is ignored before you
  commit anything.
- `BAO_ADDR` is not forced to be https, because the test suites use a
  disposable dev server over http. A non-https address that is not a loopback
  or dev-server alias prints a warning: your token, your cookie and every
  secret value cross the network in cleartext.
- Env vars are visible in `docker inspect` on a running container and in your
  shell history if set inline — prefer `--env-file` with a mode-600
  `baokv.env`. The oauth2-proxy cookie is additionally passed on the `bao`
  command line, so it is readable from `/proc` by anything sharing the
  container's PID namespace.
