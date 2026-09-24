"""Deterministic checks of the resource-guard logic (no psutil, no real load).

Feeds simulated monitoring samples and a fake clock through the pure rules in
guard_rules.py and asserts:
  1. unsafe startup (baseline below the memory floor) is REJECTED
  2. an explicit degraded-host override proceeds but marks compliance OVERRIDDEN
  3. a safe baseline is accepted with compliance ENFORCED
  4. a sustained memory breach (>= 10 s below floor) fires; a shorter dip does not
  5. a sustained CPU breach fires; recovery (condition clears) resets it
  6. build_thresholds does NOT auto-lower the floor from the baseline
Run: python test_guard.py    (exit 0 = all pass)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard_rules as g

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


# 1. unsafe startup rejected (memory-starved host like the local Windows box)
gate = g.startup_gate({'host': {'mem_available_pct_min': 4.85, 'disk_free_gb': 42}}, min_mem_pct=20.0)
check('1_unsafe_baseline_rejected', gate['ok'] is False and gate['compliance'] == g.COMPLIANCE_ENFORCED
      and any('MemAvailable' in r for r in gate['reasons']))

# 1b. unsafe disk also rejected
gate_d = g.startup_gate({'host': {'mem_available_pct_min': 55, 'disk_free_gb': 3}}, min_mem_pct=20.0, min_disk_gb=10)
check('1b_unsafe_disk_rejected', gate_d['ok'] is False and any('disk' in r for r in gate_d['reasons']))

# 2. explicit override proceeds but OVERRIDDEN
gate_o = g.startup_gate({'host': {'mem_available_pct_min': 4.85, 'disk_free_gb': 42}}, min_mem_pct=20.0,
                        allow_degraded_host=True)
check('2_override_proceeds_but_overridden', gate_o['ok'] is True and gate_o['compliance'] == g.COMPLIANCE_OVERRIDDEN)

# 3. safe baseline accepted, ENFORCED (a healthy VPS: RAM ~93% free)
gate_ok = g.startup_gate({'host': {'mem_available_pct_min': 93.0, 'disk_free_gb': 180}}, min_mem_pct=20.0)
check('3_safe_baseline_accepted', gate_ok['ok'] is True and gate_ok['compliance'] == g.COMPLIANCE_ENFORCED
      and gate_ok['reasons'] == [])

# 4. sustained memory breach fires only after 10 s continuously below floor
clock = {'t': 0.0}
breach = g.SustainedBreach(lambda: clock['t'])
th = g.build_thresholds(min_mem_pct=20.0)
floor, secs = th['mem_available_floor_pct'], th['mem_sustain_s']
# simulated available% stream, one sample every 2 s: dips below floor for 12 s then recovers
mem_stream = [(0, 55), (2, 18), (4, 17), (6, 16), (8, 15), (10, 14), (12, 13), (14, 60)]
fired_at = None
for t, avail in mem_stream:
    clock['t'] = t
    if breach('mem', avail < floor, secs) and fired_at is None:
        fired_at = t
check('4_mem_breach_fires_after_10s', fired_at == 12)   # below-floor from t=2; elapsed 10 s at t=12

# 4b. a short dip (< 10 s) never fires
clock['t'] = 0.0
breach2 = g.SustainedBreach(lambda: clock['t'])
short = [(0, 55), (2, 10), (4, 10), (6, 10), (8, 60)]   # only 6 s below floor
fired_short = any(breach2('mem', avail < floor, secs) for t, avail in short for clock_set in [clock.__setitem__('t', t)])
check('4b_short_dip_no_fire', fired_short is False)

# 5. sustained CPU breach fires, then recovery resets it
clock['t'] = 0.0
breach3 = g.SustainedBreach(lambda: clock['t'])
cpu_secs = th['host_cpu_sustain_s']
cpu_floor = th['host_cpu_pct']
cpu_stream = [(0, 90), (5, 91), (10, 92), (15, 93), (20, 94), (22, 40), (24, 95)]
fires = []
for t, cpu in cpu_stream:
    clock['t'] = t
    fires.append((t, breach3('cpu', cpu > cpu_floor, cpu_secs)))
fired_first = next((t for t, f in fires if f), None)
after_recovery = dict(fires).get(24)
check('5_cpu_breach_fires_at_20s', fired_first == 20)
check('5b_recovery_resets_breach', after_recovery is False)   # 40% at t=22 cleared the timer

# 6. no baseline auto-lowering; override uses absolute exhaustion floor
th_default = g.build_thresholds(min_mem_pct=20.0)
th_override = g.build_thresholds(min_mem_pct=20.0, allow_degraded_host=True)
check('6_default_floor_is_20', th_default['mem_available_floor_pct'] == 20.0
      and th_default['compliance_mode'] == g.COMPLIANCE_ENFORCED)
check('6b_override_floor_is_absolute', th_override['mem_available_floor_pct'] == g.ABSOLUTE_MEM_EXHAUSTION_PCT
      and th_override['compliance_mode'] == g.COMPLIANCE_OVERRIDDEN)

print()
if FAILS:
    print(f'{len(FAILS)} FAILURES: {FAILS}')
    sys.exit(1)
print('ALL GUARD CHECKS PASSED')
