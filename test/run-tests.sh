#!/bin/sh
# Hermetic e2e test suite. Only local dependency: the docker CLI.
# Spins up a throwaway OpenBao dev server in docker, runs the test suite
# inside the baokv image against it, and tears everything down.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
NET="baokv-test-$$"
SRV="baokv-test-server-$$"
SERVER_IMAGE=${SERVER_IMAGE:-openbao/openbao:2.4.4}

cleanup() {
    docker rm -f "$SRV" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "# building baokv image ..."
docker build -q -t baokv "$DIR" >/dev/null

echo "# starting disposable OpenBao dev server ($SERVER_IMAGE) ..."
docker network create "$NET" >/dev/null
docker run -d --name "$SRV" --network "$NET" --network-alias openbao \
    -e VAULT_DEV_ROOT_TOKEN_ID=test-root-token \
    -e BAO_DEV_ROOT_TOKEN_ID=test-root-token \
    -e VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
    -e BAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
    "$SERVER_IMAGE" >/dev/null

i=0
until docker run --rm --network "$NET" --entrypoint bao \
        -e VAULT_ADDR=http://openbao:8200 baokv status >/dev/null 2>&1; do
    i=$((i+1))
    if [ "$i" -gt 30 ]; then
        echo "dev server did not become ready" >&2
        docker logs "$SRV" | tail -5 >&2
        exit 1
    fi
    sleep 1
done

echo "# running e2e suite ..."
docker run --rm --network "$NET" \
    -e BAO_ADDR=http://openbao:8200 \
    -e BAO_TOKEN=test-root-token \
    -e BAO_COOKIE=dummy-test-cookie \
    -e VAULT_ADDR=http://openbao:8200 \
    -e VAULT_TOKEN=test-root-token \
    -v "$DIR/test:/test:ro" \
    --entrypoint python3 baokv /test/e2e_test.py
