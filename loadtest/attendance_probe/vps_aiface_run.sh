#!/usr/bin/env bash
# AI Face attendance + durable notification outbox — VPS-ISOLATED LOAD VALIDATION
# on the PRESERVED 10-school × 1,000-student dataset of attlt-20260917-7b7cb5-vps.
#
#   bash vps_aiface_run.sh --preflight   # read-only: changes nothing, exits non-zero if blocked
#   bash vps_aiface_run.sh --run         # the whole round, end to end (~15-25 min incl. venv build)
#   bash vps_aiface_run.sh --status      # progress while it runs (second console)
#   bash vps_aiface_run.sh --digest      # compact result block to copy back
#
# Tested source: ca2a6a2 = production main (ac635af, durable AI Face outbox)
# + ONE commit, the explicit AI Face school-isolation guard. It is imported into
# an EXPERIMENT-OWNED bare clone from aifx/aifx.bundle; /var/www/mecha-school is
# only read. No Sync Foundation code is part of it (the runner verifies the
# exact two-file diff and the tree hash).
#
# Production is never written: no service restart, no .env / systemd / drop-in
# change, no firewall/routing change, no deploy, no port 7788, no real Firebase,
# no production database. prod_pre/prod_post snapshots prove it (hashes + pids).
#
# Isolation: the app, worker, PostgreSQL, driver and in-namespace watchdog run
# as the unprivileged `attlt` account inside ONE ephemeral `unshare --net`
# namespace (loopback only; no veth, no persistent netns). A host-side sentinel
# (production health + host CPU/memory/disk) runs outside it. Root is used only
# for the namespace, runuser/nsenter and read-only production probes.
set -uo pipefail

BASE="/srv/attlt"
TEST_USER="attlt"
PRESERVED="$BASE/attlt-20260917-7b7cb5-vps"
PGDATA="$PRESERVED/pgdata"
RUN_ID="aifx-20260925"
ROOT="$BASE/attlt-$RUN_ID"
SRC_REPO="$BASE/src-$RUN_ID"                       # experiment-owned bare clone
PROD_REPO="/var/www/mecha-school"                  # read-only, never modified
BASE_SHA="ac635aff39b58c67b9d9d9cf515c870e97aaf6db"   # production main: durable AI Face outbox
TESTED_SHA="ca2a6a2bd22bf594d29326d49eee212f8a359605" # + AI Face school-isolation guard
TESTED_TREE="adf7ea16eb719282611b67b064bd9be19fdbd9a5"
EXPECTED_DIFF="app/services/ai_face_ws.py tests/test_aiface_attendance_outbox.py "
ALLOWED_MIGRATIONS="a9t8n9d0s1c2,d1o2c3s4d5e6,e3x4m5g6r7p8,h7w8i9g0r1p2,i1n2s3t4t5y6,j1s2h3l4t5n6,k1n2s3t4g5r6,n0tb0x1a2b3c"
HEALTH_URL="https://school.smartcoreiq.cloud/ops/health"
# Local container rehearsal only (never set on the VPS): a stub health endpoint.
if [ -n "${ATTLT_REHEARSAL_HEALTH_URL:-}" ]; then
  HEALTH_URL="$ATTLT_REHEARSAL_HEALTH_URL"
  echo "!!! REHEARSAL: production health URL overridden — this is NOT a VPS run !!!" >&2
fi
PGBIN="/srv/attlt-pkg/pg-runtime/root/usr/lib/postgresql/16/bin"
PGLIB="/srv/attlt-pkg/pg-runtime/root/usr/lib/x86_64-linux-gnu:/srv/attlt-pkg/pg-runtime/root/usr/lib/postgresql/16/lib"
HTTP_PORT=18180
WS_PORT=18188
PG_PORT=55480
PROD_WS_PORT=7788
MIN_MEM_PCT=20
TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$TOOL/aifx/aifx.bundle"
RUNLOG="$BASE/runner-$RUN_ID.log"
GENPY="$ROOT/venv-gen/bin/python"
TGTPY="$ROOT/venv-target/bin/python"
OUT="$ROOT/results/aiface_load"

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { printf '[%s] FATAL: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; STOP_REASON="$*"; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (namespace, runuser/nsenter, read-only production probes)"; }
git_ro() { env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*" \
             GIT_OPTIONAL_LOCKS=0 git "$@"; }

# Minimal, explicit environment for every experiment process: nothing from
# root's shell (no DATABASE_URL, no credentials) can leak into it.
CLEAN_ENV=(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$BASE/home" LANG=C.UTF-8 LC_ALL=C.UTF-8
           LD_LIBRARY_PATH="$PGLIB" ATTLT_PG_BIN="$PGBIN" GIT_CONFIG_COUNT=1
           GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*" GIT_OPTIONAL_LOCKS=0)
as_user() { runuser -u "$TEST_USER" -- "${CLEAN_ENV[@]}" "$@"; }
in_ns()   { nsenter --net="/proc/$NSPID/ns/net" -- runuser -u "$TEST_USER" -- "${CLEAN_ENV[@]}" "$@"; }
in_ns_root() { nsenter --net="/proc/$NSPID/ns/net" -- "$@"; }
pgctl_ns() { in_ns "$PGBIN/pg_ctl" "$@"; }
controldata() { env LD_LIBRARY_PATH="$PGLIB" "$PGBIN/pg_controldata" "$1" 2>/dev/null; }
cluster_state() { controldata "$1" | awk -F': *' '/^Database cluster state/{print $2}'; }
cluster_sysid() { controldata "$1" | awk -F': *' '/^Database system identifier/{print $2}'; }
listening_on_host() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
mins_to_midnight() { TZ=Asia/Baghdad date +'%H %M' | awk '{print (23-$1)*60 + (60-$2)}'; }

# ── preflight ────────────────────────────────────────────────────────────────
preflight() {
  local fail=0 warn=0
  echo "=== AI Face outbox VPS round — preflight (read-only; nothing is created or changed) ==="
  [ "$(id -u)" -eq 0 ] && echo "  root: yes" || { echo "  root: NO"; fail=$((fail+1)); }

  echo "  --- host resources ---"
  local mem; mem=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.2f",100*a/t}' /proc/meminfo)
  if awk "BEGIN{exit !($mem >= $MIN_MEM_PCT)}"; then echo "  MemAvailable: ${mem}% (floor ${MIN_MEM_PCT}%)"
  else echo "  MemAvailable: ${mem}% BELOW the ${MIN_MEM_PCT}% floor"; fail=$((fail+1)); fi
  local cpu; cpu=$(awk '/^cpu /{i=$5+$6; t=0; for(k=2;k<=NF;k++) t+=$k; print i, t}' /proc/stat); sleep 3
  local cpu2; cpu2=$(awk '/^cpu /{i=$5+$6; t=0; for(k=2;k<=NF;k++) t+=$k; print i, t}' /proc/stat)
  echo "$cpu $cpu2" | awk '{printf "  host CPU (3 s sample): %.1f%% busy, %d cores\n", 100*(1-($3-$1)/($4-$2)), '"$(nproc)"'}'
  local pg_mb; pg_mb=$(du -sm "$PGDATA" 2>/dev/null | cut -f1)
  local free_gb; free_gb=$(df -PBG "$BASE" | awk 'NR==2{gsub("G","",$4);print $4}')
  local need_gb=$(( ${pg_mb:-0} / 1024 + 12 ))
  if [ "${free_gb:-0}" -ge "$need_gb" ]; then echo "  free disk on $BASE: ${free_gb} GiB (need >= ${need_gb}: cold backup ${pg_mb:-?} MiB + 12 GiB headroom)"
  else echo "  free disk on $BASE: ${free_gb:-?} GiB — need >= ${need_gb}"; fail=$((fail+1)); fi

  echo "  --- preserved experiment ---"
  for f in "$PRESERVED/experiment.json" "$PRESERVED/secrets/secrets.json" "$PRESERVED/run/fixtures.json" "$PRESERVED/.attlt_owner"; do
    [ -f "$f" ] && echo "  present: ${f#$PRESERVED/}" || { echo "  MISSING: $f"; fail=$((fail+1)); }
  done
  if [ -d "$PGDATA" ]; then
    echo "  pgdata owner: $(stat -c '%U %a' "$PGDATA")"
    [ "$(stat -c %U "$PGDATA")" = "$TEST_USER" ] || { echo "  pgdata is not owned by $TEST_USER"; fail=$((fail+1)); }
    [ -e "$PGDATA/postmaster.pid" ] && { echo "  postmaster.pid EXISTS — the preserved cluster may be running"; fail=$((fail+1)); }
    local st; st=$(cluster_state "$PGDATA")
    [ "$st" = "shut down" ] && echo "  cluster state: shut down" || { echo "  cluster state: '${st:-unreadable}' (need 'shut down')"; fail=$((fail+1)); }
    echo "  pg_control system identifier: $(cluster_sysid "$PGDATA")"
    grep -E "^(listen_addresses|port|unix_socket_directories)" "$PGDATA/postgresql.auto.conf" | sed 's/^/    auto.conf: /'
    grep -q "^port = $PG_PORT" "$PGDATA/postgresql.auto.conf" || { echo "  auto.conf port is not $PG_PORT"; fail=$((fail+1)); }
    grep -q "^unix_socket_directories = ''" "$PGDATA/postgresql.auto.conf" || { echo "  cluster is not TCP-only"; fail=$((fail+1)); }
  else
    echo "  pgdata MISSING at $PGDATA"; fail=$((fail+1))
  fi
  python3 - "$PRESERVED" <<'PY' || fail=$((fail+1))
import json, sys
p = sys.argv[1]
c = json.load(open(f'{p}/experiment.json'))
s = json.load(open(f'{p}/secrets/secrets.json'))
tag = c['experiment_id'].rsplit('-', 1)[-1]
ok = (c['pg_port'] == 55480 and c['pg_host'] == '127.0.0.1' and c['db_name'] == 'core_school_attendance_load_test'
      and (c['num_schools'], c['students_per_school'], c['devices_per_school']) == (10, 1000, 2)
      and s.get('pg_user') == f'attlt_{tag}')
print(f"  experiment_id={c['experiment_id']} tag={tag} db={c['pg_host']}:{c['pg_port']}/{c['db_name']} "
      f"role={s.get('pg_user')} shape={c['num_schools']}x{c['students_per_school']}x{c['devices_per_school']}")
sys.exit(0 if ok else 1)
PY

  echo "  --- this run ---"
  [ -e "$ROOT" ] && { echo "  run root $ROOT ALREADY EXISTS — refusing to reuse; move it aside"; fail=$((fail+1)); } || echo "  run root $ROOT: free"
  [ -e "$SRC_REPO" ] && { echo "  $SRC_REPO ALREADY EXISTS — refusing"; fail=$((fail+1)); } || echo "  source repo $SRC_REPO: free"
  for p in $HTTP_PORT $WS_PORT $PG_PORT; do
    listening_on_host "$p" && { echo "  port $p: IN USE on the host"; fail=$((fail+1)); } || echo "  port $p: free on the host"
  done
  listening_on_host "$PROD_WS_PORT" && echo "  production AI Face port $PROD_WS_PORT: listening (production; never touched)" \
                                   || echo "  production AI Face port $PROD_WS_PORT: not listening"

  echo "  --- portable PostgreSQL runtime ---"
  if [ -x "$PGBIN/pg_ctl" ] && [ -x "$PGBIN/postgres" ] && [ -x "$PGBIN/pg_controldata" ]; then
    echo "  $(env LD_LIBRARY_PATH="$PGLIB" "$PGBIN/postgres" --version 2>&1 | head -1)"
  else echo "  PostgreSQL binaries MISSING under $PGBIN"; fail=$((fail+1)); fi

  echo "  --- tested source ---"
  if [ -f "$BUNDLE" ]; then
    echo "  bundle: $(stat -c%s "$BUNDLE") bytes"; git_ro bundle list-heads "$BUNDLE" 2>/dev/null | sed 's/^/    head: /'
  else echo "  bundle MISSING at $BUNDLE"; fail=$((fail+1)); fi
  if git_ro -C "$PROD_REPO" cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "  base $BASE_SHA present in $PROD_REPO (read-only)"
    echo "  production checkout HEAD: $(git_ro -C "$PROD_REPO" rev-parse HEAD 2>/dev/null)"
  else echo "  base $BASE_SHA NOT in $PROD_REPO — this bundle cannot be applied"; fail=$((fail+1)); fi

  echo "  --- tools ---"
  for t in unshare nsenter runuser ss git python3 curl tar sha256sum; do
    command -v "$t" >/dev/null && echo "  $t: ok" || { echo "  $t: MISSING"; fail=$((fail+1)); }
  done
  python3 -c 'import venv, ensurepip' 2>/dev/null && echo "  python3 $(python3 -V 2>&1 | cut -d' ' -f2) venv+ensurepip: ok" || { echo "  python3 venv/ensurepip: MISSING"; fail=$((fail+1)); }
  id "$TEST_USER" >/dev/null 2>&1 && echo "  account $TEST_USER: exists" || { echo "  account $TEST_USER: MISSING"; fail=$((fail+1)); }
  local nsout; nsout=$(unshare --net -- sh -c 'ip link set lo up 2>/dev/null; cat /proc/net/dev | tail -n +3 | wc -l' 2>/dev/null)
  [ "${nsout:-0}" = "1" ] && echo "  unshare --net: ok (1 interface: lo)" || { echo "  unshare --net: FAILED (${nsout:-none})"; fail=$((fail+1)); }
  if unshare --net -- timeout 6 curl -sS -o /dev/null -m 4 "$HEALTH_URL" 2>/dev/null; then
    echo "  namespace isolation: FAILED — production reachable from a fresh namespace"; fail=$((fail+1))
  else echo "  namespace isolation: production NOT reachable from a fresh namespace (expected)"; fi
  local code; code=$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)
  [ "$code" = "200" ] && echo "  production health: 200" || { echo "  production health: $code"; fail=$((fail+1)); }
  code=$(curl -sS -o /dev/null -m 10 -w '%{http_code}' https://pypi.org/simple/ 2>/dev/null)
  [ "$code" = "200" ] && echo "  pypi reachable (venv build, outside the namespace): 200" || { echo "  pypi: $code"; fail=$((fail+1)); }
  local mins; mins=$(mins_to_midnight)
  [ "$mins" -ge 90 ] && echo "  Asia/Baghdad midnight in ${mins} min (>= 90)" || { echo "  Asia/Baghdad midnight in ${mins} min — WAIT (test dates would shift)"; fail=$((fail+1)); }
  echo "=== preflight: $fail blocking problem(s), $warn warning(s) ==="
  [ "$fail" -eq 0 ] || exit 2
}

# ── namespace lifecycle ──────────────────────────────────────────────────────
start_namespace() {
  unshare --net -- bash -c 'ip link set lo up && exec sleep 10800' >/dev/null 2>&1 &
  NSPID=$!
  sleep 1
  kill -0 "$NSPID" 2>/dev/null || die "could not create the private network namespace"
  [ "$(readlink /proc/$NSPID/ns/net)" != "$(readlink /proc/self/ns/net)" ] \
    || die "the namespace holder shares the host network namespace"
  echo "$NSPID" > "$ROOT/run/netns_holder.pid"
  log "private network namespace holder pid=$NSPID"
}
stop_namespace() {
  [ -n "${NSPID:-}" ] || return 0
  kill "$NSPID" 2>/dev/null; sleep 1; kill -9 "$NSPID" 2>/dev/null
  log "network namespace released"; NSPID=""
}

# Stops ONLY experiment processes, identified by pid files / the attlt account.
stop_experiment() {
  if [ -n "${NSPID:-}" ] && kill -0 "$NSPID" 2>/dev/null; then
    [ -f "$ROOT/run/worker.json" ] && in_ns "$GENPY" "$TOOL/worker_control.py" stop --root "$ROOT" >/dev/null 2>&1
    [ -f "$ROOT/run/target.json" ] && in_ns "$GENPY" "$TOOL/target.py" stop --root "$ROOT" >/dev/null 2>&1
    if [ -e "$PGDATA/postmaster.pid" ] && [ "${PG_STARTED:-0}" = "1" ]; then
      pgctl_ns -D "$PGDATA" -m fast -w -t 120 stop >/dev/null 2>&1 && log "experiment PostgreSQL stopped"
    fi
  fi
  stop_namespace
}

cleanup_on_exit() {
  local rc=$?
  # Act ONLY on a run root THIS invocation created. A refused second `--run`
  # (root already exists) must never signal, stop or write into another run.
  if [ "${PHASE:-}" = "run" ] && [ "${CREATED_ROOT:-0}" != "1" ]; then
    log "runner refused before creating anything (rc=$rc${STOP_REASON:+: $STOP_REASON}) — nothing to clean up"
    exit $rc
  fi
  if [ "${PHASE:-}" = "run" ]; then
    log "runner exiting early (rc=$rc${STOP_REASON:+: $STOP_REASON}) — stopping experiment processes only"
    [ -n "${DRIVER_PID:-}" ] && kill "$DRIVER_PID" 2>/dev/null
    stop_experiment
    [ -n "${SENTINEL_PID:-}" ] && kill "$SENTINEL_PID" 2>/dev/null
    pkill -u "$TEST_USER" -f "$ROOT" 2>/dev/null
    [ -d "$ROOT/results" ] && python3 - "$ROOT" "$rc" "${STOP_REASON:-}" <<'PY'
import json, sys, datetime
root, rc, why = sys.argv[1], int(sys.argv[2]), sys.argv[3]
json.dump({'runner_exit_code': rc, 'stopped_early': True, 'reason': why,
           'at_utc': datetime.datetime.utcnow().isoformat() + 'Z'},
          open(f'{root}/results/runner_status.json', 'w'), indent=2)
PY
  fi
  exit $rc
}

# ── the round ────────────────────────────────────────────────────────────────
do_run() {
  PHASE=run
  need_root
  trap cleanup_on_exit EXIT INT TERM
  log "=== 0. preflight gates (re-run, fail closed) ==="
  # subshell: preflight's own `exit 2` must not end the runner before it reports
  ( preflight ) > "$BASE/preflight-$RUN_ID.log" 2>&1 || { cat "$BASE/preflight-$RUN_ID.log"; die "preflight has blocking problems"; }
  local SYSID; SYSID=$(cluster_sysid "$PGDATA")
  [ -n "$SYSID" ] || die "could not read the preserved cluster's system identifier"

  log "=== 1. run root (preserved experiment reused, nothing of round 1 modified) ==="
  mkdir -p "$BASE"
  chown -R "$TEST_USER:" "$TOOL"
  [ -e "$ROOT" ] && die "run root $ROOT appeared after preflight — refusing"
  as_user python3 "$TOOL/vps_aiface_root.py" create --root "$ROOT" --preserved-root "$PRESERVED" \
      --tested-sha "$TESTED_SHA" --run-id "$RUN_ID" || die "could not create the run root"
  CREATED_ROOT=1
  cp "$BASE/preflight-$RUN_ID.log" "$ROOT/results/preflight.log"; chown "$TEST_USER:" "$ROOT/results/preflight.log"

  log "=== 2. production snapshot BEFORE (read-only: units, pids, hashes, 7788, health) ==="
  python3 "$TOOL/vps_prod_snapshot.py" --root "$ROOT" --label pre --health-url "$HEALTH_URL" \
      --prod-repo "$PROD_REPO" > "$ROOT/logs/prod_pre.log" 2>&1 || die "production pre-snapshot failed"
  chown -R "$TEST_USER:" "$ROOT/results" "$ROOT/logs"

  log "=== 3. cold backup of the preserved cluster (it is shut down; copy, then verify) ==="
  [ "$(cluster_state "$PGDATA")" = "shut down" ] || die "preserved cluster is not cleanly shut down"
  as_user mkdir -p "$ROOT/backup"
  as_user cp -a "$PGDATA" "$ROOT/backup/pgdata" || die "cold backup copy failed"
  local h1 h2 n1 n2
  h1=$(cd "$PGDATA" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64)
  h2=$(cd "$ROOT/backup/pgdata" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64)
  n1=$(find "$PGDATA" -type f | wc -l); n2=$(find "$ROOT/backup/pgdata" -type f | wc -l)
  [ "$h1" = "$h2" ] && [ "$n1" = "$n2" ] || die "cold backup verification FAILED (content hash differs)"
  [ "$(cluster_sysid "$ROOT/backup/pgdata")" = "$SYSID" ] || die "backup system identifier differs"
  as_user python3 - "$ROOT" "$PGDATA" "$ROOT/backup/pgdata" "$h1" "$n1" "$SYSID" "$(du -sb "$PGDATA" | cut -f1)" <<'PY' || die "could not record the backup"
import json, sys, datetime
root, src, dst, h, n, sysid, size = sys.argv[1:8]
json.dump({'verified': True, 'method': 'cold copy (cluster shut down) + per-file sha256 manifest',
           'source': src, 'dest': dst, 'files': int(n), 'bytes': int(size),
           'content_sha256_of_manifest': h, 'system_identifier': sysid,
           'at_utc': datetime.datetime.utcnow().isoformat() + 'Z'},
          open(f'{root}/run/backup.json', 'w'), indent=2)
PY
  log "backup verified: $n1 files, $(du -sh "$ROOT/backup/pgdata" | cut -f1), identical content hash"

  log "=== 4. tested source: experiment-owned bare clone + bundle (production checkout read-only) ==="
  git_ro -C "$PROD_REPO" cat-file -e "${BASE_SHA}^{commit}" || die "base $BASE_SHA not in $PROD_REPO"
  git_ro clone --quiet --bare --no-hardlinks "$PROD_REPO" "$SRC_REPO" || die "could not clone the production checkout"
  chown -R "$TEST_USER:" "$SRC_REPO"
  as_user git -C "$SRC_REPO" bundle verify "$BUNDLE" >/dev/null 2>&1 || die "bundle verification failed"
  as_user git -C "$SRC_REPO" fetch --quiet "$BUNDLE" 'refs/heads/aifx-tested:refs/heads/attlt/aifx-tested' \
    || die "could not import the bundle"
  [ "$(git_ro -C "$SRC_REPO" rev-parse refs/heads/attlt/aifx-tested)" = "$TESTED_SHA" ] || die "bundle head is not $TESTED_SHA"
  [ "$(git_ro -C "$SRC_REPO" rev-parse "${TESTED_SHA}^{tree}")" = "$TESTED_TREE" ] || die "tested tree hash mismatch"
  [ "$(git_ro -C "$SRC_REPO" rev-parse "${TESTED_SHA}^")" = "$BASE_SHA" ] || die "tested commit's parent is not $BASE_SHA"
  local CHANGED; CHANGED=$(git_ro -C "$SRC_REPO" diff --name-only "$BASE_SHA" "$TESTED_SHA" | sort | tr '\n' ' ')
  [ "$CHANGED" = "$EXPECTED_DIFF" ] || die "tested revision changes unexpected files: $CHANGED"
  log "tested $TESTED_SHA = $BASE_SHA + [$CHANGED] (tree $TESTED_TREE)"

  log "=== 5. archived application source + virtual environments pinned to PRODUCTION's versions ==="
  # requirements.txt leaves transitive deps unpinned; a fresh resolve today pulls
  # SQLAlchemy 2.1 (psycopg3 default driver) which cannot run this app. The test
  # venv therefore mirrors production's installed versions (read-only listing).
  local PROD_PID; PROD_PID=$(systemctl show -p MainPID --value mecha-school.service 2>/dev/null)
  python3 "$TOOL/vps_prod_constraints.py" --root "$ROOT" --prod-pid "${PROD_PID:-0}" \
      --prod-repo "$PROD_REPO" --owner "$TEST_USER" > "$ROOT/logs/prod_constraints.log" 2>&1 \
    || { cat "$ROOT/logs/prod_constraints.log"; die "could not derive production package versions"; }
  chown -R "$TEST_USER:" "$ROOT/results"
  local PROD_SQLA; PROD_SQLA=$(python3 -c "import json;print(json.load(open('$ROOT/results/prod_constraints_summary.json')).get('production_sqlalchemy') or '')")
  log "production SQLAlchemy: ${PROD_SQLA:-unknown (fallback pin SQLAlchemy<2.1)}"
  if ! as_user python3 "$TOOL/setup_env.py" --root "$ROOT" --repo "$SRC_REPO" --revision "$TESTED_SHA" \
      --steps src,venv --target-requirements pinned --constraints "$ROOT/run/prod_constraints.txt" \
      > "$ROOT/logs/setup_env.log" 2>&1; then
    tail -15 "$ROOT/logs/setup_env.log"
    [ -n "$PROD_SQLA" ] || die "setup failed (src/venv)"
    log "full production constraint set did not resolve — retrying with ONLY SQLAlchemy==$PROD_SQLA"
    as_user rm -rf "$ROOT/venv-target"
    as_user sh -c "echo 'SQLAlchemy==$PROD_SQLA' > '$ROOT/run/prod_constraints_min.txt'"
    as_user python3 "$TOOL/setup_env.py" --root "$ROOT" --repo "$SRC_REPO" --revision "$TESTED_SHA" \
        --steps venv --target-requirements pinned --constraints "$ROOT/run/prod_constraints_min.txt" \
        > "$ROOT/logs/setup_env_retry.log" 2>&1 || { tail -30 "$ROOT/logs/setup_env_retry.log"; die "setup failed (venv, minimal pin)"; }
  fi
  [ -x "$GENPY" ] && [ -x "$TGTPY" ] || die "virtual environments missing"
  local TEST_SQLA; TEST_SQLA=$("$TGTPY" -c 'import sqlalchemy;print(sqlalchemy.__version__)' 2>/dev/null)
  as_user sh -c "'$TGTPY' -m pip freeze > '$ROOT/results/venv_target_freeze.txt' 2>/dev/null"
  log "test venv SQLAlchemy: $TEST_SQLA"
  if [ -n "$PROD_SQLA" ]; then [ "$TEST_SQLA" = "$PROD_SQLA" ] || die "test SQLAlchemy $TEST_SQLA != production $PROD_SQLA"
  else case "$TEST_SQLA" in 2.0.*) : ;; *) die "test SQLAlchemy $TEST_SQLA is not 2.0.x";; esac; fi
  grep -q GUNICORN_MAX_REQUESTS "$ROOT/experiment.json" && die "experiment.json pins GUNICORN_MAX_REQUESTS"
  as_user python3 "$TOOL/vps_aiface_root.py" identity --root "$ROOT" > "$ROOT/logs/identity.log" 2>&1 \
    || { cat "$ROOT/logs/identity.log"; die "identity card refused"; }

  log "=== 6. production targets resolved on the HOST (for per-target negative proofs) ==="
  PUBIP=$(ip -4 -o addr show scope global | awk 'NR==1{split($4,a,"/");print a[1]}')
  python3 "$TOOL/vps_forbidden_targets.py" --root "$ROOT" --prod-pid "${PROD_PID:-0}" \
      --prod-repo "$PROD_REPO" --public-ip "$PUBIP" --owner "$TEST_USER" > "$ROOT/logs/forbidden_targets.log" 2>&1 \
    || { cat "$ROOT/logs/forbidden_targets.log"; die "could not resolve the production targets — cannot prove unreachability"; }
  chown -R "$TEST_USER:" "$ROOT/results"

  log "=== 7. ephemeral private network namespace + isolation proofs BEFORE anything starts ==="
  start_namespace
  in_ns_root python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope inside --label pre-load \
      --public-ip "$PUBIP" --targets-file "$ROOT/secrets/forbidden_targets.json" \
      > "$ROOT/logs/isolation_inside_pre.log" 2>&1 || { tail -40 "$ROOT/logs/isolation_inside_pre.log"; die "isolation proof FAILED inside the namespace"; }
  python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope outside --label pre-load --nspid "$NSPID" \
      --live-health-url "$HEALTH_URL" > "$ROOT/logs/isolation_outside_pre.log" 2>&1 \
    || { tail -40 "$ROOT/logs/isolation_outside_pre.log"; die "isolation proof FAILED on the host"; }
  chown -R "$TEST_USER:" "$ROOT/results" "$ROOT/logs"
  log "isolation proven: only lo inside; production DB/Supabase, Redis, Firebase, public 443/7788 unreachable"

  log "=== 8. preserved PostgreSQL cluster, INSIDE the namespace (TCP-only, 127.0.0.1:$PG_PORT) ==="
  [ "$(cluster_state "$PGDATA")" = "shut down" ] || die "preserved cluster state changed before start"
  PG_STARTED=1
  pgctl_ns -D "$PGDATA" -l "$ROOT/logs/postgres.log" -w -t 180 \
      -o "-c listen_addresses=127.0.0.1 -c port=$PG_PORT -c track_commit_timestamp=on" \
      start > "$ROOT/logs/pg_ctl_start.log" 2>&1 || { tail -20 "$ROOT/logs/postgres.log"; die "preserved cluster did not start"; }
  listening_on_host "$PG_PORT" && die "PostgreSQL is visible on the HOST — isolation broken"
  # TCP-only comes from the preserved postgresql.auto.conf (unix_socket_directories = '');
  # track_commit_timestamp is a command-line option only: no config file is edited.
  log "preserved cluster up inside the namespace only (host port $PG_PORT closed)"

  log "=== 9. prepare: identity, dataset, verified backup, allow-listed migrations, fake tokens ==="
  in_ns "$TGTPY" "$TOOL/vps_prepare_db.py" --root "$ROOT" --preserved-root "$PRESERVED" \
      --system-identifier "$SYSID" --allowed-migrations "$ALLOWED_MIGRATIONS" \
      > "$ROOT/logs/prepare.log" 2>&1 || { tail -60 "$ROOT/logs/prepare.log"; die "prepare REFUSED — see logs/prepare.log"; }
  log "prepare PASS"

  log "=== 10. host sentinel (outside the namespace): production health, host CPU/memory/disk ==="
  as_user mkdir -p "$OUT"
  runuser -u "$TEST_USER" -- "${CLEAN_ENV[@]}" "$GENPY" "$TOOL/health_sentinel.py" --root "$ROOT" --out "$OUT" \
      --live-health-url "$HEALTH_URL" --min-mem-pct "$MIN_MEM_PCT" --mem-sustain-s 20 \
      --max-cpu-pct 80 --cpu-sustain-s 20 --health-slow-ms 3000 --health-slow-sustain-s 20 \
      --health-sustain-s 10 --tail-seconds 30 --max-seconds 3600 >> "$ROOT/logs/sentinel.log" 2>&1 &
  SENTINEL_PID=$!
  sleep 3; kill -0 "$SENTINEL_PID" 2>/dev/null || die "sentinel did not start"
  log "sentinel pid=$SENTINEL_PID"

  log "=== 11. THE ROUND (driver inside the namespace; watchdog in its own session) ==="
  log "    sanity (same-school + cross-school) → Stage A 10.5/s → drain → >=80 s idle baseline → Stage B 21/s → drain"
  in_ns env ATTLT_AIFACE_MODE=vps ATTLT_AIFACE_ROOT="$ROOT" ATTLT_EXPECTED_COMMIT="$TESTED_SHA" \
      ATTLT_PG_HOST=127.0.0.1 ATTLT_PG_PORT="$PG_PORT" \
      ATTLT_FORBIDDEN_TARGETS="$ROOT/secrets/forbidden_targets.json" \
      "$GENPY" "$TOOL/docker/aiface_load.py" > "$ROOT/logs/driver.log" 2>&1 &
  DRIVER_PID=$!
  wait "$DRIVER_PID"; local RC=$?; DRIVER_PID=""
  log "driver exit code $RC ($(python3 -c "import json;print(json.load(open('$OUT/summary.json')).get('VERDICT'))" 2>/dev/null))"

  log "=== 12. wait for the watchdog's recovery window + summary ==="
  for _ in $(seq 1 60); do pgrep -u "$TEST_USER" -f "watchdog.py --root $ROOT" >/dev/null || break; sleep 2; done
  pgrep -u "$TEST_USER" -f "watchdog.py --root $ROOT" >/dev/null && { log "watchdog still running — terminating it"; pkill -u "$TEST_USER" -f "watchdog.py --root $ROOT"; }

  log "=== 13. isolation proofs AFTER the round ==="
  in_ns_root python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope inside --label post-load \
      --public-ip "$PUBIP" --targets-file "$ROOT/secrets/forbidden_targets.json" \
      > "$ROOT/logs/isolation_inside_post.log" 2>&1 || log "WARNING: post-load inside proof reported a failure"
  python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope outside --label post-load --nspid "$NSPID" \
      --live-health-url "$HEALTH_URL" > "$ROOT/logs/isolation_outside_post.log" 2>&1 || log "WARNING: post-load outside proof reported a failure"

  log "=== 14. stop the experiment (PostgreSQL we started, namespace); data + evidence retained ==="
  stop_experiment
  PG_STARTED=0
  local st; st=$(cluster_state "$PGDATA"); log "preserved cluster state after stop: $st"
  for _ in $(seq 1 40); do kill -0 "$SENTINEL_PID" 2>/dev/null || break; sleep 2; done
  kill "$SENTINEL_PID" 2>/dev/null; SENTINEL_PID=""

  log "=== 15. post-checks: ports closed, no experiment process, production unchanged ==="
  # live processes only: an exited child awaiting its reaper (state Z) is not running
  local leftovers; leftovers=$(ps -u "$TEST_USER" -o pid=,stat=,args= 2>/dev/null | awk '$2 !~ /^Z/' | head -5)
  python3 "$TOOL/vps_prod_snapshot.py" --root "$ROOT" --label post --health-url "$HEALTH_URL" \
      --prod-repo "$PROD_REPO" --compare pre > "$ROOT/logs/prod_post.log" 2>&1
  local PROD_RC=$?
  python3 - "$ROOT" "$RC" "$PROD_RC" "$st" "$leftovers" <<'PY'
import json, sys, subprocess, datetime
root, rc, prod_rc, st, left = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
def listening(p):
    return bool(subprocess.run(['ss', '-ltnH', f'sport = :{p}'], capture_output=True, text=True).stdout.strip())
out = {'driver_exit_code': rc, 'production_unchanged': prod_rc == 0,
       'host_ports_listening_after': {p: listening(p) for p in (18180, 18188, 55480)},
       'preserved_cluster_state_after': st,
       'experiment_processes_left': left.splitlines() if left.strip() else [],
       'namespace_released': True, 'at_utc': datetime.datetime.utcnow().isoformat() + 'Z'}
json.dump(out, open(f'{root}/results/runner_status.json', 'w'), indent=2)
print(json.dumps(out, indent=2))
PY
  chown -R "$TEST_USER:" "$ROOT"
  PHASE=done
  log "=== done. evidence: $ROOT/results   run: bash $TOOL/vps_aiface_run.sh --digest ==="
  return $RC
}

status() {
  echo "runner log tail:"; tail -6 "$RUNLOG" 2>/dev/null
  [ -f "$OUT/live_p0.json" ] && { echo "driver live snapshot:"; cat "$OUT/live_p0.json"; echo; }
  [ -f "$OUT/STOP.json" ] && { echo "STOP requested:"; head -c 600 "$OUT/STOP.json"; echo; }
  echo "driver log tail:"; tail -4 "$ROOT/logs/driver.log" 2>/dev/null
  echo "experiment processes:"; pgrep -u "$TEST_USER" -a 2>/dev/null | cut -c1-140
}

case "${1:---help}" in
  --preflight) preflight ;;
  --run)       need_root; mkdir -p "$BASE"; do_run 2>&1 | tee -a "$RUNLOG"; exit "${PIPESTATUS[0]}" ;;
  --digest)    python3 "$TOOL/vps_aiface_digest.py" --root "$ROOT" ;;
  --status)    status ;;
  *) sed -n '2,25p' "${BASH_SOURCE[0]}" ;;
esac
