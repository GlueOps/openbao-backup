#!/usr/bin/env python3
"""Dump / restore all KV v2 secrets of an OpenBao instance.

Runs INSIDE a docker image (openbao + python3) — the only local dependency
is the docker CLI. The `bao` binary in the image makes all API calls; an
oauth2-proxy cookie is sent as a request header on every call. Mount a
host directory at /work for dump files. See README.md for the full
`docker run` commands.

Config (env vars, pass with `docker run -e` or `--env-file`):
  BAO_ADDR    server URL, e.g. https://foobar.example.com
  BAO_TOKEN   OpenBao token
  BAO_COOKIE  oauth2-proxy cookie ("_oauth2_proxy=..." or just the value)

Subcommands:
  list                          # audit: paths, key names, no values
  dump -o secrets.json          # export everything to a file
  restore -i secrets.json       # DESTRUCTIVE: make server match file
  restore -i secrets.json --dry-run
  restore -i secrets.json --yes
  restore -i secrets.json --allow-incomplete --allow-mount-deletion
"""

import argparse
import datetime
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

DUMP_FORMAT = "openbao-kvv2-dump-v1"


def require_env(name, hint):
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("REPLACE_WITH"):
        sys.exit(f"error: set the {name} env var ({hint}) — "
                 "pass it with docker run -e or --env-file")
    return value


def cookie_value():
    raw = require_env("BAO_COOKIE", "your _oauth2_proxy cookie value")
    if not raw.startswith("_oauth2_proxy="):
        raw = "_oauth2_proxy=" + raw
    return raw


# hosts where cleartext is expected: the test suites run a disposable dev
# server over http on a private docker network
PLAINTEXT_OK = ("localhost", "127.0.0.1", "::1", "openbao")


def setup_env():
    addr = require_env("BAO_ADDR", "server URL, e.g. https://foobar.example.com")
    host = urllib.parse.urlparse(addr).hostname or ""
    if not addr.startswith("https://") and host not in PLAINTEXT_OK:
        print(f"# WARNING: {addr} is not https — your token, your oauth2-proxy "
              "cookie and every secret value will cross the network in "
              "cleartext", file=sys.stderr)
    os.environ["VAULT_ADDR"] = addr
    os.environ["VAULT_TOKEN"] = require_env("BAO_TOKEN", "your OpenBao token")


last_error = None
last_status = None

# Warnings that mean the RESULT IS MISSING DATA. A later restore reads absence
# as "this secret should not exist" and deletes it, so this has to travel in
# the dump file — a line on stderr is gone by the time a cron-produced file is
# restored months later.
incomplete_reasons = []


def warn_incomplete(msg):
    incomplete_reasons.append(msg)
    print(f"#   WARNING: {msg}", file=sys.stderr)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """The oauth2-proxy answers an expired session with a redirect to the OAuth
    login. Following it would fetch an HTML page and fail later with a confusing
    JSON error, so refuse the redirect and let it surface as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = None


def opener():
    global _opener
    if _opener is None:
        ctx = ssl.create_default_context(
            cafile=os.environ.get("BAO_CACERT") or None)
        _opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    return _opener


def enc(path):
    """Percent-encode a KV path for a URL. `/` stays a separator; everything
    else is escaped, including a literal `%` (so a secret actually named
    `%2F` survives as `%252F` rather than decoding back into a separator)."""
    return urllib.parse.quote(path, safe="/")


def expired_cookie():
    sys.exit("error: got an OAuth redirect — your _oauth2_proxy cookie has "
             "likely expired. Grab a fresh one from your browser and update "
             "the BAO_COOKIE env var")


def api(method, path, body=None, check=True):
    """One OpenBao HTTP call. `path` is everything after /v1/, already
    percent-encoded. Returns the decoded JSON body, or None on a handled
    failure when check is False.

    The token and the session cookie travel as request headers. They used to
    be `bao -header=Cookie=...` on the command line, where anything sharing
    the container's PID namespace could read them out of /proc.
    """
    global last_error, last_status
    url = f"{os.environ['VAULT_ADDR'].rstrip('/')}/v1/{path}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("X-Vault-Token", os.environ["VAULT_TOKEN"])
    req.add_header("Cookie", cookie_value())
    if body is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with opener().open(req) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        last_status = e.code
        if e.code in (301, 302, 303, 307, 308):
            expired_cookie()
        raw = e.read()
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            doc = None
        # kv-v2 answers 404 for a soft-deleted or destroyed current version,
        # but with the real envelope attached (data.data is null). That is not
        # a failure — the caller needs to see it to tell the two apart.
        if e.code == 404 and isinstance(doc, dict) and doc.get("data") is not None:
            last_error = None
            return doc
        errors = (doc or {}).get("errors") or []
        last_error = "; ".join(errors) or f"HTTP {e.code}"
        if check:
            sys.exit(f"error: {method} {path}: {last_error}")
        return None
    except urllib.error.URLError as e:
        last_status = None                # never reached the server
        last_error = str(getattr(e, "reason", e))
        if check:
            sys.exit(f"error: {method} {path}: cannot reach "
                     f"{os.environ['VAULT_ADDR']}: {last_error}")
        return None

    last_error, last_status = None, 200
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # a proxy that serves its login page with a 200 rather than a redirect
        expired_cookie()


def preflight():
    d = (api("GET", "auth/token/lookup-self") or {}).get("data") or {}
    if not d:
        sys.exit("error: token lookup returned no data — is BAO_TOKEN a valid "
                 "token for this server?")
    expires = (d.get("expire_time") or "never")[:10]
    print(f"# auth ok: {d.get('display_name')} policies={d.get('policies')} "
          f"token expires {expires}", file=sys.stderr)


def kvv2_mounts():
    out = api("GET", "sys/internal/ui/mounts")
    mounts = []
    for path, info in sorted(out["data"]["secret"].items()):
        if info.get("type") == "kv" and (info.get("options") or {}).get("version") == "2":
            mounts.append(path.rstrip("/"))
    return mounts


def walk(mount):
    """Return sorted list of all secret paths (leaves) under a kv-v2 mount."""
    leaves, stack = [], [""]
    while stack:
        prefix = stack.pop()
        out = api("GET", f"{enc(mount)}/metadata/{enc(prefix)}?list=true",
                  check=False)
        # 404 is how kv-v2 says "nothing here", which is a real answer. Every
        # other failure — denied, a 5xx, a connection reset mid-walk — leaves a
        # subtree looking empty when it is not, and a later restore reads that
        # as "delete all of it". Only 404 may pass without a warning.
        if out is None and last_status != 404:
            warn_incomplete(f"cannot list {mount}/{prefix} ({last_error}) — "
                            "this whole subtree is MISSING from the result and "
                            "a restore from this dump would DELETE it")
        for entry in ((out or {}).get("data") or {}).get("keys") or []:
            if entry.endswith("/"):
                stack.append(prefix + entry)
            else:
                leaves.append(prefix + entry)
    return sorted(leaves)


# A secret whose current version is soft-deleted or destroyed is an ordinary,
# documented state, not a gap in what we were allowed to see. Marking those
# dumps incomplete would make --allow-incomplete a permanent habit, and a flag
# everyone always passes stops protecting anything.
SOFT_DELETED = "soft-deleted"


def read_secret(mount, path):
    out = api("GET", f"{enc(mount)}/data/{enc(path)}", check=False)
    if out is None or out.get("data") is None:
        return None                       # unreadable: denied, or an error
    if out["data"]["data"] is None:
        return SOFT_DELETED               # success, but data: null
    meta = out["data"]["metadata"]
    return {
        "data": out["data"]["data"],
        "custom_metadata": meta.get("custom_metadata") or None,
        "version_at_export": meta.get("version"),
        "created_time_at_export": meta.get("created_time"),
    }


def has_unsafe_number(v):
    """True if any number in v needs more precision than float64 offers.
    OpenBao itself decodes JSON numbers into float64, so such values have
    already been rounded server-side and cannot be recovered — reading them
    over the raw HTTP API does not help (test Q measures exactly this)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return abs(v) >= 2 ** 53
    if isinstance(v, dict):
        return any(has_unsafe_number(x) for x in v.values())
    if isinstance(v, list):
        return any(has_unsafe_number(x) for x in v)
    return False


def collect():
    """Read every secret in every visible kv-v2 mount. `list` needs the values
    too — it prints key names, not values — so there is no cheaper mode."""
    result = {}
    for mount in kvv2_mounts():
        print(f"# walking {mount}/ ...", file=sys.stderr)
        secrets = {}
        for path in walk(mount):
            s = read_secret(mount, path)
            if s is SOFT_DELETED:
                print(f"#   WARNING: skipping {mount}/{path} (current version "
                      "soft-deleted or destroyed) — a restore from this dump "
                      "will remove it", file=sys.stderr)
                continue
            if s is None:
                warn_incomplete(f"cannot read {mount}/{path} — it is MISSING "
                                "from the result and a restore would DELETE it")
                continue
            if has_unsafe_number(s["data"]):
                print(f"#   WARNING: {mount}/{path} contains a number at or "
                      "above 2^53 — OpenBao stores such values as float64, so "
                      "the exact value is already gone; store huge numbers as "
                      "strings", file=sys.stderr)
            secrets[path] = s
        result[mount] = secrets
    return result


def cmd_list(_args):
    preflight()
    data = collect()
    total = 0
    for mount, secrets in data.items():
        print(f"{mount}/  ({len(secrets)} secrets)")
        for path, s in secrets.items():
            keys = ", ".join(sorted(s["data"].keys())) or "(empty)"
            cm = " +custom_metadata" if s["custom_metadata"] else ""
            print(f"  {path}  [v{s['version_at_export']}] keys: {keys}{cm}")
            total += 1
    print(f"# total: {total} secrets")


def cmd_dump(args):
    preflight()
    data = collect()
    doc = {
        "format": DUMP_FORMAT,
        "address": os.environ["VAULT_ADDR"],
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "complete": not incomplete_reasons,
        "mounts": data,
    }
    if incomplete_reasons:
        doc["incomplete_count"] = len(incomplete_reasons)
        doc["incomplete_reasons"] = incomplete_reasons[:20]
    warn_if_in_git_worktree(args.output)
    # O_EXCL: never write secrets into a path someone else pre-created (a
    # symlink, a hard link, or a file they still hold an open fd on).
    # O_NOFOLLOW: belt and braces, since O_EXCL already refuses symlinks.
    try:
        fd = os.open(args.output,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        sys.exit(f"error: {args.output} already exists — refusing to write "
                 "secrets onto an existing path. Pick a new filename (the "
                 "README's examples timestamp it) or remove the old file.")
    os.fchmod(fd, 0o600)  # the mode above applies only on create
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    n = sum(len(s) for s in data.values())
    print(f"# wrote {n} secrets from {len(data)} mount(s) to {args.output}")
    if incomplete_reasons:
        print(f"# INCOMPLETE: {len(incomplete_reasons)} secret(s) or subtree(s) "
              "could not be read. This dump is marked incomplete and restore "
              "will refuse it — everything missing here would be DELETED by a "
              "restore. Re-dump with a token that can read and list "
              "everything.", file=sys.stderr)


def check_capabilities(to_write, to_delete):
    """Confirm the token may perform every operation in the plan before any of
    it runs. sys/capabilities-self is a pure read — no canary is written into
    the target mounts."""
    need = {}
    for mount, path, _ in to_write:
        need[f"{mount}/data/{path}"] = {"create", "update"}   # either suffices
        need[f"{mount}/metadata/{path}"] = {"delete"}         # wipes history
    for mount, path in to_delete:
        need[f"{mount}/metadata/{path}"] = {"delete"}
    if not need:
        return
    paths, denied = sorted(need), []
    for i in range(0, len(paths), 100):
        chunk = paths[i:i + 100]
        out = api("POST", "sys/capabilities-self", {"paths": chunk},
                  check=False)
        if out is None:
            # advisory only: the reordering below is what prevents data loss
            print("# WARNING: could not check token capabilities "
                  f"({last_error}) — proceeding without the pre-check",
                  file=sys.stderr)
            return
        for p in chunk:
            got = set(out["data"].get(p) or [])
            if not ({"root", "sudo"} & got) and not (need[p] & got):
                denied.append(f"{p} (need one of {sorted(need[p])}, have "
                              f"{sorted(got) or ['nothing']})")
    if denied:
        shown = "\n  ".join(denied[:10])
        more = f"\n  ... and {len(denied) - 10} more" if len(denied) > 10 else ""
        sys.exit(f"error: this token cannot carry out the whole plan, so the "
                 f"restore would stop part-way through:\n  {shown}{more}\n"
                 "Nothing has been changed. Re-run with a token that covers "
                 "every path above.")


def cmd_restore(args):
    # validate the input file fully before any server interaction
    with open(args.input) as f:
        doc = json.load(f)
    if doc.get("format") != DUMP_FORMAT:
        sys.exit(f"error: {args.input} is not a {DUMP_FORMAT} file")
    # A missing "complete" key means a dump written before this field existed;
    # those cannot be judged, so they are allowed through.
    if doc.get("complete") is False and not args.allow_incomplete:
        reasons = "\n  ".join(doc.get("incomplete_reasons") or ["(not recorded)"])
        sys.exit(f"error: {args.input} is marked INCOMPLETE — "
                 f"{doc.get('incomplete_count', '?')} secret(s) or subtree(s) "
                 "could not be read when it was made:\n  "
                 f"{reasons}\nRestoring it would DELETE every one of them "
                 "from the server. Re-dump with a token that can read and list "
                 "everything, or pass --allow-incomplete.")
    source = doc.get("address")
    if source and source != os.environ["VAULT_ADDR"] and not args.allow_address_mismatch:
        sys.exit(f"error: {args.input} was dumped from {source} but BAO_ADDR "
                 f"is {os.environ['VAULT_ADDR']}. Restoring it here would make "
                 "this server match a different server's contents. Pass "
                 "--allow-address-mismatch if that is what you intend.")

    preflight()
    file_mounts = doc["mounts"]
    server_mounts = kvv2_mounts()
    missing = [m for m in file_mounts if m not in server_mounts]
    if missing:
        sys.exit(f"error: mounts in file but not on server: {missing} "
                 "(mount creation is blocked by the oauth proxy — create them "
                 "manually first)")

    # Audit current server state before touching anything.
    print("# auditing current server state ...", file=sys.stderr)
    audit_start = len(incomplete_reasons)
    server_state = {m: walk(m) for m in server_mounts}
    if len(incomplete_reasons) > audit_start:
        print("# WARNING: parts of the server could not be listed during the "
              "audit, so the plan below may be missing entries", file=sys.stderr)

    # A mount the FILE has no entry for is not a mount the file says is empty.
    # `sys/internal/ui/mounts` is filtered by the dumping token's policy, so a
    # mount that token could not see is simply absent — and treating absent as
    # empty deletes every secret in it, permanently, with no warning.
    absent = [m for m in server_mounts if m not in doc["mounts"]]
    if absent and not args.allow_mount_deletion:
        detail = "\n  ".join(
            f"{m}/ ({len(server_state[m])} secret"
            f"{'' if len(server_state[m]) == 1 else 's'})" for m in absent)
        sys.exit(f"error: these kv-v2 mounts exist on the server but are "
                 f"absent from {args.input}:\n  {detail}\n"
                 "The file has no opinion about them, which is not the same as "
                 "saying they should be empty — the dump may have been made "
                 "with a token that could not see them. Every secret in them "
                 "would be permanently deleted. Re-dump with a token that "
                 "covers every mount, or pass --allow-mount-deletion.")

    to_delete, to_write = [], []
    for mount, paths in server_state.items():
        wanted = set(file_mounts.get(mount, {}))
        to_delete += [(mount, p) for p in paths if p not in wanted]
    for mount, secrets in file_mounts.items():
        to_write += [(mount, p, s) for p, s in sorted(secrets.items())]

    print(f"\nRestore plan (server will exactly match {args.input}):")
    print(f"  write (wipe history, then import): {len(to_write)}")
    for m, p, _ in to_write:
        tag = "overwrite" if p in server_state.get(m, []) else "create"
        print(f"    ~ {m}/{p}  ({tag})")
    print(f"  DELETE (on server, not in file): {len(to_delete)}")
    for m, p in to_delete:
        print(f"    - {m}/{p}")

    check_capabilities(to_write, to_delete)

    if args.dry_run:
        print("\n# dry run: no changes made")
        return
    if not args.yes:
        reply = input("\nType 'yes' to apply these changes: ")
        if reply.strip() != "yes":
            sys.exit("aborted, no changes made")

    # Writes first, deletions last. Everything the write phase touches is
    # present in the file, so an abort there is recoverable by re-running.
    # Deletions remove data that is NOT in the file and nothing can bring it
    # back, so they must not run until every write has succeeded.
    written = deleted = 0
    phase = "write"
    try:
        for mount, path, s in to_write:
            # metadata delete wipes old versions so the imported state is
            # clean; -cas=0 is then always valid and keeps restore working on
            # mounts with cas_required=true
            api("DELETE", f"{enc(mount)}/metadata/{enc(path)}", check=False)
            api("POST", f"{enc(mount)}/data/{enc(path)}",
                {"data": s["data"], "options": {"cas": 0}})
            if s.get("custom_metadata"):
                api("POST", f"{enc(mount)}/metadata/{enc(path)}",
                    {"custom_metadata": s["custom_metadata"]})
            written += 1
            print(f"imported {mount}/{path}")
        phase = "delete"
        for mount, path in to_delete:
            api("DELETE", f"{enc(mount)}/metadata/{enc(path)}")
            deleted += 1
            print(f"deleted {mount}/{path}")
    except SystemExit:
        if phase == "write":
            print(f"\n# ABORTED during the write phase: {written}/{len(to_write)} "
                  f"imported, none of the {len(to_delete)} deletions attempted. "
                  "Nothing outside the file was touched — every secret affected "
                  f"so far is present in {args.input}, so re-running the same "
                  "restore recovers it.", file=sys.stderr)
        else:
            print(f"\n# ABORTED during the delete phase: all {written} writes "
                  f"succeeded, {deleted}/{len(to_delete)} deletions done. The "
                  "remaining deletions did not run; re-run the same restore to "
                  "finish.", file=sys.stderr)
        raise
    print(f"\n# done: {written} imported, {deleted} deleted")


def json_path(value):
    if not value.endswith(".json"):
        raise argparse.ArgumentTypeError(f"'{value}' must end in .json")
    return value


def warn_if_in_git_worktree(path):
    """A dump holds every secret in plaintext. The .json suffix matches this
    repo's `*.json` ignore rule, but a suffix cannot prove a path is ignored —
    negation rules exist, and other repos have other rules. Say so out loud
    rather than implying a guarantee (git is not in the image, so the ignore
    rules cannot be evaluated here)."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            print(f"# WARNING: {path} is inside a git working tree ({d}). It "
                  "will hold every secret in plaintext — confirm the path is "
                  "git-ignored before committing anything.", file=sys.stderr)
            return
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="audit all secrets (paths + key names, no values)")
    p = sub.add_parser("dump", help="export all kv-v2 secrets to a JSON file")
    p.add_argument("-o", "--output", required=True, type=json_path)
    p = sub.add_parser("restore", help="DESTRUCTIVE: make server match a dump file")
    p.add_argument("-i", "--input", required=True, type=json_path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-address-mismatch", action="store_true",
                   help="restore a dump taken from a different BAO_ADDR")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="restore a dump marked incomplete (DELETES everything "
                        "the dump could not read)")
    p.add_argument("--allow-mount-deletion", action="store_true",
                   help="empty kv-v2 mounts the dump file does not mention")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = ap.parse_args()

    setup_env()
    {"list": cmd_list, "dump": cmd_dump, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    main()
