"""Produce a self-contained tarball of the tooling for transfer to the VPS.

The package contains ONLY the tooling source (no experiment data, no secrets, no
venvs, no results, no local databases). On the VPS it is unpacked and driven by
the steps in README.md — execution never requires giving anyone SSH access to
this workstation.

    python make_package.py --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
import time

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
INCLUDE = ('.py', '.md', '.sh', '.yml', '.txt', '.bundle')
SKIP_DIRS = {'__pycache__', 'out', 'results'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=TOOL_DIR)
    a = ap.parse_args()
    stamp = time.strftime('%Y%m%d')
    tar_path = os.path.join(os.path.abspath(a.out), f'attlt_tooling_{stamp}.tgz')
    files = []
    for base, dirs, names in os.walk(TOOL_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if n.endswith(INCLUDE) and n != os.path.basename(tar_path):
                files.append(os.path.join(base, n))
    with tarfile.open(tar_path, 'w:gz') as tf:
        for f in sorted(files):
            arc = 'attendance_probe/' + os.path.relpath(f, TOOL_DIR).replace(os.sep, '/')
            tf.add(f, arcname=arc)
    sha = hashlib.sha256(open(tar_path, 'rb').read()).hexdigest()
    print(f'package: {tar_path}')
    print(f'sha256:  {sha}')
    print(f'files:   {len(files)}')
    # LF only: this file is consumed by `sha256sum -c` on Linux, which would treat
    # a trailing CR as part of the file name and fail to open it.
    with open(tar_path + '.sha256', 'w', newline='\n') as fh:
        fh.write(f'{sha}  {os.path.basename(tar_path)}\n')


if __name__ == '__main__':
    main()
