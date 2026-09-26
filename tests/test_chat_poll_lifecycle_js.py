"""
Browser-side lifecycle of the web-chat room poll (room_detail / user_room).

The polling block is taken verbatim from each template and executed in Node's
``vm`` module against a fake clock, a fake ``fetch`` and a fake ``document``,
so the timer behaviour is measured on the shipped code, not a copy of it.

Proves, for both templates:
  * exactly one loop; the next poll is scheduled only after the previous one
    finished (no overlapping requests, even when the server never answers);
  * hidden tab -> zero polls; visible -> one immediate poll and one loop;
  * repeated hidden/visible toggling never multiplies timers or requests;
  * beforeunload stops the loop, and a late response cannot restart it;
  * errors and non-OK responses keep the loop alive (unchanged behaviour);
  * the poll URL carries the existing ``after_id`` cursor.

No network, database, or browser is used. Skipped when Node is not installed.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ('room_detail.html', 'user_room.html')
NODE = shutil.which('node')

pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')

START_MARK = '// ─── Polling'
END_MARK = "window.addEventListener('beforeunload', stopPolling);"


def _polling_block(template_name):
    text = (ROOT / 'app' / 'templates' / 'chat' / template_name).read_text(
        encoding='utf-8')
    start = text.index(START_MARK)
    end = text.index(END_MARK, start) + len(END_MARK)
    return text[start:end].replace('{{ poll_url }}', '/chat/rooms/7/poll')


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const flush = () => new Promise(r => setImmediate(r));

function env() {
  const st = { now: 0, seq: 0, timers: new Map(), fetches: [], maxInFlight: 0,
               doc: {}, win: {} };
  const inFlight = () => st.fetches.filter(f => !f.done).length;
  const ctx = {
    console: { log() {}, debug() {}, warn() {}, error() {} },
    document: { hidden: false,
                addEventListener(t, f) { (st.doc[t] = st.doc[t] || []).push(f); } },
    window: { addEventListener(t, f) { (st.win[t] = st.win[t] || []).push(f); } },
    setTimeout(fn, ms) { const id = ++st.seq; st.timers.set(id, { at: st.now + ms, fn }); return id; },
    clearTimeout(id) { st.timers.delete(id); },
    setInterval() { throw new Error('setInterval must not be used'); },
    clearInterval() {},
    fetch(url) {
      return new Promise((res, rej) => {
        st.fetches.push({ url, res, rej, done: false });
        st.maxInFlight = Math.max(st.maxInFlight, inFlight());
      });
    },
    appendMessageToDom() {},
    lastMsgId: 0,
  };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  const api = {
    ctx, st, inFlight,
    timers: () => st.timers.size,
    async advance(ms) {
      const target = st.now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of st.timers) if (t.at <= target && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        st.now = next[1].at; st.timers.delete(next[0]); next[1].fn(); await flush();
      }
      st.now = target; await flush();
    },
    async resolve(body = { messages: [] }, ok = true) {
      const f = st.fetches.find(x => !x.done); if (!f) throw new Error('nothing in flight');
      f.done = true; f.res({ ok, json: async () => body }); await flush(); await flush();
    },
    async reject() {
      const f = st.fetches.find(x => !x.done); f.done = true; f.rej(new Error('net')); await flush(); await flush();
    },
    async fire(target, type) { for (const f of (target === 'doc' ? st.doc : st.win)[type] || []) f(); await flush(); },
    async setHidden(h) { ctx.document.hidden = h; await api.fire('doc', 'visibilitychange'); },
  };
  return api;
}

const results = {};
async function check(name, fn) {
  try { await fn(); results[name] = 'PASS'; } catch (e) { results[name] = 'FAIL: ' + e.message; }
}
function eq(a, b, m) { if (a !== b) throw new Error(`${m}: expected ${b}, got ${a}`); }
function le(a, b, m) { if (a > b) throw new Error(`${m}: expected <= ${b}, got ${a}`); }

(async () => {
  await check('open_starts_one_loop_first_poll_after_3s', async () => {
    const e = env();
    eq(e.st.fetches.length, 0, 'no poll at load'); eq(e.timers(), 1, 'one timer');
    await e.advance(2999); eq(e.st.fetches.length, 0, 'not before 3s');
    await e.advance(1); eq(e.st.fetches.length, 1, 'poll at 3s');
  });

  await check('slow_request_never_overlaps', async () => {
    const e = env(); await e.advance(3000);
    await e.advance(60000);
    eq(e.st.fetches.length, 1, 'no second request while first is pending');
    eq(e.timers(), 0, 'no timer while in flight');
    await e.resolve(); eq(e.timers(), 1, 'rescheduled after completion');
    await e.advance(3000); eq(e.st.fetches.length, 2, 'next poll 3s after completion');
    eq(e.st.maxInFlight, 1, 'max concurrent');
  });

  await check('steady_state_single_loop', async () => {
    const e = env();
    for (let i = 0; i < 20; i++) { await e.advance(3000); await e.resolve(); le(e.timers(), 1, 'timers'); }
    eq(e.st.fetches.length, 20, 'one poll per cycle'); eq(e.st.maxInFlight, 1, 'max concurrent');
  });

  await check('hidden_tab_zero_polls_visible_resumes_one_loop', async () => {
    const e = env();
    await e.setHidden(true); eq(e.timers(), 0, 'timer cleared when hidden');
    await e.advance(120000); eq(e.st.fetches.length, 0, 'no poll while hidden');
    await e.setHidden(false); eq(e.st.fetches.length, 1, 'immediate poll on visible');
    await e.resolve(); eq(e.timers(), 1, 'exactly one loop after resume');
    await e.advance(3000); eq(e.st.fetches.length, 2, 'loop continues');
  });

  await check('hidden_while_in_flight_does_not_reschedule', async () => {
    const e = env(); await e.advance(3000);
    await e.setHidden(true); await e.resolve();
    eq(e.timers(), 0, 'late response must not restart the loop');
    await e.advance(120000); eq(e.st.fetches.length, 1, 'no poll while hidden');
  });

  await check('rapid_visibility_toggles_no_multiplication', async () => {
    const e = env(); await e.advance(3000);          // one request in flight
    for (let i = 0; i < 10; i++) {
      await e.setHidden(true); await e.setHidden(false);
      le(e.timers(), 1, 'timers'); le(e.inFlight(), 1, 'in flight');
    }
    await e.resolve(); eq(e.timers(), 1, 'one loop after toggling');
    for (let i = 0; i < 5; i++) { await e.advance(3000); if (e.inFlight()) await e.resolve(); }
    eq(e.st.maxInFlight, 1, 'max concurrent'); le(e.timers(), 1, 'timers');
    for (let i = 0; i < 10; i++) {                   // toggles with nothing in flight
      await e.setHidden(true); await e.setHidden(false);
      if (e.inFlight()) await e.resolve();
      le(e.timers(), 1, 'timers');
    }
    eq(e.st.maxInFlight, 1, 'max concurrent');
  });

  await check('beforeunload_stops_all_future_polls', async () => {
    const e = env(); await e.advance(3000);
    await e.fire('win', 'beforeunload'); await e.resolve();
    eq(e.timers(), 0, 'no timer after unload');
    await e.advance(120000); eq(e.st.fetches.length, 1, 'no poll after unload');
  });

  await check('errors_keep_loop_alive', async () => {
    const e = env(); await e.advance(3000);
    await e.reject(); eq(e.timers(), 1, 'rescheduled after network error');
    await e.advance(3000); await e.resolve({}, false);
    eq(e.timers(), 1, 'rescheduled after non-OK'); eq(e.st.maxInFlight, 1, 'max concurrent');
  });

  await check('cursor_sent_and_advanced', async () => {
    const e = env(); e.ctx.lastMsgId = 42;
    await e.advance(3000);
    eq(e.st.fetches[0].url, '/chat/rooms/7/poll?after_id=42', 'url');
    await e.resolve({ messages: [{ id: 50 }, { id: 57 }] });
    await e.advance(3000);
    eq(e.st.fetches[1].url, '/chat/rooms/7/poll?after_id=57', 'cursor advanced');
  });

  process.stdout.write(JSON.stringify(results));
})();
"""


@pytest.mark.parametrize('template_name', TEMPLATES)
def test_room_poll_lifecycle(template_name, tmp_path):
    block = tmp_path / 'poll_block.js'
    block.write_text(_polling_block(template_name), encoding='utf-8')
    harness = tmp_path / 'harness.js'
    harness.write_text(HARNESS, encoding='utf-8')
    proc = subprocess.run([NODE, str(harness), str(block)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    failures = {k: v for k, v in results.items() if v != 'PASS'}
    assert len(results) == 9, results
    assert not failures, failures


@pytest.mark.parametrize('template_name', TEMPLATES)
def test_room_poll_uses_no_interval_timer(template_name):
    """The fixed setInterval (which fired regardless of a pending request) is gone."""
    block = _polling_block(template_name)
    assert 'setInterval' not in block
    assert 'POLL_INTERVAL = 3000' in block      # interval unchanged
