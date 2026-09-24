"""Pre-authenticate every synthetic parent OUTSIDE the timed round.

Strategy (isolated, test-only): tokens are issued by the application's own
`encode_token()` inside the target runtime, signed with the experiment's
randomly generated JWT_SECRET_KEY (secrets.json). The production signing key is
never involved, no rate limit is changed, and no forwarding header is spoofed.
The real HTTP login endpoint is still exercised for a handful of accounts in
precheck.py (within the unchanged 10/min limit).

Tokens are valid 24 h; the round must start before `expires_at`.
Output: <root>/secrets/tokens.json   (never copied into reports)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outside(root):
    import target
    cfg = common.load_config(root)
    env = target.build_env(cfg, common.load_secrets(root), ws_enabled=False)
    r = subprocess.run([target.venv_bin(root, 'python'), os.path.abspath(__file__), '--root', root, '--inside'],
                       cwd=os.path.join(root, 'app_src'), env=env)
    raise SystemExit(r.returncode)


def inside(root):
    from sqlalchemy.orm import joinedload
    from app import create_app
    from app.models import User
    from app.blueprints.mobile_api.utils import encode_token

    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
    usernames = [s['username'] for s in fx['students']] + [p['username'] for p in fx['precheck'].values()]
    app = create_app('production')
    with app.app_context():
        users = {u.username: u for u in User.query.execution_options(bypass_tenant_scope=True)
                 .options(joinedload(User.role)).filter(User.username.in_(usernames)).all()}
        missing = [u for u in usernames if u not in users]
        if missing:
            raise SystemExit(f'{len(missing)} synthetic users missing')
        tokens = {u: encode_token(users[u], 'access') for u in usernames}
    issued = dt.datetime.now(dt.timezone.utc)
    out = {'issued_at': issued.isoformat(), 'expires_at': (issued + dt.timedelta(hours=24)).isoformat(),
           'method': 'app encode_token() with experiment-only JWT_SECRET_KEY', 'tokens': tokens}
    json.dump(out, open(os.path.join(root, 'secrets', 'tokens.json'), 'w'))
    print(f'issued {len(tokens)} access tokens; expire {out["expires_at"]}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--inside', action='store_true')
    a = ap.parse_args()
    (inside if a.inside else outside)(os.path.abspath(a.root))
