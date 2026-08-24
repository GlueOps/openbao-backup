#!/bin/sh
# Cross-version compatibility check. Only local dependency: the docker CLI.
#
# For each OpenBao server version, restores a dump into a fresh dev server,
# dumps it back, and verifies the content is identical to the checked-in
# golden fixture. Each version restores the DUMP PRODUCED BY THE PREVIOUS
# VERSION (the first restores the golden itself), which simulates restoring
# a pre-upgrade backup after a server upgrade.
#
#   VERSIONS="2.4.4 2.6.1" test/cross-version.sh   # override the matrix
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
VERSIONS=${VERSIONS:-"2.4.4 2.5.5 2.6.1"}
NET="baokv-xver-$$"
SRV="baokv-xver-srv-$$"
WORKVOL="$DIR/test/.xver-work"

cleanup() {
    docker rm -f "$SRV" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$WORKVOL"
}
trap cleanup EXIT INT TERM

echo "# building baokv image ..."
docker build -q -t baokv "$DIR" >/dev/null
docker network create "$NET" >/dev/null
rm -rf "$WORKVOL" && mkdir -p "$WORKVOL"
cp "$DIR/test/fixtures/golden.json" "$WORKVOL/golden.json"

PREV=golden.json
for V in $VERSIONS; do
    echo "# --- server openbao/openbao:$V (restoring from $PREV) ---"
    docker pull -q "openbao/openbao:$V" >/dev/null
    docker rm -f "$SRV" >/dev/null 2>&1 || true
    docker run -d --name "$SRV" --network "$NET" --network-alias openbao \
        -e VAULT_DEV_ROOT_TOKEN_ID=test-root-token \
        -e BAO_DEV_ROOT_TOKEN_ID=test-root-token \
        -e VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
        -e BAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
        "openbao/openbao:$V" >/dev/null
    i=0
    until docker run --rm --network "$NET" --entrypoint bao \
            -e VAULT_ADDR=http://openbao:8200 baokv status >/dev/null 2>&1; do
        i=$((i+1))
        [ "$i" -gt 30 ] && { echo "server $V did not become ready" >&2; exit 1; }
        sleep 1
    done
    docker run --rm --network "$NET" \
        --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -e BAO_ADDR=http://openbao:8200 -e BAO_TOKEN=test-root-token \
        -e BAO_COOKIE=dummy-test-cookie \
        -v "$DIR/test:/test:ro" -v "$WORKVOL:/xwork" -w /xwork \
        --entrypoint python3 baokv \
        /test/golden_check.py /xwork/golden.json "/xwork/dump-$V.json" "/xwork/$PREV"
    PREV="dump-$V.json"
done
echo "# cross-version golden checks passed for: $VERSIONS"
