# Attendance load probe (AI Face WebSocket + parent attendance reads)

Experiment-scoped tooling. Every resource is recorded in `<root>/manifest.json`;
nothing touches production services, production databases, real devices, or FCM.

## Files
| File | Purpose |
|---|---|
| `manifest.py` | experiment id, ownership markers, resource registry |
| `setup_env.py` | config + secrets, PostgreSQL (own cluster or dedicated DB/role), `git archive` source, firebase import guard, venvs |
| `target.py` | start/stop/status of the isolated app; `isolation-check` |
| `serve_waitress.py` | Windows-only stand-in for gunicorn (validation only) |
| `seed.py` | real migrations + 10 schools × 1,000 students, 20 devices, 10,000 parents, history |
| `preauth.py` | pre-issued access tokens (experiment JWT key; production limits untouched) |
| `precheck.py` | 7 focused correctness/isolation checks |
| `probe_core.py` | schedule, protocol, validation, accounting, stop protocol |
| `locustfile.py` | Locust adapter (real round, Linux) |
| `thread_driver.py` | thread adapter (local tooling validation only) |
| `watchdog.py` | independent guardrails, monitor.csv, generator termination |
| `reconcile.py` | event manifest ↔ database verification |
| `analyze.py` | per-stage results, summary, report.html |
| `run_round.py` | orchestrates one round |
| `reset_round.py` | removes one round's synthetic test-date rows (experiment DB only) |
| `cleanup.py` | manifest-restricted cleanup (dry run by default) |

## Run on the VPS — one reviewed runner (Hostinger Web console, no SSH needed)
```bash
bash vps_run.sh --preflight     # read-only: changes nothing, exits non-zero if blocked
bash vps_run.sh --run           # bootstrap + the authorized ~600 s round
bash vps_run.sh --status        # progress while it runs
bash vps_run.sh --digest        # print the result files to copy back
```
`vps_run.sh` pins the deployed revision, creates the unprivileged `attlt` account,
builds the isolated runtime, holds an **ephemeral private network namespace**
(`unshare --net`; no persistent netns, no veth, no firewall or routing change),
**proves isolation from both sides** (`netns_proof.py`) before any load, records
production↔experiment configuration differences without printing values
(`prod_config_probe.py`), runs the in-namespace watchdog plus a host-side
`health_sentinel.py` (production health + host memory, outside the namespace),
and stops the runtime afterwards while retaining data, tooling and reports.
Root is used only for the account, the namespace and configuration reads; the
app, PostgreSQL, generator, watchdog and sentinel all run unprivileged.

## Run on the VPS manually (as a NON-root existing account; nothing installed globally)
```bash
REPO=/path/to/checked-out/mecha-school          # read-only use; tooling under loadtest/attendance_probe
cd $REPO/loadtest/attendance_probe
python3 manifest.py /srv/attlt-experiments        # prints <root>; choose a disk path outside the live app dir
ROOT=<printed root>
python3 setup_env.py --root $ROOT --revision <deployed sha> --steps config,src,venv \
    --http-port 18180 --ws-port 18188 --pg-mode local-cluster --pg-port 55480 --target-requirements pinned
ATTLT_PG_BIN=/usr/lib/postgresql/16/bin $ROOT/venv-gen/bin/python setup_env.py --root $ROOT --steps pg,db
$ROOT/venv-gen/bin/python target.py isolation-check --root $ROOT
$ROOT/venv-gen/bin/python seed.py --root $ROOT
$ROOT/venv-gen/bin/python target.py start --root $ROOT
$ROOT/venv-gen/bin/python preauth.py --root $ROOT
$ROOT/venv-gen/bin/python precheck.py --root $ROOT
$ROOT/venv-gen/bin/python run_round.py --root $ROOT --round-name round1 --driver locust \
    --live-health-url https://<production-domain>/ops/health
$ROOT/venv-gen/bin/python target.py stop --root $ROOT
```
`--pg-mode existing` (with `ATTLT_PG_ADMIN_DSN` in the environment, never on the command line)
creates a dedicated role + `core_school_attendance_load_test` on an existing server instead.

## Private endpoints (gap 4)
The app binds HTTP to `127.0.0.1` (private) but the AI Face WebSocket server
binds `0.0.0.0` in application code (`app/services/ai_face_ws.py`), which must
not be changed. To keep the WS port private **without touching the firewall or
app code**, run the experiment inside an isolated network namespace:

- **Docker internal network** (`docker/compose.driver.yml`, `internal: true`,
  no published ports): `0.0.0.0` inside the container is confined to the private
  network, unreachable from the VPS public interface. This is the recommended
  private runtime and is exercised by the Locust driver check
  (`docker/container_check.py`).
- If Docker is unavailable, run target + generator inside one `unshare --net`
  namespace on the VPS so the WS `0.0.0.0` bind stays namespace-local.

Never publish 18180/18188 to the host, and never widen the firewall.

## Package & transfer to the VPS (no SSH access to this workstation required)
```bash
python make_package.py --out .        # -> attlt_tooling_<date>.tgz (+ .sha256)
# copy the tarball to the VPS by any means you control, verify the hash, unpack:
sha256sum -c attlt_tooling_<date>.tgz.sha256
tar xzf attlt_tooling_<date>.tgz      # -> attendance_probe/
```
Discovery first: run `attendance_probe/HOSTINGER_DISCOVERY.sh` (read-only) to
learn the service name, app dir, deployed SHA, effective settings, and isolation
options; then follow "Run on the VPS" with the discovered `<deployed sha>`.

## Cleanup (only when explicitly requested)
```bash
$ROOT/venv-gen/bin/python cleanup.py --root $ROOT                       # plan
$ROOT/venv-gen/bin/python cleanup.py --root $ROOT --execute --delete-db # stop + delete runtime, keep reports
```
