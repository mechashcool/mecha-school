"""Per-stage analysis of one round → stage_results.{json,csv}, summary.json, report.html.

Inputs (all in the round output directory): round_start.json, round_end_p*.json,
requests_p*.csv, events_p*.jsonl, live_history_p*.jsonl, stage_marks_p*.jsonl,
control_p*.jsonl, monitor.csv, baseline.json, watchdog_*.json(l),
reconciliation.json.
Percentiles with fewer than 100 samples are flagged low_confidence.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

OPS = ('ws_sendlog_ack', 'parent_attendance_read', 'parent_cross_school_denied', 'ws_reg_ack')


def pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_jsonl(pattern):
    out = []
    for f in sorted(glob.glob(pattern)):
        for line in open(f, encoding='utf-8'):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def stage_windows(start, end_rel, stage_limit, mode_at, mode):
    wins = []
    for no, s, e, target in common.STAGES[:stage_limit]:
        if s >= end_rel:
            break
        stop = min(e, end_rel)
        if mode in ('recovery', 'halt') and mode_at is not None and mode_at < e:
            stop = min(stop, mode_at)
        if stop <= s:
            break
        wins.append({'stage': no, 'start': s, 'end': stop, 'planned_end': e, 'target': target,
                     'truncated': stop < e})
        if stop < e:
            break
    last = wins[-1]['end'] if wins else 0
    rec_end = min(end_rel, last + 60) if mode != 'normal' else min(end_rel, common.STAGES[stage_limit - 1][2] + 60)
    wins.append({'stage': 'recovery', 'start': last, 'end': max(last, end_rel), 'planned_end': rec_end,
                 'target': common.RECOVERY[3], 'truncated': False})
    return wins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    root, out = os.path.abspath(a.root), os.path.abspath(a.out)
    cfg = common.load_config(root)
    start = json.load(open(os.path.join(out, 'round_start.json')))
    ends = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(out, 'round_end_p*.json')))]
    end_rel = max((e['end_rel'] for e in ends), default=None)
    mode = ends[0]['mode'] if ends else 'unknown'
    mode_at = ends[0]['mode_at_rel'] if ends else None
    mode_reason = ends[0]['mode_reason'] if ends else None
    t0 = start['start_wall_epoch']

    reqs = []
    for f in sorted(glob.glob(os.path.join(out, 'requests_p*.csv'))):
        with open(f, newline='', encoding='utf-8') as fh:
            reqs.extend(csv.DictReader(fh))
    if end_rel is None:
        end_rel = max((fnum(r['rel_s']) or 0) for r in reqs) if reqs else 0
    events = {}
    for e in load_jsonl(os.path.join(out, 'events_p*.jsonl')):
        events[e['k']] = {**events.get(e['k'], {}), **e}
    live = load_jsonl(os.path.join(out, 'live_history_p*.jsonl'))
    marks = load_jsonl(os.path.join(out, 'stage_marks_p*.jsonl'))
    control = load_jsonl(os.path.join(out, 'control_p*.jsonl'))
    mon = []
    if os.path.exists(os.path.join(out, 'monitor.csv')):
        with open(os.path.join(out, 'monitor.csv'), newline='') as fh:
            mon = list(csv.DictReader(fh))
    for m in mon:
        m['rel'] = dt.datetime.fromisoformat(m['wall_utc']).timestamp() - t0
    recon = json.load(open(os.path.join(out, 'reconciliation.json'))) if os.path.exists(os.path.join(out, 'reconciliation.json')) else {}
    baseline = json.load(open(os.path.join(out, 'baseline.json'))) if os.path.exists(os.path.join(out, 'baseline.json')) else {}
    wd = json.load(open(os.path.join(out, 'watchdog_summary.json'))) if os.path.exists(os.path.join(out, 'watchdog_summary.json')) else {}

    stage_limit = start['stage_limit']
    wins = stage_windows(0, end_rel, stage_limit, mode_at, mode)
    results = []
    for w in wins:
        s, e = w['start'], w['end']
        dur = max(0.001, e - s)
        in_w = [r for r in reqs if s <= (fnum(r['rel_s']) or -1) < e]
        ops = {}
        for op in OPS:
            rs = [r for r in in_w if r['op'] == op]
            lat = [fnum(r['latency_ms']) for r in rs if r['ok'] == '1' and fnum(r['latency_ms']) is not None]
            ops[op] = {'n': len(rs), 'ok': sum(1 for r in rs if r['ok'] == '1'),
                       'errors': sum(1 for r in rs if r['ok'] != '1'),
                       'p50_ms': pct(lat, 50), 'p95_ms': pct(lat, 95), 'p99_ms': pct(lat, 99),
                       'rate_per_s': round(len(rs) / dur, 2), 'low_confidence': len(lat) < 100}
        hold = [r for r in in_w if r['op'] == 'parent_attendance_read' and r['ok'] == '1'
                and (fnum(r['rel_s']) or 0) >= s + common.RAMP_SECONDS]
        hold_lat = [fnum(r['latency_ms']) for r in hold]
        ramp_end = min(e, s + common.RAMP_SECONDS)
        if w['stage'] != 'recovery':
            prev = 0 if w['stage'] == 1 else common.STAGES[w['stage'] - 2][3]
            ks = range(prev, w['target'])
            evs = [events.get(k, {}) for k in ks]
            st = collections.Counter(ev.get('status', 'not_in_manifest') for ev in evs)
            sent = sum(1 for ev in evs if ev.get('sent_rel') is not None)
            delays = [ev['send_delay_s'] for ev in evs if ev.get('send_delay_s') is not None]
            rs_stage = recon.get('per_stage', {}).get(str(w['stage']), {})
            intended_rate = common.intended_event_rate(common.STAGES[w['stage'] - 1])
            ev_block = {'scheduled': len(ks), 'sent': sent, 'acked': st['acked'],
                        'verified_committed': rs_stage.get('verified_committed'),
                        'sent_not_committed': rs_stage.get('sent_not_committed', 0),
                        'status': dict(st), 'intended_events_per_s': round(intended_rate, 3),
                        'intended_events_per_min': round(intended_rate * 60, 1),
                        'achieved_sent_per_s_over_ramp': round(sent / common.RAMP_SECONDS, 3),
                        'verified_committed_per_s_over_ramp': (round(rs_stage['verified_committed'] / common.RAMP_SECONDS, 3)
                                                               if rs_stage.get('verified_committed') is not None else None),
                        'send_delay_p95_s': pct(delays, 95), 'send_delay_max_s': round(max(delays), 3) if delays else None}
        else:
            ev_block = {}
        lv = [x for x in live if s <= x['rel'] < e]
        mw = [m for m in mon if s <= m['rel'] < e]

        def magg(key, fn):
            vals = [fnum(m.get(key)) for m in mw if fnum(m.get(key)) is not None]
            return round(fn(vals), 2) if vals else None
        mark = next((m for m in marks if m['stage'] == w['stage']), None)
        res = {
            'stage': w['stage'], 'start_rel_s': round(s, 1), 'end_rel_s': round(e, 1), 'duration_s': round(dur, 1),
            'truncated': w['truncated'], 'target_cumulative_students': w['target'],
            'achieved_cumulative_students_sent': sum(1 for ev in events.values()
                                                     if ev.get('sent_rel') is not None and ev['sent_rel'] < e),
            'active_parents_max': max((x.get('active_parents', 0) for x in lv), default=None),
            'active_parents_at_end': lv[-1].get('active_parents') if lv else None,
            'target_parents_at_end': lv[-1].get('target_parents') if lv else None,
            'events': ev_block, 'ops': ops,
            'parent_read_hold_period': {'n': len(hold_lat), 'p95_ms': pct(hold_lat, 95), 'p99_ms': pct(hold_lat, 99),
                                        'rate_per_s': round(len(hold) / max(0.001, e - (s + common.RAMP_SECONDS)), 2)
                                        if e > s + common.RAMP_SECONDS else None},
            'backlog_max': max((x.get('event_backlog', 0) + x.get('pending_acks', 0) for x in lv), default=None),
            'missed_read_schedules_cum': (lv[-1].get('counters') or {}).get('parent_read.missed_schedule', 0) if lv else None,
            'ws_disconnects_cum': (lv[-1].get('devices') or {}).get('disconnects') if lv else None,
            'ws_reconnects_cum': (lv[-1].get('devices') or {}).get('reconnects') if lv else None,
            'resources': {
                'host_cpu_pct_avg': magg('host_cpu_pct', lambda v: sum(v) / len(v)), 'host_cpu_pct_max': magg('host_cpu_pct', max),
                'host_mem_available_pct_min': magg('host_mem_available_pct', min),
                'target_cpu_pct_max': magg('target_cpu_pct', max), 'target_rss_mb_max': magg('target_rss_mb', max),
                'pg_cpu_pct_max': magg('pg_cpu_pct', max),
                'db_connections_max': magg('db_connections', max), 'db_active_max': magg('db_active', max),
                'disk_write_mb_s_max': magg('disk_write_mb_s', max), 'disk_io_time_ratio_max': magg('disk_io_time_ratio', max),
                'generator_cpu_pct_max': magg('generator_cpu_pct', max), 'generator_rss_mb_max': magg('generator_rss_mb', max),
                'monitor_samples': len(mw)},
            'stage_completion_mark': mark,
        }
        stop_in_stage = mode_at is not None and s <= mode_at < (w['planned_end'] if w['stage'] != 'recovery' else e + 1)
        if w['stage'] == 'recovery':
            res['verdict'] = 'observation'
        elif stop_in_stage and mode in ('recovery', 'halt'):
            res['verdict'] = 'fail'
            res['verdict_reason'] = mode_reason
        elif mark and mark.get('completed') and not w['truncated']:
            lowc = ops['parent_attendance_read']['low_confidence'] or ops['ws_sendlog_ack']['low_confidence']
            res['verdict'] = 'pass (low sample count)' if lowc else 'pass'
        else:
            res['verdict'] = 'inconclusive'
        results.append(res)

    passed = [r for r in results if isinstance(r['stage'], int) and r['verdict'].startswith('pass')]

    # ── three SEPARATE verdicts (never conflated) ──────────────────────────────
    wcfg = json.load(open(os.path.join(out, 'watchdog_config.json'))) if os.path.exists(os.path.join(out, 'watchdog_config.json')) else {}
    compliance = (wcfg.get('thresholds') or {}).get('compliance_mode')
    gate = wcfg.get('startup_gate') or (json.load(open(os.path.join(out, 'startup_gate.json')))
                                        if os.path.exists(os.path.join(out, 'startup_gate.json')) else None)
    functional_ok = recon.get('correct') is True and not [r for r in results if isinstance(r['stage'], int)
                                                          and r['verdict'] == 'fail']
    latency_ok = all(r['verdict'].startswith('pass') for r in results if isinstance(r['stage'], int)) and mode == 'normal'
    resource_guard = 'ENFORCED (no breach)' if (compliance == 'ENFORCED' and not wd.get('fired')) else (
        'ENFORCED (breach stopped the run)' if compliance == 'ENFORCED' else 'OVERRIDDEN — safety NOT certified')
    verdicts = {
        'functional_correctness': 'pass' if functional_ok else 'fail',
        'latency_success': 'pass' if latency_ok else ('n/a (stopped early)' if mode != 'normal' else 'fail'),
        'resource_guard_compliance': resource_guard,
        'overall_safety': ('passed' if (compliance == 'ENFORCED' and functional_ok and not wd.get('fired'))
                           else 'NOT PASSED — resource guard overridden or a breach/failure occurred'),
        'note': 'overall_safety is passed only when the resource guard was ENFORCED (not overridden), '
                'no guard fired, and correctness held. Latency and correctness are reported separately.',
    }
    summary = {
        'verdicts': verdicts, 'resource_guard_compliance_mode': compliance, 'startup_gate': gate,
        'experiment_id': cfg['experiment_id'], 'round_start_utc': start['start_utc'],
        'round_end_utc': ends[0]['end_utc'] if ends else None, 'duration_s': round(end_rel, 1),
        'test_date': start['test_date'], 'stage_limit': stage_limit, 'final_mode': mode, 'stop_reason': mode_reason,
        'stop_at_rel_s': mode_at, 'control_log': control, 'watchdog_fired': wd.get('fired', []),
        'highest_stage_passed': passed[-1]['stage'] if passed else None,
        'highest_passed_students': passed[-1]['target_cumulative_students'] if passed else None,
        'highest_passed_parents': passed[-1]['active_parents_max'] if passed else None,
        'highest_passed_intended_events_per_s': passed[-1]['events']['intended_events_per_s'] if passed else None,
        'highest_passed_verified_committed_per_s': passed[-1]['events'].get('verified_committed_per_s_over_ramp') if passed else None,
        'reconciliation_correct': recon.get('correct'), 'baseline': baseline,
    }
    json.dump(results, open(os.path.join(out, 'stage_results.json'), 'w'), indent=2, default=str)
    json.dump(summary, open(os.path.join(out, 'summary.json'), 'w'), indent=2, default=str)
    flat_keys = ['stage', 'start_rel_s', 'end_rel_s', 'duration_s', 'target_cumulative_students',
                 'achieved_cumulative_students_sent', 'active_parents_max', 'backlog_max', 'verdict']
    with open(os.path.join(out, 'stage_results.csv'), 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(flat_keys + ['events_scheduled', 'events_sent', 'events_acked', 'events_verified_committed',
                                'intended_ev_s', 'verified_ev_s', 'send_delay_p95_s']
                   + [f'{op}_{m}' for op in OPS[:2] for m in ('n', 'errors', 'p50_ms', 'p95_ms', 'p99_ms', 'rate_per_s')]
                   + ['read_hold_p95_ms', 'host_cpu_max', 'mem_avail_min_pct', 'target_cpu_max', 'target_rss_max',
                      'pg_cpu_max', 'db_conn_max', 'gen_cpu_max'])
        for r in results:
            ev = r['events']
            w.writerow([r.get(k) for k in flat_keys]
                       + [ev.get('scheduled'), ev.get('sent'), ev.get('acked'), ev.get('verified_committed'),
                          ev.get('intended_events_per_s'), ev.get('verified_committed_per_s_over_ramp'), ev.get('send_delay_p95_s')]
                       + [r['ops'][op][m] for op in OPS[:2] for m in ('n', 'errors', 'p50_ms', 'p95_ms', 'p99_ms', 'rate_per_s')]
                       + [r['parent_read_hold_period']['p95_ms'], r['resources']['host_cpu_pct_max'],
                          r['resources']['host_mem_available_pct_min'], r['resources']['target_cpu_pct_max'],
                          r['resources']['target_rss_mb_max'], r['resources']['pg_cpu_pct_max'],
                          r['resources']['db_connections_max'], r['resources']['generator_cpu_pct_max']])
    write_html(out, summary, results, reqs, mon, live)
    print(json.dumps({k: summary[k] for k in ('final_mode', 'stop_reason', 'highest_stage_passed',
                                              'highest_passed_students', 'reconciliation_correct')}, indent=2))


def svg_line(title, series, xmax, ylabel, width=760, height=220):
    """series: list of (name, color, [(x, y), ...])"""
    pts_all = [p for _, _, pts in series for p in pts if p[1] is not None]
    ymax = max([p[1] for p in pts_all] + [1]) * 1.1
    L, B = 55, 30
    W, H = width - L - 10, height - B - 25

    def xy(x, y):
        return L + W * x / max(1, xmax), 20 + H - H * y / ymax
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
             f'<text x="{L}" y="14" class="t">{html.escape(title)}</text>']
    for i in range(5):
        yv = ymax * i / 4
        _, yy = xy(0, yv)
        parts.append(f'<line x1="{L}" x2="{L + W}" y1="{yy:.1f}" y2="{yy:.1f}" class="g"/>'
                     f'<text x="{L - 6}" y="{yy + 4:.1f}" class="a" text-anchor="end">{yv:.0f}</text>')
    for no, s, e, _t in common.STAGES:
        if s <= xmax:
            xx, _ = xy(s, 0)
            parts.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="20" y2="{20 + H}" class="s"/>'
                         f'<text x="{xx + 2:.1f}" y="{20 + H + 12}" class="a">{no}</text>')
    for name, color, pts in series:
        pts = [p for p in pts if p[1] is not None]
        if pts:
            d = ' '.join(f'{xy(x, y)[0]:.1f},{xy(x, y)[1]:.1f}' for x, y in pts)
            parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.6"/>')
    lx = L
    for name, color, _ in series:
        parts.append(f'<rect x="{lx}" y="{height - 12}" width="10" height="3" fill="{color}"/>'
                     f'<text x="{lx + 14}" y="{height - 8}" class="a">{html.escape(name)}</text>')
        lx += 20 + 7 * len(name)
    parts.append(f'<text x="8" y="{20 + H / 2}" class="a" transform="rotate(-90 8 {20 + H / 2})">{html.escape(ylabel)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def rolling(reqs, op, xmax, step=5, win=10, p=95):
    out = []
    by = [(fnum(r['rel_s']), fnum(r['latency_ms'])) for r in reqs if r['op'] == op and r['ok'] == '1']
    by.sort()
    t = 0
    j0 = 0
    while t <= xmax:
        vals = [v for x, v in by if t - win <= x < t and v is not None]
        out.append((t, pct(vals, p) if len(vals) >= 5 else None))
        t += step
    return out


def write_html(out, summary, results, reqs, mon, live):
    xmax = summary['duration_s'] or 600
    charts = [
        svg_line('P95 latency (10 s rolling, ms)', [
            ('parent attendance read', 'var(--c1)', rolling(reqs, 'parent_attendance_read', xmax)),
            ('device sendlog ack', 'var(--c2)', rolling(reqs, 'ws_sendlog_ack', xmax))], xmax, 'ms'),
        svg_line('Load introduced', [
            ('active parents', 'var(--c1)', [(x['rel'], x.get('active_parents')) for x in live]),
            ('events sent (cumulative)', 'var(--c2)', [(x['rel'], sum(v for k, v in (x.get('events_status') or {}).items()
                                                                       if not k.startswith('unsent'))) for x in live])], xmax, 'count'),
        svg_line('Host / processes CPU %', [
            ('host', 'var(--c1)', [(m['rel'], fnum(m.get('host_cpu_pct'))) for m in mon]),
            ('target app', 'var(--c2)', [(m['rel'], fnum(m.get('target_cpu_pct'))) for m in mon]),
            ('postgres', 'var(--c3)', [(m['rel'], fnum(m.get('pg_cpu_pct'))) for m in mon]),
            ('generator', 'var(--c4)', [(m['rel'], fnum(m.get('generator_cpu_pct'))) for m in mon])], xmax, '% (per-core sum for processes)'),
        svg_line('Memory available % / DB connections', [
            ('host MemAvailable %', 'var(--c1)', [(m['rel'], fnum(m.get('host_mem_available_pct'))) for m in mon]),
            ('DB connections', 'var(--c2)', [(m['rel'], fnum(m.get('db_connections'))) for m in mon])], xmax, ''),
    ]
    rows = []
    for r in results:
        ev = r['events']
        ro = r['ops']['parent_attendance_read']
        wo = r['ops']['ws_sendlog_ack']
        rows.append('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in (
            r['stage'], r['duration_s'], r['target_cumulative_students'], r['achieved_cumulative_students_sent'],
            r['active_parents_max'], ev.get('scheduled', ''), ev.get('sent', ''), ev.get('acked', ''),
            ev.get('verified_committed', ''), ev.get('intended_events_per_s', ''),
            f"{wo['n']} / {wo['p50_ms']} / {wo['p95_ms']} / {wo['p99_ms']}",
            f"{ro['n']} / {ro['p50_ms']} / {ro['p95_ms']} / {ro['p99_ms']}", ro['rate_per_s'],
            r['resources']['host_cpu_pct_max'], r['resources']['db_connections_max'], r['verdict'])) + '</tr>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Attendance probe {html.escape(summary['experiment_id'])}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>:root{{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--grid:#e3e2dc;--c1:#2c6fbb;--c2:#d0661c;--c3:#3d8f5a;--c4:#8a5bb5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1a1a18;--fg:#ecebe6;--mut:#a3a29c;--grid:#34332f}}}}
body{{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:0;padding:16px;max-width:1100px;margin-inline:auto}}
svg{{width:100%;height:auto;max-width:760px;display:block;margin:12px 0}} .t{{fill:var(--fg);font-size:12px;font-weight:600}}
.a{{fill:var(--mut);font-size:10px}} .g{{stroke:var(--grid)}} .s{{stroke:var(--grid);stroke-dasharray:3 3}}
.tbl{{overflow-x:auto}} table{{border-collapse:collapse;font-size:12px}} td,th{{border-bottom:1px solid var(--grid);padding:4px 6px;text-align:right;white-space:nowrap}}
code{{font-size:12px}}</style></head><body>
<h1>Attendance load probe — {html.escape(summary['experiment_id'])}</h1>
<p>Round start {html.escape(str(summary['round_start_utc']))} UTC · duration {summary['duration_s']} s · test date {summary['test_date']} ·
final mode <b>{html.escape(str(summary['final_mode']))}</b>{(' — ' + html.escape(str(summary['stop_reason']))) if summary['stop_reason'] else ''}</p>
<p>Functional correctness: <b>{html.escape(summary['verdicts']['functional_correctness'])}</b> ·
Latency success: <b>{html.escape(summary['verdicts']['latency_success'])}</b> ·
Resource-guard compliance: <b>{html.escape(summary['verdicts']['resource_guard_compliance'])}</b> ·
Overall safety: <b>{html.escape(summary['verdicts']['overall_safety'])}</b></p>
{''.join(charts)}
<div class="tbl"><table><thead><tr><th>stage</th><th>dur s</th><th>target students</th><th>sent (cum)</th><th>parents max</th>
<th>scheduled</th><th>sent</th><th>acked</th><th>verified</th><th>intended ev/s</th><th>ack n/p50/p95/p99 ms</th>
<th>read n/p50/p95/p99 ms</th><th>reads/s</th><th>host CPU max</th><th>DB conn max</th><th>verdict</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p>Raw data: stage_results.json/csv, requests_p*.csv, events_p*.jsonl, monitor.csv, reconciliation.json.</p>
</body></html>"""
    open(os.path.join(out, 'report.html'), 'w', encoding='utf-8').write(doc)


if __name__ == '__main__':
    main()
