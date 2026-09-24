#!/usr/bin/env bash
# READ-ONLY Hostinger discovery block. Paste into the Hostinger Web console.
# Prints NO secrets: env values are shown as present/absent only; DB URL is shown
# as scheme+host+port+db (never user/password). Nothing is modified, started,
# stopped, installed, or written. Only outbound request: HEAD/GET to the app's
# own health endpoint and a reachability probe to pypi.org.
set +e
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo -n"
CAND_DOMAIN="school.smartcoreiq.cloud"   # candidate to VERIFY, not to assume

echo "===== 1. OS / resources ====="
. /etc/os-release 2>/dev/null; echo "OS: $PRETTY_NAME"; uname -r; echo "CPU cores: $(nproc)"
free -m | awk 'NR==1||/Mem|Swap/'
awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "MemAvailable: %d MB of %d MB = %.1f%% (20%% floor is enforced)\n",a/1024,t/1024,100*a/t}' /proc/meminfo
echo "disk /:"; df -h / | tail -1
echo "load/uptime:$(uptime)"; timedatectl 2>/dev/null | grep -iE 'time zone'
echo "host public IPv4:"; ip -4 -o addr show scope global 2>/dev/null | awk '{print "  "$2" "$4}'

echo; echo "===== 2. running services (no name guessing: listeners are authoritative) ====="
systemctl list-units --type=service --state=running --no-pager --plain 2>/dev/null | awk 'NR>1&&NF{print "  "$1}' | head -40
echo "--- ALL listening TCP sockets with owning process ---"
$SUDO ss -ltnp 2>/dev/null || ss -ltn 2>/dev/null
echo "--- systemd unit that owns each python/gunicorn listener ---"
for p in $($SUDO ss -ltnp 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
  cmd=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | cut -c1-140)
  case "$cmd" in *python*|*gunicorn*|*uvicorn*|*waitress*)
    unit=$($SUDO systemctl status $p --no-pager 2>/dev/null | head -1 | awk '{print $2}')
    echo "  pid=$p unit=${unit:-?} cmd=$cmd" ;;
  esac
done

echo; echo "===== 3. deployed code: dir, revision, entrypoint ====="
APP_PID=$(pgrep -f 'gunicorn.*(wsgi|app|run):|gunicorn: master|wsgi:application' | head -1)
[ -z "$APP_PID" ] && APP_PID=$(pgrep -f 'gunicorn' | head -1)
echo "app pid: ${APP_PID:-UNKNOWN}"
[ -n "$APP_PID" ] && echo "app cmdline: $(tr '\0' ' ' < /proc/$APP_PID/cmdline 2>/dev/null | cut -c1-200)"
APPDIR=$($SUDO pwdx "$APP_PID" 2>/dev/null | awk '{print $2}')
[ -z "$APPDIR" ] && APPDIR=$($SUDO readlink -f /proc/$APP_PID/cwd 2>/dev/null)
echo "app dir (discovered): ${APPDIR:-UNKNOWN}"
if [ -n "$APPDIR" ]; then
  ls -1 "$APPDIR" 2>/dev/null | head -20 | sed 's/^/  /'
  if [ -d "$APPDIR/.git" ]; then
    echo "deployed SHA: $(git -C "$APPDIR" rev-parse HEAD 2>/dev/null)"
    git -C "$APPDIR" log -1 --format='deployed commit: %h %s (%cd)' 2>/dev/null
    git -C "$APPDIR" status --porcelain 2>/dev/null | head -5 | sed 's/^/  uncommitted: /'
    git -C "$APPDIR" remote -v 2>/dev/null | head -2 | sed 's/^/  remote: /'
  else
    echo "(no .git in app dir — note the deploy method; a git checkout is needed for git archive)"
  fi
fi
echo "--- gunicorn config knobs (no secrets) ---"
[ -n "$APPDIR" ] && grep -nE '^\s*(workers|threads|worker_class|timeout|max_requests|max_requests_jitter|bind)' \
  "$APPDIR/gunicorn.conf.py" 2>/dev/null | sed 's/^/  /'
$SUDO systemctl cat "$(basename "${APPDIR:-app}")" --no-pager 2>/dev/null | grep -iE '^(ExecStart|WorkingDirectory|User|EnvironmentFile)' | sed 's/^/  unit: /'

echo; echo "===== 4. effective runtime env (PRESENCE only, never values) ====="
if [ -n "$APP_PID" ]; then
  for k in FLASK_ENV PORT WEB_CONCURRENCY GUNICORN_THREADS GUNICORN_TIMEOUT AIFACE_WS_ENABLED AIFACE_WS_PORT \
           ATTENDANCE_SCHEDULER_DISABLED HIKVISION_AUTO_SYNC REDIS_URL DURABLE_PUSH_QUEUE_ENABLED \
           SQLALCHEMY_POOL_SIZE SQLALCHEMY_MAX_OVERFLOW RATELIMIT_STORAGE_URI OPS_METRICS_TOKEN \
           FIREBASE_SERVICE_ACCOUNT_JSON GOOGLE_APPLICATION_CREDENTIALS FCM_SERVICE_ACCOUNT_JSON \
           SUPABASE_URL SERVER_NAME PREFERRED_URL_SCHEME DATABASE_URL; do
    v=$($SUDO tr '\0' '\n' < /proc/$APP_PID/environ 2>/dev/null | grep -E "^$k=" | head -1)
    if [ -z "$v" ]; then echo "  $k = (unset)"
    elif [ "$k" = "DATABASE_URL" ]; then
      echo "$v" | sed -E 's#^DATABASE_URL=([a-z+]+)://[^@]*@([^:/]+):?([0-9]*)/([^?]*).*#  DATABASE_URL = scheme=\1 host=\2 port=\3 db=\4#'
    elif [ "$k" = "SERVER_NAME" ] || [ "$k" = "PORT" ] || [ "$k" = "FLASK_ENV" ] || [ "$k" = "PREFERRED_URL_SCHEME" ]; then
      echo "  $v"
    else echo "  $k = (set)"; fi
  done
  echo "  .env files present in app dir: $(ls -a "${APPDIR:-/nonexistent}" 2>/dev/null | grep -c '^\.env')"
else echo "  (app process not found)"; fi

echo; echo "===== 5. domain verification (does $CAND_DOMAIN really serve THIS deployment?) ====="
echo "--- server_name / listen / proxy_pass from reverse-proxy config (no secrets) ---"
grep -rhnE '^\s*(server_name|listen|proxy_pass)' /etc/nginx/sites-enabled /etc/nginx/conf.d /etc/nginx/nginx.conf 2>/dev/null | sed 's/^/  nginx: /' | head -30
[ -f /etc/caddy/Caddyfile ] && grep -nE '^[^ #].*\{|reverse_proxy' /etc/caddy/Caddyfile 2>/dev/null | sed 's/^/  caddy: /' | head -20
echo "--- TLS certificates issued on this host ---"
ls -1 /etc/letsencrypt/live 2>/dev/null | sed 's/^/  cert: /' || echo "  (no letsencrypt dir)"
echo "--- DNS vs this host ---"
echo "  DNS A($CAND_DOMAIN): $(getent ahostsv4 "$CAND_DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
echo "  local global IPv4 : $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
echo "--- live responses (read-only GET/HEAD) ---"
for u in "https://$CAND_DOMAIN/ops/health" "https://$CAND_DOMAIN/login"; do
  echo "  $u -> $(curl -sS -o /dev/null -m 10 -w 'http=%{http_code} ip=%{remote_ip} ssl=%{ssl_verify_result} ct=%{content_type}' "$u" 2>&1 | tail -1)"
done
APP_PORT=$($SUDO ss -ltnp 2>/dev/null | grep -E "pid=${APP_PID:-99999999}," | grep -oE ':[0-9]+ ' | tr -d ': ' | head -1)
echo "  app listens on port: ${APP_PORT:-UNKNOWN}"
[ -n "$APP_PORT" ] && echo "  loopback :$APP_PORT/ops/health -> $(curl -sS -o /dev/null -m 10 -w 'http=%{http_code}' -H "Host: $CAND_DOMAIN" "http://127.0.0.1:$APP_PORT/ops/health" 2>&1 | tail -1)"

echo; echo "===== 6. attendance receiver (AI Face WebSocket, default :7788) ====="
$SUDO ss -ltnp 2>/dev/null | grep -E ':7788\b' || echo "  (nothing listening on 7788)"
echo "  established device connections on 7788: $($SUDO ss -tn state established '( sport = :7788 )' 2>/dev/null | tail -n +2 | wc -l)"

echo; echo "===== 7. PostgreSQL ====="
$SUDO ss -ltnp 2>/dev/null | grep -E ':5432\b' || echo "  (no local postgres on 5432 — DB is likely remote/managed)"
ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sed 's/^/  pg bin: /' || echo "  (no local postgres server binaries -> initdb/pg_ctl unavailable)"
command -v psql >/dev/null && echo "  psql: $(psql --version)"
command -v initdb >/dev/null || ls /usr/lib/postgresql/*/bin/initdb >/dev/null 2>&1 && echo "  initdb present (dedicated local cluster possible)"

echo; echo "===== 8. prerequisites for the isolated test instance ====="
command -v docker >/dev/null && echo "  docker: $(docker --version) (private internal-network runtime possible)" || echo "  docker: NOT installed"
command -v unshare >/dev/null && echo "  unshare: present (netns fallback for the WS 0.0.0.0 bind)"
command -v python3 >/dev/null && echo "  python3: $(python3 --version)"
python3 -c 'import venv' 2>/dev/null && echo "  python3 venv module: OK" || echo "  python3 venv module: MISSING (need python3-venv)"
command -v git >/dev/null && echo "  git: $(git --version)"
echo "  pypi reachable: $(curl -sS -o /dev/null -m 10 -w '%{http_code}' https://pypi.org/simple/ 2>&1 | tail -1)"
echo "  firewall (read-only view):"; $SUDO ufw status 2>/dev/null | head -3 | sed 's/^/    /' || $SUDO iptables -S 2>/dev/null | head -5 | sed 's/^/    /' || echo "    (no ufw/iptables visibility)"
echo "  candidate disks for the experiment root (need >=10 GB free):"
df -h /srv /opt /home /var/tmp 2>/dev/null | sed 's/^/    /'
echo "  non-root accounts:"; awk -F: '$3>=1000 && $3<65534 {print "    "$1" uid="$3" shell="$7}' /etc/passwd

echo; echo "===== done (read-only; nothing was modified) ====="
