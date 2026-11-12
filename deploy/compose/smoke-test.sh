#!/usr/bin/env bash
# Builds the image, runs the stack of docker-compose.yml (local accounts) on this machine with
# throw-away data, and checks from outside what docs/exploitation.md promises: who gets in,
# that an identity cannot be forged, the Live mode, then a backup and a restore.
#
# Run by the CI on every push. For a machine that does NOT already run imageBio: it uses its
# own project name, directories and ports, and removes everything when it ends.
set -euo pipefail
cd "$(dirname "$0")"

host=imagebio.test
port=${SMOKE_HTTPS_PORT:-8443}
password="correct horse battery staple"
project=imagebio-smoke
work=$(mktemp -d)
env_file=$work/env
base=https://$host:$port
config=/etc/imagebio/imagebio.toml

if [ -e caddy/users ]; then
    echo "caddy/users exists: this machine seems to run imageBio, not touching it" >&2
    exit 2
fi

dc() { docker compose --project-name "$project" --env-file "$env_file" -f docker-compose.yml "$@"; }

finish() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "── FAILED: last lines of the services ──" >&2
        dc logs --tail 60 >&2 || true
    fi
    dc down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -f caddy/users
    rm -rf "$work" 2>/dev/null || true
    exit "$status"
}
trap finish EXIT

# curl through the proxy: the name resolves to this machine, the certificate is Caddy's own
through() { curl --silent --show-error --insecure --max-time 20 --resolve "$host:$port:127.0.0.1" "$@"; }
status_of() { through --output /dev/null --write-out '%{http_code}' "$@" || true; }
as_alice() { through --user "alice:$password" "$@"; }

expect() { # expect <wanted status> <what is checked> <curl arguments...>
    local wanted=$1 what=$2 got
    shift 2
    got=$(status_of "$@")
    if [ "$got" != "$wanted" ]; then
        echo "FAILED  $what: status $got, expected $wanted" >&2
        exit 1
    fi
    echo "ok      $what ($got)"
}

field() { python3 -c 'import json, sys; print(eval(sys.argv[1], {"d": json.load(sys.stdin)}))' "$1"; }

echo "── configuration ──"
mkdir -p "$work/data" "$work/state" "$work/backups"
metrics_token=$(openssl rand -hex 16)
cat >"$env_file" <<END
IMAGEBIO_VERSION=smoke
IMAGEBIO_HOST=$host
IMAGEBIO_TLS=internal
IMAGEBIO_HTTP_PORT=${SMOKE_HTTP_PORT:-8080}
IMAGEBIO_HTTPS_PORT=$port
IMAGEBIO_UID=$(id -u)
IMAGEBIO_GID=$(id -g)
IMAGEBIO_DATA=$work/data
IMAGEBIO_STATE=$work/state
IMAGEBIO_BACKUPS=$work/backups
IMAGEBIO_PROXY_SECRET=$(openssl rand -hex 32)
IMAGEBIO_METRICS_TOKEN=$metrics_token
OAUTH2_PROXY_VERSION=v7.8.1
OIDC_ISSUER_URL=https://sso.invalid/realms/lab
OIDC_CLIENT_ID=imagebio
OIDC_CLIENT_SECRET=not-a-secret
OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -hex 16)
END
dc config --quiet
# the single sign-on stack cannot run without an identity provider: only its syntax is checked
docker compose --project-name "$project" --env-file "$env_file" \
    -f docker-compose.yml -f docker-compose.sso.yml config --quiet
echo "ok      both compose files are accepted"

echo "── image ──"
dc build --quiet imagebio
printf 'alice %s\n' "$(docker run --rm caddy:2 caddy hash-password --plaintext "$password")" >caddy/users
# three exposures written by the simulated microscope, as the account of the server
docker run --rm --user "$(id -u):$(id -g)" --volume "$work/data:/out" imagebio:smoke \
    simulate /out/session --count 3 --rate 36000 --size 256 >/dev/null
echo "ok      image built, a simulated session written"

echo "── stack ──"
dc up --detach --wait --wait-timeout 180
for _ in $(seq 60); do
    [ "$(status_of "$base/healthz")" = 200 ] && break
    sleep 1
done
expect 200 "the health check is open" "$base/healthz"
expect 401 "nobody enters without an account" "$base/api/me"
expect 401 "nor with a wrong password" --user "alice:wrong" "$base/api/me"
expect 401 "the page itself asks for an account" "$base/"
expect 200 "an account enters" --user "alice:$password" "$base/api/me"
expect 200 "and gets the interface" --user "alice:$password" "$base/"
expect 404 "monitoring is not served through the proxy" --user "alice:$password" "$base/metrics"

who=$(as_alice -H "X-Forwarded-User: mallory" -H "X-Forwarded-Groups: admin" \
    -H "X-Imagebio-Proxy-Secret: guess" "$base/api/me" | field '(d["user"], d["groups"])')
[ "$who" = "('alice', [])" ] || { echo "FAILED  a forged identity got through: $who" >&2; exit 1; }
echo "ok      identity headers sent by a browser are replaced: still $who"

# straight to the container, around the proxy, as any account of this machine could try
for address in $(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$(dc ps --quiet imagebio)"); do
    got=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
        -H "X-Forwarded-User: alice" "http://$address:8000/api/me" || true)
    case $got in
    401 | 000) echo "ok      around the proxy, $address answers $got: no secret, no identity" ;;
    *) echo "FAILED  around the proxy, $address believed a forged identity ($got)" >&2 && exit 1 ;;
    esac
done

inside=$(dc exec -T imagebio python -c "
import urllib.request
r = urllib.request.Request('http://127.0.0.1:8000/metrics', headers={'Authorization': 'Bearer $metrics_token'})
print(sum(line.startswith('imagebio_') for line in urllib.request.urlopen(r, timeout=5).read().decode().splitlines()))")
[ "$inside" -gt 5 ] || { echo "FAILED  /metrics from inside: $inside lines" >&2; exit 1; }
echo "ok      monitoring answers the bearer of the token from inside ($inside figures)"

echo "── data and Live mode ──"
as_alice "$base/api/fs/list?path=microscope/session" | grep -q "Images-Disc1" ||
    { echo "FAILED  the data are not listed" >&2; exit 1; }
echo "ok      the data mounted read-only are listed"
watch=$(as_alice --request POST "$base/api/live/watches?path=microscope/session" | field 'd["id"]')
acquired=0
for _ in $(seq 90); do
    acquired=$(as_alice "$base/api/live/watches/$watch" | field 'd["acquired"]')
    [ "$acquired" -ge 3 ] && break
    sleep 1
done
[ "$acquired" -ge 3 ] || { echo "FAILED  the Live mode found $acquired exposure(s) of 3" >&2; exit 1; }
echo "ok      the Live mode indexed the $acquired exposures of the session"

echo "── personal keys ──"
made_key=$(as_alice --request POST -H "Content-Type: application/json" \
    --data '{"name": "smoke", "days": 1}' "$base/api/me/keys")
key=$(printf '%s' "$made_key" | field 'd["key"]')
key_id=$(printf '%s' "$made_key" | field 'd["id"]')
by_key() { through -H "Authorization: Bearer $key" "$@"; }
expect 200 "a personal key reads without a password" -H "Authorization: Bearer $key" "$base/api/me"
seen=$(by_key "$base/api/live/watches/$watch" | field 'd["acquired"]')
[ "$seen" -ge 3 ] || { echo "FAILED  the key does not see alice's watch: $seen" >&2; exit 1; }
echo "ok      with the rights of its owner: alice's watch, $seen exposures"
who=$(by_key -H "X-Forwarded-User: mallory" -H "X-Forwarded-Groups: admin" "$base/api/me" |
    field '(d["user"], d["groups"], d["admin"])')
[ "$who" = "('alice', [], False)" ] || { echo "FAILED  a forged identity beside a key: $who" >&2; exit 1; }
echo "ok      identity headers sent beside a key are removed: still $who"
expect 401 "a wrong key is refused" -H "Authorization: Bearer ibk_${key:4:20}wrong" "$base/api/me"
expect 403 "a key only reads" --request POST -H "Authorization: Bearer $key" \
    "$base/api/live/watches?path=microscope/session"
expect 404 "monitoring is not served to a key either" -H "Authorization: Bearer $key" "$base/metrics"
expect 401 "a key opens nothing but the API" -H "Authorization: Bearer $key" "$base/"
by_key "$base/api/ai/imagebio_ai.py" >"$work/imagebio_ai.py"
grep -q "^def serve_mcp" "$work/imagebio_ai.py" ||
    { echo "FAILED  the client for assistants is not served" >&2; exit 1; }
echo "ok      the client for assistants is served to users"
for address in $(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$(dc ps --quiet imagebio)"); do
    got=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
        -H "Authorization: Bearer $key" "http://$address:8000/api/me" || true)
    case $got in
    401 | 000) echo "ok      around the proxy, a key alone gets $got from $address: no proxy secret" ;;
    *) echo "FAILED  around the proxy, $address accepted a key without the proxy secret ($got)" >&2 && exit 1 ;;
    esac
done
expect 200 "its owner revokes it" --user "alice:$password" --request DELETE "$base/api/me/keys/$key_id"
expect 401 "a revoked key is refused at once" -H "Authorization: Bearer $key" "$base/api/me"

echo "── backup, loss, restore ──"
made=$(dc exec -T imagebio imagebio backup /backups --config "$config" --keep 14 | sed -n 's/^backup written to //p')
[ -f "$work/backups/$(basename "$made")/manifest.json" ] || { echo "FAILED  no backup written" >&2; exit 1; }
if dc exec -T imagebio imagebio restore "$made" --config "$config" --force 2>/dev/null; then
    echo "FAILED  a restore was accepted under a running server" >&2
    exit 1
fi
echo "ok      backup written while the server runs; a restore is refused meanwhile"
dc stop imagebio >/dev/null 2>&1
rm -f "$work"/state/live.sqlite*
dc run --rm -T imagebio restore "$made" --config "$config"
dc up --detach --wait --wait-timeout 120 imagebio
for _ in $(seq 30); do
    [ "$(status_of --user "alice:$password" "$base/api/me")" = 200 ] && break
    sleep 1
done
back=$(as_alice "$base/api/live/watches" | field '[(w["id"], w["path"]) for w in d["watches"]]')
[ "$back" = "[($watch, 'microscope/session')]" ] || { echo "FAILED  after the restore: $back" >&2; exit 1; }
again=$(as_alice "$base/api/live/watches/$watch" | field 'd["acquired"]')
[ "$again" -ge 3 ] || { echo "FAILED  after the restore: $again exposure(s)" >&2; exit 1; }
echo "ok      after losing its index and a restore, the server shows the watch and its $again exposures"
echo "── all checks passed ──"
