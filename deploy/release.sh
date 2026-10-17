#!/bin/bash
# Herald release procedure (optiplex). Run from a clean checkout of ~/src/herald.
#
#   deploy/release.sh build             build /usr/local/lib/herald/<release> from HEAD and test it
#   deploy/release.sh switch <release>  checkpoint, then point the units at <release> and enable timers
#
# `build` changes no consumer. `switch` writes a protected checkpoint with the
# previous units, environments and database first, plus a rollback.sh that
# restores them (the new database is left in place, unused).
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
LIB=/usr/local/lib/herald
UNITS="herald-web.service herald-collect.service herald-collect.timer herald-digest-morning.service herald-digest-morning.timer herald-digest-afternoon.service herald-digest-afternoon.timer"
TIMERS="herald-collect.timer herald-digest-morning.timer herald-digest-afternoon.timer"
NEW_DB=/var/lib/herald/herald-feed.db

build() {
  cd "$SRC"
  [ -z "$(git status --porcelain)" ] || { echo "worktree not clean" >&2; exit 1; }
  local sha n rel stage
  sha=$(git rev-parse --short=12 HEAD)
  n=1
  while [ -e "$LIB/prod-$(date -u +%Y%m%d).$n-g$sha" ]; do n=$((n + 1)); done
  rel="prod-$(date -u +%Y%m%d).$n-g$sha"
  stage=$(mktemp -d)
  git archive HEAD | tar -x -C "$stage"
  rm -rf "$stage/eval" "$stage/deploy"
  sudo install -d -o root -g root -m 0755 "$LIB/$rel"
  sudo cp -a "$stage/." "$LIB/$rel/"
  sudo python3 -m venv "$LIB/$rel/venv"
  sudo "$LIB/$rel/venv/bin/pip" install --quiet --no-cache-dir --require-virtualenv -r "$LIB/$rel/requirements.lock"
  sudo chown -R root:root "$LIB/$rel"
  sudo chmod -R u=rwX,go=rX "$LIB/$rel"
  rm -rf "$stage"
  # Tests run against the release itself, with a throwaway database and no network.
  (cd /tmp && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$LIB/$rel" "$LIB/$rel/venv/bin/python" -m unittest -q test_herald)
  git tag -f "production/$rel" HEAD >/dev/null
  echo "$rel"
}

switch() {
  local rel=$1 old ts cp
  [ -d "$LIB/$rel" ] || { echo "no such release $rel" >&2; exit 1; }
  old=$(systemctl show -p WorkingDirectory --value herald-web.service)
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  cp=/data/security-backups/herald-feed-digest-$ts
  sudo install -d -m 0700 "$cp"
  for u in $UNITS; do sudo cp -a "/etc/systemd/system/$u" "$cp/$u.before"; done
  sudo cp -a /etc/herald/web.env "$cp/web.env.before"
  sudo cp -a /etc/herald/worker.env "$cp/worker.env.before"
  sudo sqlite3 -readonly /var/lib/herald/herald.db ".backup $cp/herald.db.before"
  git -C "$SRC" bundle create /tmp/herald-$ts.bundle --all >/dev/null 2>&1 && sudo mv /tmp/herald-$ts.bundle "$cp/source.bundle"
  sudo tee "$cp/rollback.sh" >/dev/null <<EOF
#!/bin/bash
# Return Herald to the units/environments in use before $ts ($old).
set -euo pipefail
systemctl disable --now $TIMERS || true
for u in $UNITS; do cp -a "$cp/\$u.before" "/etc/systemd/system/\$u"; done
cp -a "$cp/web.env.before" /etc/herald/web.env
cp -a "$cp/worker.env.before" /etc/herald/worker.env
systemctl daemon-reload
systemctl restart herald-web.service
systemctl is-active herald-web.service
EOF
  sudo chmod 0700 "$cp/rollback.sh"
  echo "checkpoint: $cp"

  for u in $UNITS; do
    sed "s/@RELEASE@/$rel/g" "$SRC/deploy/systemd/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
  done
  # Point both identities at the new database; give the worker the NanoGPT key
  # from the operator's private .env without printing it.
  sudo sed -i "s|^HERALD_DB_PATH=.*|HERALD_DB_PATH=$NEW_DB|" /etc/herald/web.env /etc/herald/worker.env
  sudo sed -i '/^HERALD_REVIEW_PASSWORD=/d' /etc/herald/web.env
  sudo sed -i '/^NANOGPT_API_KEY=/d' /etc/herald/worker.env
  grep '^NANOGPT_API_KEY=' "$SRC/.env" | sudo tee -a /etc/herald/worker.env >/dev/null
  sudo systemctl daemon-reload
  sudo systemd-analyze verify $(for u in $UNITS; do echo /etc/systemd/system/$u; done)
  sudo -u herald-worker -g herald sh -c "umask 0007; HERALD_DB_PATH=$NEW_DB python3 -c \"import sys; sys.path.insert(0, '$LIB/$rel'); import herald_store; herald_store.connect()\""
  sudo systemctl restart herald-web.service
  sleep 2
  curl -fsS -o /dev/null -w "web %{http_code}\n" http://127.0.0.1:8060/
  sudo systemctl enable --now $TIMERS
  systemctl list-timers 'herald-*' --no-pager
  echo "rollback: sudo $cp/rollback.sh"
}

case "${1:-}" in
  build) build ;;
  switch) switch "${2:?release}" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
