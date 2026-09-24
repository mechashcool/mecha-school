#!/usr/bin/env bash
# ROUND 2 bootstrap + runner for the attendance load round on the VPS.
#
#   bash vps_run.sh --preflight   # read-only checks, changes nothing, exits
#   bash vps_run.sh --run         # bootstrap + the authorized ~600 s round
#   bash vps_run.sh --status      # progress while it runs
#   bash vps_run.sh --digest      # print the result files to copy back
#
# What changed since round 1 (attlt-20260917-7b7cb5-vps):
#   * the tested source is the PATCHED revision, obtained through an
#     EXPERIMENT-OWNED git repository: production is cloned read-only and the
#     reviewed fix is imported from an incremental bundle shipped in this
#     package. /var/www/mecha-school is never written to;
#   * a FRESH experiment id, root, PostgreSQL cluster/port, configuration and
#     results directory. Reusing the round-1 root is refused, not silently
#     resumed, and every round-1 resource is left untouched;
#   * the retained portable PostgreSQL runtime is used, with its private
#     library paths, and the new cluster is created TCP-only
#     (unix_socket_directories = '') so it cannot repeat the round-1 failure on
#     a missing /var/run/postgresql;
#   * before any load, the effective runtime configuration is verified to be
#     one worker, four threads, max_requests=0, with the worker actually
#     adopting the master-owned WS listener.
#
# Unchanged from round 1: ephemeral private network namespace, two-sided
# isolation proofs, unprivileged application/PostgreSQL/generator processes,
# notification isolation, production-health sentinel outside the namespace,
# ownership manifest, watchdog thresholds, and the fixed 20% memory floor with
# no degraded-host override anywhere in this script.
set -uo pipefail

EXP_ID="attlt-20260918-round2"
BASE="/srv/attlt"
TEST_USER="attlt"
ROOT="$BASE/$EXP_ID"
ROUND1_ROOT="$BASE/attlt-20260917-7b7cb5-vps"
PROD_REPO="/var/www/mecha-school"                  # read-only, never modified
SRC_REPO="$BASE/src-round2"                        # experiment-owned source repository
BASE_SHA="98930540e583c33a3fea311427cc7adf350c30e0"
TESTED_SHA="47204b2be965d1e06229aa679ffc7f775fce47c6"
HEALTH_URL="https://school.smartcoreiq.cloud/ops/health"
PG_RUNTIME_ROOT="${ATTLT_PG_RUNTIME_ROOT:-/srv/attlt-pkg/pg-runtime/root}"
HTTP_PORT=18180
WS_PORT=18188
PG_PORT=55481                                      # round 1 used 55480; keep them separate
MIN_MEM_PCT=20
TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$TOOL/round2/round2.bundle"
RUNLOG="$BASE/runner-round2.log"
GENPY="$ROOT/venv-gen/bin/python"

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { printf '[%s] FATAL: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root (bootstrap needs to create the test account and the namespace)"; }

# ── portable PostgreSQL runtime ──────────────────────────────────────────────
discover_pg() {
  PGBIN="${ATTLT_PG_BIN_OVERRIDE:-}"
  if [ -z "$PGBIN" ]; then
    PGBIN=$(find "$PG_RUNTIME_ROOT" -type d -name bin -path '*postgresql*' 2>/dev/null | sort | head -1)
  fi
  PGLIBS=$(find "$PG_RUNTIME_ROOT" -maxdepth 6 -type d \( -name 'x86_64-linux-gnu' -o -name 'lib' \) \
             2>/dev/null | tr '\n' ':' | sed 's/:$//')
  export ATTLT_PG_BIN="$PGBIN"
  export LD_LIBRARY_PATH="${PGLIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

as_user() { runuser -u "$TEST_USER" -- env ATTLT_REPO="$SRC_REPO" ATTLT_PG_BIN="$PGBIN" \
              LD_LIBRARY_PATH="$LD_LIBRARY_PATH" ATTLT_TOOL="$TOOL" GIT_CONFIG_COUNT=1 \
              GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*" \
              GIT_OPTIONAL_LOCKS=0 HOME="$BASE/home" "$@"; }
in_ns() { nsenter --net="/proc/$NSPID/ns/net" -- runuser -u "$TEST_USER" -- \
            env ATTLT_REPO="$SRC_REPO" ATTLT_PG_BIN="$PGBIN" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
            ATTLT_TOOL="$TOOL" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory \
            GIT_CONFIG_VALUE_0="*" GIT_OPTIONAL_LOCKS=0 HOME="$BASE/home" "$@"; }
in_ns_root() { nsenter --net="/proc/$NSPID/ns/net" -- "$@"; }

git_ro() { env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="*" \
             GIT_OPTIONAL_LOCKS=0 git "$@"; }

# ── preflight ────────────────────────────────────────────────────────────────
preflight() {
  local fail=0 warn=0
  discover_pg
  echo "=== round 2 preflight (read-only; nothing is created or changed) ==="
  [ "$(id -u)" -eq 0 ] && echo "  root: yes" || { echo "  root: NO"; fail=$((fail+1)); }

  local mem
  mem=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.2f",100*a/t}' /proc/meminfo)
  if awk "BEGIN{exit !($mem >= $MIN_MEM_PCT)}"; then echo "  MemAvailable: ${mem}% (>= ${MIN_MEM_PCT}% floor)"
  else echo "  MemAvailable: ${mem}% BELOW the ${MIN_MEM_PCT}% floor — the startup gate would reject the round"; fail=$((fail+1)); fi

  local free_gb; free_gb=$(df -PBG /srv | awk 'NR==2{gsub("G","",$4);print $4}')
  if [ "${free_gb:-0}" -ge 10 ]; then echo "  free disk on /srv: ${free_gb} GiB"
  else echo "  free disk on /srv: ${free_gb:-?} GiB (need >= 10)"; fail=$((fail+1)); fi

  echo "  --- round isolation ---"
  if [ -e "$ROOT" ]; then echo "  round2 root $ROOT ALREADY EXISTS — refusing to reuse it; move it aside first"; fail=$((fail+1));
  else echo "  round2 root $ROOT: free"; fi
  if [ -e "$ROUND1_ROOT" ]; then echo "  round1 root present and will be left untouched: $ROUND1_ROOT"
  else echo "  round1 root not found (nothing to preserve at $ROUND1_ROOT)"; warn=$((warn+1)); fi
  for p in $HTTP_PORT $WS_PORT $PG_PORT; do
    if ss -ltn 2>/dev/null | tail -n +2 | grep -qE "[:.]$p[[:space:]]"; then echo "  port $p: IN USE on the host"; fail=$((fail+1));
    else echo "  port $p: free on the host"; fi
  done

  echo "  --- portable PostgreSQL runtime ---"
  echo "  runtime root: $PG_RUNTIME_ROOT"
  echo "  bin: ${PGBIN:-NOT FOUND}"
  if [ -n "$PGBIN" ] && [ -x "$PGBIN/initdb" ] && [ -x "$PGBIN/pg_ctl" ] && [ -x "$PGBIN/postgres" ]; then
    local v; v=$(env LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$PGBIN/initdb" --version 2>&1 | head -1)
    if echo "$v" | grep -qi initdb; then echo "  initdb runs with the private library path: $v"
    else echo "  initdb FAILED to run (library path problem): $v"; fail=$((fail+1)); fi
  else
    echo "  initdb/pg_ctl/postgres MISSING under $PG_RUNTIME_ROOT"; fail=$((fail+1))
  fi
  echo "  the new cluster will be created TCP-only (unix_socket_directories = '')"

  echo "  --- source repository ---"
  if [ -f "$BUNDLE" ]; then echo "  bundle: $BUNDLE ($(stat -c%s "$BUNDLE") bytes)"
    git_ro bundle list-heads "$BUNDLE" 2>/dev/null | sed 's/^/    head: /'
  else echo "  bundle MISSING at $BUNDLE"; fail=$((fail+1)); fi
  if git_ro -C "$PROD_REPO" cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "  base revision $BASE_SHA present in $PROD_REPO (read-only)"
  else
    echo "  base revision $BASE_SHA NOT FOUND in $PROD_REPO — the bundle cannot be applied"; fail=$((fail+1))
  fi
  if [ -e "$SRC_REPO" ]; then echo "  experiment source repo $SRC_REPO already exists — refusing; move it aside"; fail=$((fail+1));
  else echo "  experiment source repo $SRC_REPO: free"; fi

  echo "  --- tools ---"
  for t in unshare nsenter runuser setpriv ip git python3 curl tar; do
    if command -v "$t" >/dev/null; then echo "  $t: $(command -v "$t")"
    else echo "  $t: MISSING"; fail=$((fail+1)); fi
  done
  python3 -c 'import venv, ensurepip' 2>/dev/null && echo "  python3 venv+ensurepip: OK" || { echo "  python3 venv+ensurepip: MISSING"; fail=$((fail+1)); }
  local nsout
  nsout=$(unshare --net -- sh -c 'ip link set lo up 2>/dev/null; ip -o link show 2>/dev/null | wc -l' 2>/dev/null)
  if [ "${nsout:-0}" = "1" ]; then echo "  unshare --net: works (namespace has exactly 1 interface: lo)"
  else echo "  unshare --net: FAILED or unexpected interface count (${nsout:-none})"; fail=$((fail+1)); fi
  if unshare --net -- timeout 6 curl -sS -o /dev/null -m 4 "$HEALTH_URL" 2>/dev/null; then
    echo "  namespace isolation: FAILED — production is reachable from inside a fresh namespace"; fail=$((fail+1))
  else echo "  namespace isolation: production NOT reachable from inside a fresh namespace (expected)"; fi

  local code; code=$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)
  [ "$code" = "200" ] && echo "  production health: 200" || { echo "  production health: $code (expected 200)"; fail=$((fail+1)); }
  code=$(curl -sS -o /dev/null -m 10 -w '%{http_code}' https://pypi.org/simple/ 2>/dev/null)
  [ "$code" = "200" ] && echo "  pypi reachable: 200" || { echo "  pypi reachable: $code"; fail=$((fail+1)); }

  local mins; mins=$(TZ=Asia/Baghdad date +'%H %M' | awk '{print (23-$1)*60 + (60-$2)}')
  if [ "$mins" -ge 60 ]; then echo "  time to Asia/Baghdad midnight: ${mins} min (enough for one round)"
  else echo "  time to Asia/Baghdad midnight: ${mins} min — WAIT, the test date would change mid-round"; fail=$((fail+1)); fi

  echo "=== preflight: $fail blocking problem(s), $warn warning(s) ==="
  [ "$fail" -eq 0 ] || exit 2
}

# ── namespace lifecycle ──────────────────────────────────────────────────────
start_namespace() {
  unshare --net -- bash -c 'ip link set lo up && exec sleep 7200' >/dev/null 2>&1 &
  NSPID=$!
  sleep 1
  kill -0 "$NSPID" 2>/dev/null || die "could not create the private network namespace"
  [ "$(readlink /proc/$NSPID/ns/net)" != "$(readlink /proc/self/ns/net)" ] \
    || die "the namespace holder shares the host network namespace"
  echo "$NSPID" > "$ROOT/run/netns_holder.pid"
  as_user "$GENPY" -c "
import sys; sys.path.insert(0, '$TOOL')
import manifest, psutil
p = psutil.Process($NSPID)
manifest.add_resource('$ROOT', 'process', role='netns-holder', pid=$NSPID,
                      create_time=p.create_time(), cmdline=p.cmdline(),
                      note='ephemeral private network namespace; dies with this process')" 2>/dev/null
  log "private network namespace holder pid=$NSPID"
}

stop_namespace() {
  [ -n "${NSPID:-}" ] || return 0
  kill "$NSPID" 2>/dev/null
  sleep 1
  kill -9 "$NSPID" 2>/dev/null
  log "network namespace released"
}

cleanup_on_exit() {
  local rc=$?
  if [ "${PHASE:-}" = "run" ]; then
    [ -n "${SENTINEL_PID:-}" ] && kill "$SENTINEL_PID" 2>/dev/null
    if [ -n "${NSPID:-}" ] && [ -f "$ROOT/run/target.json" ]; then
      in_ns "$GENPY" "$TOOL/target.py" stop --root "$ROOT" >/dev/null 2>&1
      in_ns env PATH="$PGBIN:$PATH" pg_ctl -D "$ROOT/pgdata" -m fast stop >/dev/null 2>&1
    fi
    stop_namespace
  fi
  exit $rc
}

# ── the round ────────────────────────────────────────────────────────────────
do_run() {
  PHASE=run
  need_root
  discover_pg
  trap cleanup_on_exit EXIT INT TERM
  export ATTLT_TOOL="$TOOL"

  log "=== 0. refusing any reuse of round 1 ==="
  [ "$ROOT" = "$ROUND1_ROOT" ] && die "round2 root equals the round1 root"
  [ -e "$ROOT" ] && die "round2 root $ROOT already exists — refusing to resume or overwrite it. Move it aside and rerun."
  [ -e "$SRC_REPO" ] && die "$SRC_REPO already exists — refusing to reuse a source repository of unknown provenance."
  [ -n "$PGBIN" ] && [ -x "$PGBIN/initdb" ] || die "portable PostgreSQL runtime not usable under $PG_RUNTIME_ROOT (run --preflight)"
  log "round1 root left untouched: $ROUND1_ROOT"

  log "=== 1. test account and fresh experiment root ==="
  mkdir -p "$BASE"
  if ! id "$TEST_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$BASE/home" --create-home --shell /bin/bash "$TEST_USER" \
      || die "could not create the test account"
    log "created unprivileged test account: $TEST_USER"
    CREATED_USER=1
  else
    log "test account $TEST_USER already exists — reusing"
    CREATED_USER=0
  fi
  mkdir -p "$BASE/home"
  chown "$TEST_USER:" "$BASE" "$BASE/home"
  chown -R "$TEST_USER:" "$TOOL"
  runuser -u "$TEST_USER" -- python3 -c "
import sys; sys.path.insert(0, '$TOOL')
import manifest; print(manifest.create('$EXP_ID', base='$BASE'))" || die "could not create the experiment root"
  [ -d "$ROOT" ] || die "experiment root $ROOT was not created"

  log "=== 2. experiment-owned source repository (production stays read-only) ==="
  git_ro -C "$PROD_REPO" cat-file -e "${BASE_SHA}^{commit}" \
    || die "base revision $BASE_SHA not present in $PROD_REPO"
  # --no-hardlinks: do not share an object store with the production checkout
  git_ro clone --quiet --no-hardlinks "$PROD_REPO" "$SRC_REPO" || die "could not clone the production checkout"
  chown -R "$TEST_USER:" "$SRC_REPO"
  [ -f "$BUNDLE" ] || die "bundle missing at $BUNDLE"
  as_user git bundle verify "$BUNDLE" >/dev/null 2>&1 \
    || die "bundle verification failed (its prerequisite $BASE_SHA is not in $SRC_REPO)"
  as_user git -C "$SRC_REPO" fetch --quiet "$BUNDLE" 'refs/heads/*:refs/heads/attlt/*' \
    || die "could not import the bundle"
  as_user git -C "$SRC_REPO" cat-file -e "${TESTED_SHA}^{commit}" \
    || die "tested revision $TESTED_SHA not present after importing the bundle"
  CHANGED=$(git_ro -C "$SRC_REPO" diff --name-only "$BASE_SHA" "$TESTED_SHA" | sort | tr '\n' ' ')
  log "bundle imported: $BASE_SHA -> $TESTED_SHA"
  log "revision changes only: $CHANGED"
  [ "$CHANGED" = "app/services/ai_face_ws.py gunicorn.conf.py " ] \
    || die "the tested revision changes unexpected files: $CHANGED"
  as_user python3 -c "
import sys; sys.path.insert(0, '$TOOL')
import manifest
manifest.add_resource('$ROOT', 'directory', path='$SRC_REPO',
                      note='experiment-owned source repository: read-only clone of the production '
                           'checkout plus the reviewed fix imported from round2.bundle')
manifest.add_resource('$ROOT', 'revision', base_sha='$BASE_SHA', tested_sha='$TESTED_SHA',
                      changed_files='$CHANGED')
if not any(r.get('kind') == 'os_user' for r in manifest.load('$ROOT')['resources']):
    manifest.add_resource('$ROOT', 'os_user', name='$TEST_USER', created_by_runner=bool($CREATED_USER),
                          home='$BASE/home', note='unprivileged account owning every experiment process')
    manifest.add_resource('$ROOT', 'directory', path='$TOOL', note='transferred tooling package')
"

  log "=== 3. configuration, archived application source, virtual environments ==="
  as_user python3 "$TOOL/setup_env.py" --root "$ROOT" --repo "$SRC_REPO" --revision "$TESTED_SHA" \
      --steps config,src,venv --http-port "$HTTP_PORT" --ws-port "$WS_PORT" \
      --pg-mode local-cluster --pg-port "$PG_PORT" --target-requirements pinned \
    || die "setup failed (config/src/venv)"
  chown -R "$TEST_USER:" "$ROOT"
  [ -x "$GENPY" ] || die "generator virtual environment missing at $GENPY"
  grep -q GUNICORN_MAX_REQUESTS "$ROOT/experiment.json" \
    && die "experiment.json pins GUNICORN_MAX_REQUESTS — this package must let gunicorn.conf.py decide"
  log "experiment.json carries no GUNICORN_MAX_REQUESTS override"

  log "=== 4. production configuration differences (read-only, no values printed) ==="
  PROD_PID=$(systemctl show -p MainPID --value mecha-school.service 2>/dev/null)
  if [ -n "$PROD_PID" ] && [ "$PROD_PID" != "0" ]; then
    python3 "$TOOL/prod_config_probe.py" --root "$ROOT" --repo "$PROD_REPO" --prod-pid "$PROD_PID" \
        --expected-revision "$TESTED_SHA" > "$ROOT/logs/config_probe.log" 2>&1 \
      || log "WARNING: configuration probe failed (see logs/config_probe.log) — continuing"
    chown -R "$TEST_USER:" "$ROOT/results" "$ROOT/logs"
  else
    log "WARNING: mecha-school.service MainPID not found — configuration differences not recorded"
  fi

  log "=== 5. ephemeral private network namespace ==="
  start_namespace
  PUBIP=$(ip -4 -o addr show scope global | awk 'NR==1{split($4,a,"/");print a[1]}')

  log "=== 6. proving isolation BEFORE any load ==="
  in_ns_root python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope inside --label pre-load \
      --public-ip "$PUBIP" > "$ROOT/logs/isolation_inside_pre.log" 2>&1 \
    || die "isolation proof FAILED inside the namespace — see logs/isolation_inside_pre.log"
  python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope outside --label pre-load --nspid "$NSPID" \
      --live-health-url "$HEALTH_URL" > "$ROOT/logs/isolation_outside_pre.log" 2>&1 \
    || die "isolation proof FAILED on the host — see logs/isolation_outside_pre.log"
  chown -R "$TEST_USER:" "$ROOT/results" "$ROOT/logs"
  log "isolation proven: only lo inside, production/Redis/internet unreachable, no test listener on the host"

  log "=== 7. dedicated PostgreSQL cluster (portable runtime, TCP-only) ==="
  in_ns "$GENPY" "$TOOL/setup_env.py" --root "$ROOT" --steps pg,db || die "PostgreSQL setup failed"
  grep -q "unix_socket_directories = ''" "$ROOT/pgdata/postgresql.auto.conf" \
    || die "the new cluster is not TCP-only — unix_socket_directories was not set"
  log "cluster is TCP-only on 127.0.0.1:$PG_PORT"

  log "=== 8. isolated target, fixtures ==="
  in_ns "$GENPY" "$TOOL/target.py" isolation-check --root "$ROOT" || die "isolation-check failed"
  in_ns "$GENPY" "$TOOL/seed.py" --root "$ROOT" || die "seeding failed"
  in_ns "$GENPY" "$TOOL/target.py" start --root "$ROOT" || die "target did not start"

  log "=== 9. verifying the effective runtime BEFORE load ==="
  in_ns "$GENPY" "$TOOL/preload_verify.py" --root "$ROOT" \
      --expect-workers 1 --expect-threads 4 --expect-max-requests 0 \
    || die "pre-load verification FAILED — see results/preload_verification.json"
  log "verified: 1 worker, 4 threads, max_requests=0, worker adopted the master-owned WS listener"

  log "=== 10. tokens and prechecks ==="
  in_ns "$GENPY" "$TOOL/preauth.py" --root "$ROOT" || die "token pre-issue failed"
  in_ns "$GENPY" "$TOOL/precheck.py" --root "$ROOT" || die "prechecks did not pass"

  log "=== 11. proving isolation again, with the target listening ==="
  in_ns_root python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope inside --label target-up \
      --public-ip "$PUBIP" > "$ROOT/logs/isolation_inside_target.log" 2>&1 \
    || die "isolation proof FAILED with the target up"
  python3 "$TOOL/netns_proof.py" --root "$ROOT" --scope outside --label target-up --nspid "$NSPID" \
      --live-health-url "$HEALTH_URL" > "$ROOT/logs/isolation_outside_target.log" 2>&1 \
    || die "test listeners are visible on the host — refusing to generate load"
  chown -R "$TEST_USER:" "$ROOT/results" "$ROOT/logs"

  log "=== 12. host sentinel (outside the namespace) ==="
  as_user mkdir -p "$ROOT/results/round1"
  as_user "$GENPY" "$TOOL/health_sentinel.py" --root "$ROOT" --out "$ROOT/results/round1" \
      --live-health-url "$HEALTH_URL" --min-mem-pct "$MIN_MEM_PCT" --max-seconds 1200 \
      >> "$ROOT/logs/sentinel.log" 2>&1 &
  SENTINEL_PID=$!
  log "sentinel pid=$SENTINEL_PID (host resources + production health + owned-process termination)"
  as_user python3 -c "
import sys; sys.path.insert(0, '$TOOL')
import manifest, psutil
p = psutil.Process($SENTINEL_PID)
manifest.add_resource('$ROOT', 'process', role='health-sentinel', pid=$SENTINEL_PID,
                      create_time=p.create_time(), cmdline=p.cmdline(),
                      note='host-side guard, outside the private network namespace')" 2>/dev/null

  log "=== 13. the authorized round: 60 s baseline, startup gate, ~600 s schedule 10 -> 10000, 120 s recovery ==="
  in_ns "$GENPY" "$TOOL/run_round.py" --root "$ROOT" --round-name round1 --driver locust \
      --stage-limit 9 --baseline-seconds 60 --post-seconds 120 --min-mem-pct "$MIN_MEM_PCT"
  RC=$?
  log "run_round exit code $RC"

  log "=== 14. stopping experiment runtime (data, tooling and reports are retained) ==="
  if [ "$RC" -ne 0 ]; then
    log "round did not complete normally — stopping the sentinel"
    kill "$SENTINEL_PID" 2>/dev/null
  fi
  wait "$SENTINEL_PID" 2>/dev/null
  SENTINEL_PID=""
  in_ns "$GENPY" "$TOOL/target.py" stop --root "$ROOT" || log "WARNING: target stop reported an error"
  in_ns env PATH="$PGBIN:$PATH" pg_ctl -D "$ROOT/pgdata" -m fast stop || log "WARNING: PostgreSQL stop reported an error"
  stop_namespace
  NSPID=""
  chown -R "$TEST_USER:" "$ROOT"
  log "=== done. results: $ROOT/results/round1 ==="
  log "run 'bash $TOOL/vps_run.sh --digest' and return its output"
  PHASE=done
  return $RC
}

# ── output helpers ───────────────────────────────────────────────────────────
digest() {
  local out="$ROOT/results/round1"
  local tlog; tlog=$(python3 -c "
import json;print(json.load(open('$ROOT/run/target.json'))['log'])" 2>/dev/null)
  echo "##### DIGEST round2 #####"
  echo "experiment_id      : $EXP_ID"
  echo "experiment_root    : $ROOT"
  echo "round1_root        : $ROUND1_ROOT (preserved, untouched)"
  echo "base_sha           : $BASE_SHA"
  echo "tested_sha         : $TESTED_SHA"
  echo "source_repository  : $SRC_REPO (experiment-owned)"
  echo "archived_revision  : $(python3 -c "
import json;print(json.load(open('$ROOT/experiment.json')).get('app_revision_full'))" 2>/dev/null)"
  echo "pg_cluster         : 127.0.0.1:$PG_PORT  TCP-only=$(grep -c "unix_socket_directories = ''" "$ROOT/pgdata/postgresql.auto.conf" 2>/dev/null)"
  if [ -n "$tlog" ] && [ -f "$tlog" ]; then
    echo "listener_ownership : master_owns=$(grep -c 'master owns the AI Face WS listening socket' "$tlog") \
worker_adopted=$(grep -c 'inherited master socket' "$tlog") \
worker_bound_itself=$(grep -c 'bound by this process' "$tlog")"
    echo "worker_boots       : $(grep -c 'Booting worker' "$tlog")   (recycles = boots - 1)"
    echo "port_in_use_errors : $(grep -c 'already in use' "$tlog")"
  else
    echo "listener_ownership : (target log not found)"
  fi
  for f in "$ROOT/results/preload_verification.json" "$ROOT/results/config_differences.json" \
           "$ROOT/results/isolation_inside_pre-load.json" "$ROOT/results/isolation_outside_pre-load.json" \
           "$ROOT/results/isolation_inside_target-up.json" "$ROOT/results/isolation_outside_target-up.json" \
           "$ROOT/results/isolation_check.json" "$ROOT/results/precheck.json" \
           "$out/run_meta.json" "$out/baseline.json" "$out/startup_gate.json" "$out/watchdog_config.json" \
           "$out/stage_results.json" "$out/summary.json" "$out/reconciliation.json" \
           "$out/watchdog_summary.json" "$out/sentinel_summary.json" "$out/STOP.json"; do
    echo; echo "##### FILE: ${f#$ROOT/} #####"
    [ -f "$f" ] && cat "$f" || echo "(absent)"
  done
  echo; echo "##### FILE: results/round1/monitor.csv (every 10th sample) #####"
  [ -f "$out/monitor.csv" ] && awk 'NR==1 || NR%10==2' "$out/monitor.csv" || echo "(absent)"
  echo; echo "##### FILE: results/round1/sentinel_monitor.csv (every 10th sample) #####"
  [ -f "$out/sentinel_monitor.csv" ] && awk 'NR==1 || NR%10==2' "$out/sentinel_monitor.csv" || echo "(absent)"
  echo; echo "##### END DIGEST #####"
}

status() {
  echo "runner log tail:"; tail -5 "$RUNLOG" 2>/dev/null
  local live; live=$(ls "$ROOT/results/round1"/live_p*.json 2>/dev/null | head -1)
  [ -n "$live" ] && { echo "generator live snapshot:"; cat "$live"; }
  [ -f "$ROOT/results/round1/STOP.json" ] && { echo "STOP requested:"; cat "$ROOT/results/round1/STOP.json"; }
  echo "processes:"; pgrep -a -u "$TEST_USER" -f 'locust|gunicorn|watchdog.py|health_sentinel.py|postgres' 2>/dev/null | cut -c1-120
}

case "${1:---help}" in
  --preflight) preflight ;;
  --run)       need_root; mkdir -p "$BASE"; do_run 2>&1 | tee -a "$RUNLOG" ;;
  --digest)    digest ;;
  --status)    status ;;
  *) sed -n '2,30p' "${BASH_SOURCE[0]}" ;;
esac
