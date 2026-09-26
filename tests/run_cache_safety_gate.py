"""Run the focused cache-safety gate.

    .venv\\Scripts\\python.exe tests/run_cache_safety_gate.py [extra pytest args]

``tests/cache_safety_gate.txt`` is the documented source of truth: which areas
the gate covers, which pre-existing failures are deselected, and the evidence
for each.  pytest's own ``@argfile`` support comes from argparse and does not
strip comments, so this runner does that one job and hands the rest to pytest.

Exit code is pytest's own, so it drops straight into CI.
"""
import subprocess
import sys
from pathlib import Path

GATE_FILE = Path(__file__).with_name('cache_safety_gate.txt')
REPO_ROOT = Path(__file__).resolve().parent.parent


def gate_args():
    args = []
    for line in GATE_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            args.append(line)
    return args


def main():
    args = gate_args()
    cmd = [sys.executable, '-m', 'pytest', *args, *sys.argv[1:]]
    print(f'cache-safety gate: {len(args)} arguments from {GATE_FILE.name}')
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


if __name__ == '__main__':
    raise SystemExit(main())
