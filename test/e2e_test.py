#!/usr/bin/env python3
"""End-to-end regression suite for baokv. Runs inside the baokv image against
a disposable OpenBao dev server (see run-tests.sh). Covers CLI guards, mount
discovery, deep/shallow nesting, special-character paths, value edge cases,
custom metadata, soft-deleted/destroyed versions, restore semantics
(create/overwrite/delete, dry-run, prompt, history wipe, missing mounts),
dump-file properties, and full wipe-and-restore round trips."""
import json
import os
import subprocess
import sys

TOOL = ["python3", "/app/baokv.py"]
WORK = "/tmp/testwork"
os.makedirs(WORK, exist_ok=True)
os.chdir(WORK)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    extra = "" if cond or not detail else f"   [{str(detail)[:200]}]"
    print(f"{mark}  {name}{extra}")


def tool(*args, env=None, stdin=None):
    e = dict(os.environ) if env is None else env
    return subprocess.run(TOOL + list(args), capture_output=True, text=True,
                          input=stdin, env=e)


def bao(*args, stdin=None):
    return subprocess.run(["bao"] + list(args), capture_output=True,
                          text=True, input=stdin)


def put(mount, path, data):
    p = bao("kv", "put", f"{mount}/{path}", "-", stdin=json.dumps(data))
    assert p.returncode == 0, f"seed put {mount}/{path} failed: {p.stderr}"


def get_data(mount, path):
    p = bao("kv", "get", "-format=json", f"{mount}/{path}")
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)["data"]["data"]


def get_version(mount, path):
    p = bao("kv", "get", "-format=json", f"{mount}/{path}")
    return json.loads(p.stdout)["data"]["metadata"]["version"]


def reset_mount(mount, version="2"):
    bao("secrets", "disable", f"{mount}/")
    p = bao("secrets", "enable", f"-path={mount}", f"-version={version}", "kv")
    assert p.returncode == 0, f"enable {mount} failed: {p.stderr}"


def load_mounts(fname):
    return json.load(open(fname))["mounts"]


MINIMAL_ENV = {"PATH": os.environ["PATH"]}
FULL_ENV = {**MINIMAL_ENV,
            "BAO_ADDR": os.environ["BAO_ADDR"],
            "BAO_TOKEN": os.environ["BAO_TOKEN"],
            "BAO_COOKIE": os.environ["BAO_COOKIE"]}

# ---------------------------------------------------------------- A. guards
print("== A. CLI and config guards ==")
p = tool("dump", "-o", "backup.txt")
check("A1 non-.json dump output rejected", p.returncode != 0 and ".json" in p.stderr, p.stderr)
p = tool("restore", "-i", "backup.yaml")
check("A2 non-.json restore input rejected", p.returncode != 0 and ".json" in p.stderr, p.stderr)
p = tool("list", env=MINIMAL_ENV)
check("A3 missing BAO_ADDR reported", p.returncode != 0 and "BAO_ADDR" in p.stdout + p.stderr, p.stderr)
p = tool("list", env={**MINIMAL_ENV, "BAO_ADDR": "http://openbao:8200"})
check("A4 missing BAO_TOKEN reported", p.returncode != 0 and "BAO_TOKEN" in p.stdout + p.stderr)
p = tool("list", env={**FULL_ENV, "BAO_COOKIE": ""})
check("A5 empty BAO_COOKIE reported", p.returncode != 0 and "BAO_COOKIE" in p.stdout + p.stderr)
p = tool("list", env={**FULL_ENV, "BAO_TOKEN": "REPLACE_WITH_YOUR_OPENBAO_TOKEN"})
check("A6 placeholder value rejected", p.returncode != 0 and "BAO_TOKEN" in p.stdout + p.stderr)
p = tool("restore", "-i", "does-not-exist.json")
check("A7 nonexistent restore file errors", p.returncode != 0)
with open("badformat.json", "w") as f:
    json.dump({"something": "else"}, f)
p = tool("restore", "-i", "badformat.json")
check("A8 wrong-format file rejected", p.returncode != 0 and "openbao-kvv2-dump-v1" in p.stdout + p.stderr)
p = tool("list", env={**FULL_ENV, "BAO_TOKEN": "s.invalidtoken123456789012"})
check("A9 bad token surfaces an error", p.returncode != 0)
p = tool("list", env={**FULL_ENV, "BAO_COOKIE": "_oauth2_proxy=someval"})
check("A10 cookie accepted with _oauth2_proxy= prefix", p.returncode == 0, p.stderr)
p = tool("list", env={**FULL_ENV, "BAO_COOKIE": "someval"})
check("A11 cookie accepted without prefix", p.returncode == 0, p.stderr)

# ------------------------------------------------------- B. mount discovery
print("== B. mount discovery ==")
reset_mount("secret")
reset_mount("kv2b")
reset_mount("kv1", version="1")
put("secret", "app", {"k": "v-secret"})
put("kv2b", "app", {"k": "v-kv2b"})
p = bao("kv", "put", "kv1/app", "-", stdin=json.dumps({"k": "v-kv1"}))
assert p.returncode == 0
p = tool("dump", "-o", "mounts.json")
m = load_mounts("mounts.json")
check("B1 dump exits 0", p.returncode == 0, p.stderr)
check("B2 all kv-v2 mounts discovered", sorted(m) == ["kv2b", "secret"], sorted(m))
check("B3 kv v1 mount excluded", "kv1" not in m)
check("B4 system mounts excluded", not ({"sys", "identity", "cubbyhole"} & set(m)))
check("B5 per-mount contents correct",
      m["secret"]["app"]["data"] == {"k": "v-secret"} and m["kv2b"]["app"]["data"] == {"k": "v-kv2b"})

# ------------------------------------- C. nesting depth and special paths
print("== C. path depth and special characters ==")
reset_mount("secret")
reset_mount("kv2b")
DEPTH_PATHS = {}
for depth in (1, 2, 3, 5, 8, 10):
    path = "/".join(f"d{depth}l{i}" for i in range(1, depth + 1))
    DEPTH_PATHS[path] = {"depth": str(depth)}
SPECIAL_PATHS = {
    "sp/with space/name with spaces": {"v": "1"},
    "sp/dots.dashes-under_scores/v1.2.3": {"v": "2"},
    "sp/unicode-日本語-🚀/emoji-秘密": {"v": "3"},
    "sp/at@plus+eq=amp&/comma,paren(x)": {"v": "4"},
    "sp/tilde~quote'dquote\"/back`tick": {"v": "5"},
    "sp/dollar$semi;pipe|/star*bang!": {"v": "6"},
    "sp/per%cent/hash#q?mark": {"v": "7"},
    "sp/-leading-dash/x": {"v": "8"},
    "both": {"leaf": "yes"},
    "both/child": {"nested": "yes"},
    "both/child/grandchild": {"deeper": "yes"},
}
ALL_PATHS = {**DEPTH_PATHS, **SPECIAL_PATHS}
for path, data in ALL_PATHS.items():
    put("secret", path, data)
p = tool("dump", "-o", "paths.json")
m = load_mounts("paths.json")["secret"]
check("C1 all depths captured (1,2,3,5,8,10)",
      all(pth in m for pth in DEPTH_PATHS), [p_ for p_ in DEPTH_PATHS if p_ not in m])
check("C2 all special-char paths captured",
      all(pth in m for pth in SPECIAL_PATHS), [p_ for p_ in SPECIAL_PATHS if p_ not in m])
check("C3 no phantom paths", sorted(m) == sorted(ALL_PATHS), set(m) - set(ALL_PATHS))
check("C4 leaf-and-folder coexistence values intact",
      m["both"]["data"] == {"leaf": "yes"} and m["both/child"]["data"] == {"nested": "yes"}
      and m["both/child/grandchild"]["data"] == {"deeper": "yes"})
check("C5 all values intact", all(m[pth]["data"] == d for pth, d in ALL_PATHS.items()))
# round trip: wipe, restore, compare
reset_mount("secret")
p = tool("restore", "-i", "paths.json", "--yes")
check("C6 restore of all paths exits 0", p.returncode == 0, p.stderr)
check("C7 every path readable with identical value after restore",
      all(get_data("secret", pth) == d for pth, d in ALL_PATHS.items()))

# ------------------------------------------------------ D. value edge cases
print("== D. value edge cases ==")
reset_mount("secret")
reset_mount("kv2b")
VALUES = {
    "v/unicode": {"s": "héllo 日本語 🚀 ñ ✓"},
    "v/multiline": {"s": "l1\nl2\r\nl3\ttab\\backslash"},
    "v/quotes": {"s": "\"double\" 'single' `tick` $var | ; &"},
    "v/empty-value": {"k": ""},
    "v/empty-key-name": {"": "empty key"},
    "v/big-100k": {"blob": "A" * 100_000},
    "v/sixty-keys": {f"key_{i:02d}": str(i) for i in range(60)},
    "v/nested-json": {"obj": {"a": [1, 2, {"b": True}], "c": None}},
    "v/types": {"int": 42, "float": 3.14159, "true": True, "false": False, "null": None},
}
for path, data in VALUES.items():
    put("secret", path, data)
tool("dump", "-o", "values.json")
reset_mount("secret")
tool("restore", "-i", "values.json", "--yes")
for path, data in VALUES.items():
    check(f"D {path} identical after round trip", get_data("secret", path) == data,
          get_data("secret", path))

# ----------------------------------------------------- E. custom metadata
print("== E. custom metadata ==")
reset_mount("secret")
reset_mount("kv2b")
put("secret", "meta/app", {"k": "v"})
p = bao("kv", "metadata", "put", "-custom-metadata=owner=alice",
        "-custom-metadata=env=prod", "secret/meta/app")
assert p.returncode == 0, p.stderr
put("secret", "meta/plain", {"k": "v"})
tool("dump", "-o", "meta.json")
m = load_mounts("meta.json")["secret"]
check("E1 custom_metadata captured in dump",
      m["meta/app"]["custom_metadata"] == {"owner": "alice", "env": "prod"}, m["meta/app"])
check("E2 absent custom_metadata dumped as null", m["meta/plain"]["custom_metadata"] is None)
reset_mount("secret")
tool("restore", "-i", "meta.json", "--yes")
p = bao("kv", "metadata", "get", "-format=json", "secret/meta/app")
cm = json.loads(p.stdout)["data"]["custom_metadata"]
check("E3 custom_metadata restored", cm == {"owner": "alice", "env": "prod"}, cm)


def get_meta(path):
    p_ = bao("kv", "metadata", "get", "-format=json", f"secret/{path}")
    return json.loads(p_.stdout)["data"]


UNI_CM = {"键": "日本語 🚀", "note": 'spaces & "quotes" | pipes'}
put("secret", "meta/uni", {"k": "v"})
p = bao("kv", "metadata", "put", "-custom-metadata=键=日本語 🚀",
        '-custom-metadata=note=spaces & "quotes" | pipes', "secret/meta/uni")
assert p.returncode == 0, p.stderr
tool("dump", "-o", "meta2.json")
m2 = load_mounts("meta2.json")["secret"]
check("E4 unicode/special-char custom_metadata captured",
      m2["meta/uni"]["custom_metadata"] == UNI_CM, m2["meta/uni"]["custom_metadata"])
reset_mount("secret")
tool("restore", "-i", "meta2.json", "--yes")
check("E5 unicode/special-char custom_metadata restored",
      get_meta("meta/uni")["custom_metadata"] == UNI_CM)
# drift: changed values and extra keys reverted; metadata absent in the file
# must end up absent on the server
bao("kv", "metadata", "put", "-custom-metadata=owner=mallory",
    "-custom-metadata=extra=drift", "secret/meta/app")
bao("kv", "metadata", "put", "-custom-metadata=sneaky=added", "secret/meta/plain")
tool("restore", "-i", "meta2.json", "--yes")
check("E6 custom_metadata drift reverted on restore",
      get_meta("meta/app")["custom_metadata"] == {"owner": "alice", "env": "prod"},
      get_meta("meta/app")["custom_metadata"])
check("E7 custom_metadata absent in file is removed from server",
      not get_meta("meta/plain")["custom_metadata"])
entry = m2["meta/app"]
check("E8 informational fields recorded per secret",
      entry["version_at_export"] == 1 and entry["created_time_at_export"])
# documented limitation, pinned: per-secret settings are NOT preserved
bao("kv", "metadata", "put", "-max-versions=3", "-delete-version-after=24h",
    "secret/meta/app")
tool("dump", "-o", "meta3.json")
reset_mount("secret")
tool("restore", "-i", "meta3.json", "--yes")
md = get_meta("meta/app")
check("E9 per-secret settings documented as dropped on restore",
      md.get("max_versions", 0) == 0
      and md.get("delete_version_after", "0s") in ("0s", "0", ""), md)

# ------------------------------------- F. soft-deleted / destroyed versions
print("== F. soft-deleted and destroyed versions ==")
reset_mount("secret")
reset_mount("kv2b")
put("secret", "alive", {"k": "v"})
put("secret", "softdel", {"k": "v"})
bao("kv", "delete", "secret/softdel")
put("secret", "destroyed", {"k": "v"})
bao("kv", "destroy", "-versions=1", "secret/destroyed")
p = tool("dump", "-o", "deleted.json")
m = load_mounts("deleted.json")["secret"]
check("F1 soft-deleted secret skipped", "softdel" not in m)
check("F2 destroyed secret skipped", "destroyed" not in m)
check("F3 live secret still dumped", m.get("alive", {}).get("data") == {"k": "v"})
check("F4 skip warning printed", "WARNING" in p.stdout + p.stderr)
p = tool("restore", "-i", "deleted.json", "--yes")
lst = bao("kv", "list", "-format=json", "secret/")
remaining = json.loads(lst.stdout)
check("F5 restore removes skipped secrets' metadata", remaining == ["alive"], remaining)

# ------------------------------------------------------ G. restore semantics
print("== G. restore semantics ==")
reset_mount("secret")
reset_mount("kv2b")
put("secret", "keep", {"k": "original"})
put("secret", "changed", {"k": "before"})
put("secret", "gone-later", {"k": "v"})
put("kv2b", "other-mount", {"k": "v"})
tool("dump", "-o", "baseline.json")
# drift: modify one, delete one, add one extra (in each mount)
put("secret", "changed", {"k": "after"})
bao("kv", "metadata", "delete", "secret/gone-later")
put("secret", "extra", {"k": "surplus"})
put("kv2b", "extra2", {"k": "surplus"})

p = tool("restore", "-i", "baseline.json", "--dry-run")
plan = p.stdout
check("G1 dry-run exits 0", p.returncode == 0, p.stderr)
check("G2 dry-run plan lists create/overwrite/delete",
      "(create)" in plan and "(overwrite)" in plan and "- secret/extra" in plan
      and "- kv2b/extra2" in plan, plan)
check("G3 dry-run changes nothing",
      get_data("secret", "changed") == {"k": "after"}
      and get_data("secret", "extra") == {"k": "surplus"}
      and get_data("secret", "gone-later") is None)

p = tool("restore", "-i", "baseline.json", stdin="no\n")
check("G4 prompt refusal aborts", p.returncode != 0 and "aborted" in p.stdout + p.stderr)
check("G5 refusal changes nothing", get_data("secret", "extra") == {"k": "surplus"})

p = tool("restore", "-i", "baseline.json", stdin="yes\n")
check("G6 prompt acceptance applies", p.returncode == 0, p.stderr)
check("G7 modified secret reverted", get_data("secret", "changed") == {"k": "before"})
check("G8 deleted secret recreated", get_data("secret", "gone-later") == {"k": "v"})
check("G9 extras deleted in every mount",
      get_data("secret", "extra") is None and get_data("kv2b", "extra2") is None)
check("G10 untouched secret intact", get_data("secret", "keep") == {"k": "original"})
check("G11 history wiped (version reset to 1)",
      get_version("secret", "changed") == 1 and get_version("secret", "keep") == 1)
p = tool("restore", "-i", "baseline.json", "--yes")
check("G12 restore is idempotent", p.returncode == 0
      and get_data("secret", "changed") == {"k": "before"})

doc = json.load(open("baseline.json"))
doc["mounts"]["missing-mount"] = {"x": {"data": {"k": "v"}, "custom_metadata": None}}
with open("missingmount.json", "w") as f:
    json.dump(doc, f)
before = tool("dump", "-o", "pre-missing.json")
p = tool("restore", "-i", "missingmount.json", "--yes")
tool("dump", "-o", "post-missing.json")
check("G13 missing mount aborts restore",
      p.returncode != 0 and "missing-mount" in p.stdout + p.stderr, p.stdout + p.stderr)
check("G14 aborted restore changed nothing",
      load_mounts("pre-missing.json") == load_mounts("post-missing.json"))

# ------------------------------------------------- H. dump file properties
print("== H. dump file properties ==")
doc = json.load(open("baseline.json"))
check("H1 format field", doc["format"] == "openbao-kvv2-dump-v1")
check("H2 address recorded", doc["address"] == os.environ["BAO_ADDR"], doc["address"])
check("H3 exported_at present", "exported_at" in doc)
check("H4 file mode 600", os.stat("baseline.json").st_mode & 0o777 == 0o600,
      oct(os.stat("baseline.json").st_mode))
tool("dump", "-o", "again.json")
tool("dump", "-o", "again2.json")
check("H5 back-to-back dumps identical", load_mounts("again.json") == load_mounts("again2.json"))

# ------------------------------------------- I. full wipe-and-restore trips
print("== I. full round trips ==")
# empty server: dump of nothing, restore of nothing
reset_mount("secret")
reset_mount("kv2b")
p = tool("dump", "-o", "empty.json")
check("I1 empty-server dump works", p.returncode == 0
      and sum(len(s) for s in load_mounts("empty.json").values()) == 0)
put("secret", "doomed1", {"k": "v"})
put("kv2b", "doomed2", {"k": "v"})
p = tool("restore", "-i", "empty.json", "--yes")
check("I2 restore from empty dump erases everything", p.returncode == 0
      and get_data("secret", "doomed1") is None and get_data("kv2b", "doomed2") is None)
# populated multi-mount wipe + restore + deep equality
for pth, d in ALL_PATHS.items():
    put("secret", pth, d)
for pth, d in VALUES.items():
    put("kv2b", pth, d)
tool("dump", "-o", "full.json")
reset_mount("secret")
reset_mount("kv2b")
tool("restore", "-i", "full.json", "--yes")
tool("dump", "-o", "full-after.json")
a, b = load_mounts("full.json"), load_mounts("full-after.json")
deep_equal = sorted(a) == sorted(b) and all(
    sorted(a[mt]) == sorted(b[mt])
    and all(a[mt][pth]["data"] == b[mt][pth]["data"]
            and a[mt][pth]["custom_metadata"] == b[mt][pth]["custom_metadata"]
            for pth in a[mt])
    for mt in a)
check("I3 multi-mount wipe + restore is deeply identical", deep_equal)

# ------------------------------- J. multiple leaves in one subtree, mixed depth
print("== J. multiple leaves under one subtree at different depths ==")
reset_mount("secret")
reset_mount("kv2b")


def nkeys(n, tag):
    return {f"{tag}_k{i:02d}": f"val-{i}" for i in range(n)}


CHAIN = {  # five leaves stacked on one chain, every node both leaf and folder
    "chain": nkeys(1, "c0"),
    "chain/a": nkeys(2, "c1"),
    "chain/a/b": nkeys(3, "c2"),
    "chain/a/b/c": nkeys(5, "c3"),
    "chain/a/b/c/d": nkeys(8, "c4"),
}
MIX = {  # siblings at assorted depths with assorted key counts
    "mix/leaf": nkeys(1, "m0"),
    "mix/sub": nkeys(4, "m1"),
    "mix/sub/leaf2": nkeys(16, "m2"),
    "mix/sub/deeper": nkeys(2, "m3"),
    "mix/sub/deeper/leaf3": nkeys(32, "m4"),
}
J_ALL = {**CHAIN, **MIX}
for pth, d in J_ALL.items():
    put("secret", pth, d)
tool("dump", "-o", "j.json")
m = load_mounts("j.json")["secret"]
check("J1 five stacked leaves on one chain all captured", all(p in m for p in CHAIN),
      [p for p in CHAIN if p not in m])
check("J2 exact key counts preserved (1/2/3/5/8 and 1/4/16/2/32)",
      all(p in m and len(m[p]["data"]) == len(d) for p, d in J_ALL.items()))
check("J3 no phantom or merged paths", sorted(m) == sorted(J_ALL), set(m) ^ set(J_ALL))
reset_mount("secret")
tool("restore", "-i", "j.json", "--yes")
check("J4 round trip identical at every depth",
      all(get_data("secret", p) == d for p, d in J_ALL.items()))

# ------------------------------------------ K. adaptive tricky path candidates
print("== K. tricky path names (adaptive: server may reject/normalize) ==")
reset_mount("secret")
reset_mount("kv2b")
K_CAND = {
    "data": {"v": "reserved1"}, "metadata": {"v": "reserved2"},
    "config": {"v": "reserved3"}, "delete": {"v": "reserved4"},
    "destroy": {"v": "reserved5"}, "undelete": {"v": "reserved6"},
    "case": {"k": "lower"}, "Case": {"k": "title"}, "CASE": {"k": "upper"},
    "k/back\\slash/x": {"v": "bs"},
    "k/.hidden/x": {"v": "hidden"},
    "k/..dots/x": {"v": "dots"},
    "k/%2Fliteral/x": {"v": "pct-enc"},
    "k/two//slashes/x": {"v": "dslash"},
    "k/trailing.dot./x": {"v": "tdot"},
    "k/tab\tseg/x": {"v": "tab"},
    "k/nl\nseg/x": {"v": "newline"},
}
K_CAND["k/" + "L" * 300 + "/x"] = {"v": "long-segment"}
k_accepted, k_rejected = {}, []
for pth, d in K_CAND.items():
    p = bao("kv", "put", f"secret/{pth}", "-", stdin=json.dumps(d))
    (k_accepted.update({pth: d}) if p.returncode == 0 else k_rejected.append(pth))
p = tool("dump", "-o", "k.json")
check("K1 dump survives every accepted tricky path", p.returncode == 0, p.stderr)
m = load_mounts("k.json")["secret"]
k_present = {pth: d for pth, d in k_accepted.items() if m.get(pth, {}).get("data") == d}
for pth in k_rejected:
    print(f"   note: server rejected: {pth!r}")
for pth in set(k_accepted) - set(k_present):
    print(f"   note: server normalized/aliased: {pth!r}")
check("K2 reserved-looking names are ordinary secrets",
      all(n in k_present for n in ["data", "metadata", "config", "delete", "destroy", "undelete"]))
check("K3 case-sensitive paths stay distinct",
      all(n in k_present for n in ["case", "Case", "CASE"])
      and len({json.dumps(m[n]["data"]) for n in ["case", "Case", "CASE"]}) == 3)
reset_mount("secret")
tool("restore", "-i", "k.json", "--yes")
check("K4 all dumped tricky paths round trip",
      all(get_data("secret", pth) == d for pth, d in k_present.items()))

# ------------------------------------------------------- L. JSON value extremes
print("== L. JSON value extremes ==")
reset_mount("secret")
reset_mount("kv2b")
deep = {"leaf": "bottom"}
for i in range(12):
    deep = {f"lvl{i}": deep}
L_VALUES = {
    "j/deep-nest-12": {"root": deep},
    "j/arrays": {"a": [[1, 2], [{"x": [True, None, "s"]}], []], "empty": []},
    "j/type-distinct": {"s42": "42", "i42": 42, "strue": "true", "btrue": True,
                        "snull": "null", "null": None, "sf": "3.14", "f": 3.14},
    "j/unicode-keys": {"日本語キー": "v1", "ключ": "v2", "🔑": "v3",
                       "key.with.dots": "v4", "key/with/slash": "v5", "key with space": "v6"},
    "j/control-chars": {"c": "bell-esc-del"},
    "j/floats": {"f16": 0.1234567890123456, "tiny": 1e-300, "huge": 1e300},
    "j/int64": {"max": 9223372036854775807, "min": -9223372036854775808},
    "j/bigint": {"big": 2 ** 80},
    "j/1mb": {"blob": "B" * 1_000_000},
    "j/empty-data": {},
}
l_accepted = []
for pth, d in L_VALUES.items():
    p = bao("kv", "put", f"secret/{pth}", "-", stdin=json.dumps(d))
    if p.returncode == 0:
        l_accepted.append(pth)
    else:
        print(f"   note: server rejected value shape: {pth!r}")
# fidelity contract: restore must reproduce exactly what the server returned
# after the original write, even where the CLI/server changed the numeric
# representation of what we sent
r0 = {pth: get_data("secret", pth) for pth in l_accepted}
tool("dump", "-o", "l.json")
reset_mount("secret")
tool("restore", "-i", "l.json", "--yes")
r1 = {pth: get_data("secret", pth) for pth in l_accepted}
check("L1 every value-extreme secret restores with full server fidelity", r0 == r1,
      [p_ for p_ in l_accepted if r0[p_] != r1[p_]])
STRICT = ["j/deep-nest-12", "j/arrays", "j/type-distinct", "j/unicode-keys",
          "j/control-chars", "j/1mb"]
check("L2 non-numeric extremes match the source exactly",
      all(pth in r1 and r1[pth] == L_VALUES[pth] for pth in STRICT),
      [p_ for p_ in STRICT if r1.get(p_) != L_VALUES[p_]])
check("L3 string-vs-number typing preserved",
      isinstance(r1["j/type-distinct"]["s42"], str)
      and not isinstance(r1["j/type-distinct"]["i42"], str)
      and isinstance(r1["j/type-distinct"]["strue"], str))
for pth in ["j/floats", "j/int64", "j/bigint"]:
    if pth in r0 and r0[pth] != L_VALUES[pth]:
        print(f"   note: numeric representation changed at write time by CLI/server: "
              f"{pth} {L_VALUES[pth]} -> {r0[pth]}")

# ------------------------------------------------------ M. mount-name edge cases
print("== M. mount-name edge cases ==")
reset_mount("secret")
reset_mount("kv2b")
reset_mount("kv.dot-mnt_1")
reset_mount("team/secrets")
put("kv.dot-mnt_1", "app", {"k": "dotted"})
put("team/secrets", "nested/app", {"k": "nested-mount"})
tool("dump", "-o", "mn.json")
mm = load_mounts("mn.json")
check("M1 dotted and nested mount names discovered",
      {"kv.dot-mnt_1", "team/secrets"} <= set(mm), sorted(mm))
reset_mount("kv.dot-mnt_1")
reset_mount("team/secrets")
tool("restore", "-i", "mn.json", "--yes")
check("M2 round trip through dotted and nested mounts",
      get_data("kv.dot-mnt_1", "app") == {"k": "dotted"}
      and get_data("team/secrets", "nested/app") == {"k": "nested-mount"})
bao("secrets", "disable", "kv.dot-mnt_1/")
bao("secrets", "disable", "team/secrets/")

# ------------------------------------------------------------- N. scale / volume
print("== N. many siblings and wide fan-out ==")
reset_mount("secret")
reset_mount("kv2b")
N_ALL = {f"flat/s{i:03d}": {"i": str(i)} for i in range(120)}
N_ALL.update({f"grid/g{a}/g{b}/g{c}": {"cell": f"{a}{b}{c}"}
              for a in range(3) for b in range(3) for c in range(3)})
for pth, d in N_ALL.items():
    put("secret", pth, d)
tool("dump", "-o", "scale.json")
m = load_mounts("scale.json")["secret"]
check("N1 all 147 secrets captured", sorted(m) == sorted(N_ALL), len(m))
reset_mount("secret")
tool("restore", "-i", "scale.json", "--yes")
tool("dump", "-o", "scale-after.json")
m2 = load_mounts("scale-after.json")["secret"]
check("N2 all 147 restored", sorted(m2) == sorted(N_ALL), len(m2))
check("N3 spot values intact",
      get_data("secret", "flat/s077") == {"i": "77"}
      and get_data("secret", "grid/g2/g1/g0") == {"cell": "210"})

# ------------------------------------------------------- O. cas_required mounts
print("== O. cas_required mounts ==")
reset_mount("casmnt")
p = bao("write", "casmnt/config", "cas_required=true")
assert p.returncode == 0, p.stderr
p = bao("kv", "put", "-cas=0", "casmnt/app", "-", stdin=json.dumps({"k": "v1"}))
assert p.returncode == 0, p.stderr
p = bao("kv", "put", "-cas=1", "casmnt/app", "-", stdin=json.dumps({"k": "v2"}))
assert p.returncode == 0, p.stderr
p = tool("dump", "-o", "cas.json")
check("O1 dump works on cas_required mount", p.returncode == 0
      and load_mounts("cas.json")["casmnt"]["app"]["data"] == {"k": "v2"})
p = tool("restore", "-i", "cas.json", "--yes")
check("O2 restore works under cas_required (overwrite in place)",
      p.returncode == 0 and get_data("casmnt", "app") == {"k": "v2"}
      and get_version("casmnt", "app") == 1, p.stderr)
bao("kv", "put", "-cas=1", "casmnt/app", "-", stdin=json.dumps({"k": "drift"}))
p = tool("restore", "-i", "cas.json", "--yes")
check("O3 restore reverts drift under cas_required",
      p.returncode == 0 and get_data("casmnt", "app") == {"k": "v2"})
bao("secrets", "disable", "casmnt/")

# ------------------------------------------------- P. limited-token partial dump
print("== P. limited-token behavior ==")
reset_mount("secret")
reset_mount("kv2b")
put("secret", "visible", {"k": "v"})
put("secret", "hidden/topsecret", {"k": "v"})
POLICY = '''
path "secret/*" { capabilities = ["create", "read", "update", "delete", "list"] }
path "secret/data/hidden/*" { capabilities = ["deny"] }
'''
p = bao("policy", "write", "limited", "-", stdin=POLICY)
assert p.returncode == 0, p.stderr
p = bao("token", "create", "-policy=limited", "-policy=default", "-format=json")
limited_token = json.loads(p.stdout)["auth"]["client_token"]
p = tool("dump", "-o", "limited.json", env={**FULL_ENV, "BAO_TOKEN": limited_token})
m = load_mounts("limited.json")["secret"]
check("P1 unreadable secrets skipped with a warning",
      p.returncode == 0 and "hidden/topsecret" not in m
      and "WARNING" in p.stdout + p.stderr, p.stderr)
check("P2 readable secrets still dumped", m.get("visible", {}).get("data") == {"k": "v"})
p = tool("dump", "-o", "fulltoken.json")
check("P3 full token still sees everything",
      "hidden/topsecret" in load_mounts("fulltoken.json")["secret"])

# ---------------------------- Q. precision limits and silent-subtree guards
print("== Q. numeric precision and silent-subtree guards ==")
import urllib.request

reset_mount("secret")
reset_mount("kv2b")


def api(method, path, body=None):
    req = urllib.request.Request(
        os.environ["BAO_ADDR"] + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Vault-Token": os.environ["BAO_TOKEN"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")


INT64_MAX = 9223372036854775807
FLOAT64_SAFE = 9007199254740991  # 2^53 - 1
api("POST", "/v1/secret/data/api-written",
    {"data": {"exact": INT64_MAX, "safe": FLOAT64_SAFE, "s": "stringval"}})
server_view = api("GET", "/v1/secret/data/api-written")["data"]["data"]
print(f"   info: server stores int64 max exactly: {server_view['exact'] == INT64_MAX}")
cli_view = get_data("secret", "api-written")
print(f"   info: bao CLI reads it back as: {cli_view['exact']}")
p = tool("dump", "-o", "precision.json")
dumped = load_mounts("precision.json")["secret"]["api-written"]["data"]
check("Q1 dump warns about numbers at/above 2^53",
      "2^53" in p.stdout + p.stderr, p.stderr)
check("Q2 numbers within float64-safe range are exact", dumped["safe"] == FLOAT64_SAFE)
check("Q3 strings alongside huge numbers unaffected", dumped["s"] == "stringval")
if dumped["exact"] != INT64_MAX:
    print(f"   documented limitation: API-written int64 max is altered by the "
          f"CLI on read: {INT64_MAX} -> {dumped['exact']!r} (warned about in Q1)")

# list-denied subtree must warn loudly (it would be deleted by a restore)
put("secret", "visible2", {"k": "v"})
put("secret", "unlistable/inner", {"k": "v"})
POLICY2 = '''
path "secret/*" { capabilities = ["create", "read", "update", "delete", "list"] }
path "secret/metadata/unlistable" { capabilities = ["deny"] }
path "secret/metadata/unlistable/*" { capabilities = ["deny"] }
path "secret/data/unlistable/*" { capabilities = ["deny"] }
'''
p = bao("policy", "write", "nolist", "-", stdin=POLICY2)
assert p.returncode == 0, p.stderr
p = bao("token", "create", "-policy=nolist", "-policy=default", "-format=json")
nolist_token = json.loads(p.stdout)["auth"]["client_token"]
p = tool("dump", "-o", "nolist.json", env={**FULL_ENV, "BAO_TOKEN": nolist_token})
m = load_mounts("nolist.json")["secret"]
check("Q4 list-denied subtree missing from dump but loudly warned",
      p.returncode == 0 and "unlistable/inner" not in m
      and "MISSING" in p.stdout + p.stderr, p.stderr)
check("Q5 listable secrets still dumped", m.get("visible2", {}).get("data") == {"k": "v"})

# ------------------------------------ R. dump-file and address guards
print("== R. dump-file and address guards ==")
reset_mount("secret")
reset_mount("kv2b")
put("secret", "guard", {"k": "v"})

# dump must never write secrets onto a path someone else pre-created
open("exists.json", "w").close()
p = tool("dump", "-o", "exists.json")
check("R1 dump refuses an existing output file",
      p.returncode != 0 and "already exists" in p.stdout + p.stderr, p.stderr)
check("R2 refused dump left the existing file untouched",
      open("exists.json").read() == "")

open("symlink-target.txt", "w").close()
if os.path.lexists("link.json"):
    os.unlink("link.json")
os.symlink("symlink-target.txt", "link.json")
p = tool("dump", "-o", "link.json")
check("R3 dump refuses to write through a symlink", p.returncode != 0, p.stderr)
check("R4 symlink target received no secrets",
      open("symlink-target.txt").read() == "")

os.makedirs("fakerepo/.git", exist_ok=True)
p = tool("dump", "-o", "fakerepo/inside.json")
check("R5 dump warns when the output is inside a git working tree",
      p.returncode == 0 and "git working tree" in p.stdout + p.stderr, p.stderr)
check("R6 dump inside a working tree still has mode 600",
      os.stat("fakerepo/inside.json").st_mode & 0o777 == 0o600,
      oct(os.stat("fakerepo/inside.json").st_mode))

# a dump taken from one server must not be silently applied to another
tool("dump", "-o", "addr.json")
doc = json.load(open("addr.json"))
doc["address"] = "https://some-other-server.example:8200"
with open("addrmismatch.json", "w") as f:
    json.dump(doc, f)
p = tool("restore", "-i", "addrmismatch.json", "--yes")
check("R7 restore refuses a dump from a different server",
      p.returncode != 0 and "some-other-server.example" in p.stdout + p.stderr,
      p.stdout + p.stderr)
check("R8 refused restore changed nothing", get_data("secret", "guard") == {"k": "v"})
p = tool("restore", "-i", "addrmismatch.json", "--dry-run",
         "--allow-address-mismatch")
check("R9 --allow-address-mismatch permits the cross-server restore",
      p.returncode == 0, p.stdout + p.stderr)

# cleartext transport must be called out, but not for the dev-server aliases
p = tool("list", env={**FULL_ENV, "BAO_ADDR": "http://elsewhere.example:8200"})
check("R10 non-https BAO_ADDR warns about cleartext",
      "cleartext" in p.stdout + p.stderr, p.stderr)
p = tool("list")
check("R11 no cleartext warning for the dev-server alias",
      "cleartext" not in p.stdout + p.stderr, p.stderr)

# -------------------------------------------------------------------- report
print()
failed = [n for n, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  ")
    sys.exit(1)
print("ALL TESTS PASSED")
