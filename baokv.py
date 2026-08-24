#!/usr/bin/env python3
"""Dump / restore all KV v2 secrets of an OpenBao instance.

Runs INSIDE a docker image (openbao + python3) — the only local dependency
is the docker CLI. The `bao` binary in the image makes all API calls; an
oauth2-proxy cookie is injected on every request via `bao -header`. Mount a
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
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

DUMP_FORMAT = "openbao-kvv2-dump-v1"


def require_env(name, hint):
    value = os.environ.get(name, "").strip()
    if not value or "REPLACE_WITH" in value:
        sys.exit(f"error: set the {name} env var ({hint}) — "
                 "pass it with docker run -e or --env-file")
    return value


def cookie_header():
    raw = require_env("BAO_COOKIE", "your _oauth2_proxy cookie value")
    if not raw.startswith("_oauth2_proxy="):
        raw = "_oauth2_proxy=" + raw
    return "Cookie=" + raw


def setup_env():
    os.environ["VAULT_ADDR"] = require_env(
        "BAO_ADDR", "server URL, e.g. https://foobar.example.com")
    os.environ["VAULT_TOKEN"] = require_env("BAO_TOKEN", "your OpenBao token")


def bao(subcommand, *rest, stdin=None, check=True):
    """Run a bao subcommand; inject the oauth2 cookie.

    `subcommand` is the full space-separated command ("kv metadata delete");
    the -header flag must come after it but before positional args.
    """
    cmd = (["bao"] + subcommand.split()
           + [f"-header={cookie_header()}"] + list(rest))
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip()
        if "Redirecting" in err or "302" in err:
            sys.exit("error: got an OAuth redirect — your _oauth2_proxy "
                     "cookie has likely expired. Grab a fresh one from your "
                     "browser and update the BAO_COOKIE env var")
        if check:
            sys.exit(f"error: bao {subcommand}: {err}")
        return None
    return p.stdout


def bao_json(*args, **kw):
    out = bao(*args, **kw)
    return json.loads(out) if out else None


def preflight():
    info = bao_json("token lookup", "-format=json")
    d = info["data"]
    expires = (d.get("expire_time") or "never")[:10]
    print(f"# auth ok: {d.get('display_name')} policies={d.get('policies')} "
          f"token expires {expires}", file=sys.stderr)


def kvv2_mounts():
    out = bao_json("read", "-format=json", "sys/internal/ui/mounts")
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
        out = bao_json("kv list", "-format=json", f"{mount}/{prefix}", check=False)
        for entry in (out or []):
            if entry.endswith("/"):
                stack.append(prefix + entry)
            else:
                leaves.append(prefix + entry)
    return sorted(leaves)


def read_secret(mount, path):
    out = bao_json("kv get", "-format=json", f"{mount}/{path}", check=False)
    if out is None or out["data"]["data"] is None:
        # unreadable, or current version soft-deleted/destroyed (the API
        # then returns success with data: null)
        return None
    meta = out["data"]["metadata"]
    return {
        "data": out["data"]["data"],
        "custom_metadata": meta.get("custom_metadata") or None,
        "version_at_export": meta.get("version"),
        "created_time_at_export": meta.get("created_time"),
    }


def collect(include_values):
    result = {}
    for mount in kvv2_mounts():
        print(f"# walking {mount}/ ...", file=sys.stderr)
        secrets = {}
        for path in walk(mount):
            if include_values:
                s = read_secret(mount, path)
                if s is None:
                    print(f"#   WARNING: skipping {mount}/{path} "
                          "(current version deleted or unreadable)", file=sys.stderr)
                    continue
                secrets[path] = s
            else:
                secrets[path] = None
        result[mount] = secrets
    return result


def cmd_list(_args):
    preflight()
    data = collect(include_values=True)
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
    data = collect(include_values=True)
    doc = {
        "format": DUMP_FORMAT,
        "address": os.environ["VAULT_ADDR"],
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mounts": data,
    }
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    n = sum(len(s) for s in data.values())
    print(f"# wrote {n} secrets from {len(data)} mount(s) to {args.output}")


def cmd_restore(args):
    # validate the input file fully before any server interaction
    with open(args.input) as f:
        doc = json.load(f)
    if doc.get("format") != DUMP_FORMAT:
        sys.exit(f"error: {args.input} is not a {DUMP_FORMAT} file")

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
    server_state = {m: walk(m) for m in server_mounts}

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

    if args.dry_run:
        print("\n# dry run: no changes made")
        return
    if not args.yes:
        reply = input("\nType 'yes' to apply these changes: ")
        if reply.strip() != "yes":
            sys.exit("aborted, no changes made")

    for mount, path in to_delete:
        bao("kv metadata delete", f"{mount}/{path}")
        print(f"deleted {mount}/{path}")
    for mount, path, s in to_write:
        # metadata delete wipes old versions so the imported state is clean
        bao("kv metadata delete", f"{mount}/{path}", check=False)
        bao("kv put", f"{mount}/{path}", "-",
            stdin=json.dumps(s["data"]))
        if s.get("custom_metadata"):
            bao("write", f"{mount}/metadata/{path}", "-",
                stdin=json.dumps({"custom_metadata": s["custom_metadata"]}))
        print(f"imported {mount}/{path}")
    print(f"\n# done: {len(to_write)} imported, {len(to_delete)} deleted")


def json_path(value):
    # dump files hold plaintext secrets; forcing a .json suffix keeps them
    # covered by the repo's *.json gitignore rule
    if not value.endswith(".json"):
        raise argparse.ArgumentTypeError(
            f"'{value}' must end in .json so dump files stay git-ignored")
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="audit all secrets (paths + key names, no values)")
    p = sub.add_parser("dump", help="export all kv-v2 secrets to a JSON file")
    p.add_argument("-o", "--output", required=True, type=json_path)
    p = sub.add_parser("restore", help="DESTRUCTIVE: make server match a dump file")
    p.add_argument("-i", "--input", required=True, type=json_path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = ap.parse_args()

    setup_env()
    {"list": cmd_list, "dump": cmd_dump, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    main()
