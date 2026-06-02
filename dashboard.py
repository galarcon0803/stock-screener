"""Local analytics dashboard for the Stock Sentiment Tracker.

Zero-dependency (stdlib http.server). Reads ALL accumulated history from
data/sentiment.db — the database persists across runs, so every run adds to the
picture rather than replacing it.

    python dashboard.py                 # http://127.0.0.1:8787

What it shows (built to give an end user real insight, not a flat snapshot):
  - Coverage stats + the model's realized TRACK RECORD (win rate / avg return)
  - Top conviction movers vs the previous run (momentum)
  - A date picker to view any historical day
  - Main table with momentum arrows, NEW badges, days-tracked, sentiment mix
  - Click any ticker for a detail view: conviction timeline (sparkline), price
    history, top driving posts, and realized 5d/30d outcomes
  - "Run pipeline" button (background, live log) + link to the email report
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config          # noqa: E402
import database        # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

# --------------------------------------------------------------------------- #
# Background run state
# --------------------------------------------------------------------------- #
_run_lock = threading.Lock()
_run = {"running": False, "log": [], "rc": None}


def _start_run(extra: list[str]) -> bool:
    with _run_lock:
        if _run["running"]:
            return False
        _run.update(running=True, log=[], rc=None)

    def worker():
        cmd = [sys.executable, str(ROOT / "main.py"),
               "--no-email", "--no-outcomes", *extra]
        _run["log"].append("$ " + " ".join(cmd[-4:]))
        try:
            p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:            # type: ignore[union-attr]
                _run["log"].append(line.rstrip())
                _run["log"][:] = _run["log"][-500:]
            p.wait()
            _run["rc"] = p.returncode
        except Exception as exc:             # noqa: BLE001
            _run["log"].append(f"ERROR: {exc}")
            _run["rc"] = -1
        finally:
            _run["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# DB access (never creates/deletes — read-only open)
# --------------------------------------------------------------------------- #
def _conn():
    if not Path(config.DB_PATH).exists():
        return None
    import sqlite3
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='ticker_scores'").fetchone():
        c.close()
        return None
    return c


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
TIER_COLORS = {"ACT": "#f85149", "WATCH": "#d29922",
               "MONITOR": "#58a6ff", "NOISE": "#6e7681"}


def _chg(v):
    if v is None:
        return "<span class=muted>—</span>"
    c = "#3fb950" if v >= 0 else "#f85149"
    return f"<span style='color:{c}'>{v:+.1f}%</span>"


def _delta(v):
    if v is None:
        return "<span class='badge new'>NEW</span>"
    if v > 0.5:
        return f"<span style='color:#3fb950'>▲ {v:+.0f}</span>"
    if v < -0.5:
        return f"<span style='color:#f85149'>▼ {v:.0f}</span>"
    return "<span class=muted>– 0</span>"


def _big(v):
    if v is None:
        return "—"
    for u, d in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= d:
            return f"${v/d:.1f}{u}"
    return f"${v:,.0f}"


def _sparkline(values: list[float], w=120, h=28) -> str:
    """Tiny inline SVG sparkline of conviction over time."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = w / (len(vals) - 1)
    pts = " ".join(
        f"{i*step:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}"
        for i, v in enumerate(vals)
    )
    last_up = vals[-1] >= vals[0]
    color = "#3fb950" if last_up else "#f85149"
    return (f"<svg width={w} height={h} style='vertical-align:middle'>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' "
            f"stroke-width='1.5'/></svg>")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def render_main(day: str | None = None) -> str:
    c = _conn()
    if c is None:
        return _shell(
            "<div class=empty><h2>No data yet</h2>"
            "<p>Click <b>▶ Run pipeline</b> above to collect and score your "
            "first day. Data persists in <code>data/sentiment.db</code> and "
            "<b>accumulates</b> — every run adds history.</p></div>",
            running_controls=True)

    dates = database.get_all_score_dates(c)
    day = day if day in dates else (dates[0] if dates else None)
    stats = database.get_db_stats(c)
    perf = database.get_model_performance(c)
    movers = database.get_trending_tickers(c)
    rows_data = database.get_latest_scores_with_trend(c, day) if day else []
    c.close()

    # --- header: coverage + model track record ---
    track = _track_record_html(perf)
    coverage = (
        f"<div class=statline>"
        f"<b>{stats['days_of_data']}</b> days · "
        f"<b>{stats['distinct_tickers']}</b> tickers tracked · "
        f"<b>{stats['total_mentions']:,}</b> mentions · "
        f"range {stats['first_date'] or '—'} → {stats['last_date'] or '—'}"
        f"</div>"
    )

    # --- movers strip ---
    movers_html = ""
    if movers:
        chips = "".join(
            f"<span class=mover><a href='/ticker?t={m['ticker']}'>{m['ticker']}</a> "
            f"{_delta(m['delta'])}</span>" for m in movers)
        movers_html = f"<div class=movers><span class=lbl>📈 Biggest movers vs last run:</span> {chips}</div>"

    # --- date picker ---
    opts = "".join(
        f"<option value='{d}'{' selected' if d == day else ''}>{d}</option>"
        for d in dates)
    picker = (f"<label>Day: <select onchange='location=\"/?day=\"+this.value'>"
              f"{opts}</select></label>") if dates else ""

    # --- main table ---
    body_rows = ""
    for s in rows_data:
        tier = s.get("tier", "NOISE")
        badge = f"<span class=badge style='background:{TIER_COLORS.get(tier)}'>{tier}</span>"
        mix = s.get("sentiment_mix", {})
        bull = mix.get("bullish_dd", 0) + mix.get("bullish_general", 0)
        fomo = mix.get("bullish_fomo", 0)
        bear = mix.get("bearish", 0)
        body_rows += (
            f"<tr onclick=\"location='/ticker?t={s['ticker']}'\">"
            f"<td><b class=tkr>{s['ticker']}</b>"
            f"{' <span class=newdot>●</span>' if s.get('is_new') else ''}</td>"
            f"<td>{badge}</td>"
            f"<td class=r><b>{s['conviction_score']:.0f}</b></td>"
            f"<td class=r>{_delta(s.get('conviction_delta'))}</td>"
            f"<td class=r>{s.get('days_tracked',1)}d</td>"
            f"<td class=r>{s.get('mention_count_24h',0)}</td>"
            f"<td class=senti><span class=b>{bull}▲</span> "
            f"<span class=f>{fomo}🚀</span> <span class=br>{bear}▼</span></td>"
            f"<td class=r>{('$%.2f'%s['price']) if s.get('price') else '—'}</td>"
            f"<td class=r>{_chg(s.get('price_change_1d'))}</td>"
            f"<td class=r>{_chg(s.get('price_change_5d'))}</td>"
            f"<td class=r>{_big(s.get('market_cap'))}</td>"
            f"</tr>")

    report_link = ""
    if day and (ROOT / "out" / f"report_{day}.html").exists():
        report_link = f" · <a href='/report/{day}' target=_blank>email report ↗</a>"

    body = f"""
      {coverage}
      {track}
      {movers_html}
      <div class=bar>
        <div>{picker}{report_link}</div>
        <div class=controls>
          <label><select id=preset>
            <option value=quick>Quick (4 subs)</option>
            <option value=full>Full (14 subs)</option></select></label>
          <label><input type=checkbox id=nosent> skip sentiment (free)</label>
          <button id=runbtn onclick=startRun()>▶ Run pipeline</button>
        </div>
      </div>
      <div id=status class=status></div>
      <table>
        <thead><tr>
          <th>Ticker</th><th>Tier</th><th class=r>Conv</th>
          <th class=r title='change vs previous run'>Δ</th>
          <th class=r title='days appeared'>Age</th>
          <th class=r>Ment</th><th>Sentiment (lifetime)</th>
          <th class=r>Price</th><th class=r>1d</th><th class=r>5d</th><th class=r>Mkt Cap</th>
        </tr></thead>
        <tbody>{body_rows or '<tr><td colspan=11 class=muted>No tickers scored this day.</td></tr>'}</tbody>
      </table>
      <p class=hint>Rows are clickable → per-ticker history. Δ = conviction change vs the previous run · ● = first time seen.</p>
    """
    return _shell(body, running_controls=True)


def _track_record_html(perf: dict) -> str:
    n = perf["n_signals"]
    if not n:
        return ("<div class=track><span class=lbl>🎯 Model track record:</span> "
                f"<span class=muted>no realized outcomes yet "
                f"({perf['n_pending']} signals awaiting 5d/30d returns — "
                f"come back in a few days)</span></div>")
    wr = perf["win_rate_5d"]
    wr_color = "#3fb950" if (wr or 0) >= 50 else "#f85149"
    r5 = perf["avg_return_5d"]
    r5_color = "#3fb950" if (r5 or 0) >= 0 else "#f85149"
    return (
        f"<div class=track><span class=lbl>🎯 Model track record:</span> "
        f"<b style='color:{wr_color}'>{wr}% win rate</b> (5d) · "
        f"avg 5d return <b style='color:{r5_color}'>{r5:+.1f}%</b> · "
        f"avg 30d <b>{(perf['avg_return_30d'] or 0):+.1f}%</b> · "
        f"{n} realized / {perf['n_pending']} pending</div>"
    )


def render_ticker(ticker: str) -> str:
    c = _conn()
    if c is None:
        return _shell("<div class=empty>No data.</div>")
    h = database.get_ticker_history(c, ticker)
    c.close()
    if not h["scores"]:
        return _shell(f"<div class=empty>No history for {html.escape(ticker)}.</div>")

    convs = [s["conviction_score"] for s in h["scores"]]
    spark = _sparkline(convs, w=300, h=60)
    latest = h["scores"][-1]

    timeline = "".join(
        f"<tr><td>{s['score_date']}</td>"
        f"<td><span class=badge style='background:{TIER_COLORS.get(s['tier'])}'>{s['tier']}</span></td>"
        f"<td class=r><b>{s['conviction_score']:.0f}</b></td>"
        f"<td class=r>{s['signal_score']:.0f}</td>"
        f"<td class=r>{s['opportunity_score']:.0f}</td>"
        f"<td class=r>{s['mention_count_24h']}</td></tr>"
        for s in reversed(h["scores"]))

    posts = "".join(
        f"<li><a href='{html.escape(m['post_url'] or '#')}' target=_blank>"
        f"{html.escape((m['post_title'] or 'post')[:80])}</a> "
        f"<span class=muted>r/{m['subreddit']} ▲{m['post_score']} · "
        f"{m['sentiment_label'] or '?'}</span>"
        f"{(' — '+html.escape(m['sentiment_summary'])) if m.get('sentiment_summary') else ''}</li>"
        for m in h["top_mentions"])

    outcomes = ""
    if h["outcomes"]:
        orows = "".join(
            f"<tr><td>{o['signal_date']}</td><td class=r>{o.get('conviction_score_at_signal',0):.0f}</td>"
            f"<td class=r>{('$%.2f'%o['price_at_signal']) if o.get('price_at_signal') else '—'}</td>"
            f"<td class=r>{_chg(o.get('return_5d'))}</td>"
            f"<td class=r>{_chg(o.get('return_30d'))}</td></tr>"
            for o in h["outcomes"])
        outcomes = (f"<h3>Realized outcomes</h3><table><thead><tr><th>Signal date</th>"
                    f"<th class=r>Conv</th><th class=r>Price@signal</th>"
                    f"<th class=r>5d</th><th class=r>30d</th></tr></thead>"
                    f"<tbody>{orows}</tbody></table>")

    body = f"""
      <a href='/' class=back>← all tickers</a>
      <div class=detailhead>
        <h2><a href='https://finance.yahoo.com/quote/{ticker}' target=_blank>{ticker}</a></h2>
        <div>{spark}</div>
        <div class=detailmeta>
          Latest conviction <b>{latest['conviction_score']:.0f}</b>
          (<span class=badge style='background:{TIER_COLORS.get(latest['tier'])}'>{latest['tier']}</span>)
          · tracked {len(h['scores'])} day(s)
        </div>
      </div>
      <h3>Conviction timeline</h3>
      <table><thead><tr><th>Date</th><th>Tier</th><th class=r>Conv</th>
        <th class=r>Sig</th><th class=r>Opp</th><th class=r>Ment</th></tr></thead>
        <tbody>{timeline}</tbody></table>
      {outcomes}
      <h3>Top driving posts</h3>
      <ul class=posts>{posts or '<li class=muted>none</li>'}</ul>
    """
    return _shell(body)


def _shell(body: str, running_controls: bool = False) -> str:
    js = _JS if running_controls else ""
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Sentiment Dashboard</title><meta name=viewport content='width=device-width,initial-scale=1'>
<style>
 body{{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px}}
 header{{padding:14px 24px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:10px}}
 header h1{{font-size:17px;margin:0}}
 .wrap{{padding:18px 24px;max-width:1320px;margin:0 auto}}
 .statline{{color:#8b949e;margin-bottom:10px;font-size:13px}}
 .track{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:10px}}
 .track .lbl,.movers .lbl{{color:#8b949e;margin-right:6px}}
 .movers{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:14px}}
 .mover{{display:inline-block;margin-right:14px}} .mover a{{color:#e6edf3;font-weight:700;text-decoration:none}}
 .bar{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:14px}}
 .controls{{display:flex;align-items:center;gap:12px}}
 select,button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 10px;font-size:13px}}
 button{{background:#238636;border:none;cursor:pointer;font-weight:600}} button:disabled{{background:#30363d}}
 table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden;margin-bottom:10px}}
 th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #21262d;white-space:nowrap}}
 th{{background:#1c2128;color:#8b949e;font-size:12px;cursor:pointer}}
 td.r,th.r{{text-align:right}}
 tbody tr{{cursor:pointer}} tbody tr:hover{{background:#1c2128}}
 .tkr{{color:#e6edf3}} .badge{{color:#0d1117;font-weight:700;font-size:11px;padding:2px 7px;border-radius:9px}}
 .badge.new{{background:#8957e5;color:#fff}} .newdot{{color:#8957e5}}
 a{{color:#58a6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
 .muted{{color:#6e7681}}
 .senti .b{{color:#3fb950}} .senti .f{{color:#d29922}} .senti .br{{color:#f85149}}
 .status{{background:#0b0e13;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-family:monospace;font-size:12px;white-space:pre-wrap;max-height:240px;overflow:auto;display:none}}
 .status.show{{display:block}}
 .empty{{padding:50px;text-align:center;color:#8b949e}}
 .hint{{color:#6e7681;font-size:12px}}
 .back{{display:inline-block;margin-bottom:10px}}
 .detailhead{{display:flex;align-items:center;gap:24px;flex-wrap:wrap;margin-bottom:10px}}
 .detailhead h2{{margin:0}} .detailmeta{{color:#8b949e}}
 .posts{{line-height:1.7}} h3{{margin-top:22px;font-size:15px}}
</style></head><body>
<header><h1>📈 Stock Sentiment Dashboard</h1><span class=muted>local · full history</span></header>
<div class=wrap>{body}</div>
{js}</body></html>"""


_JS = """<script>
function startRun(){
  const preset=document.getElementById('preset').value;
  const nosent=document.getElementById('nosent').checked;
  document.getElementById('runbtn').disabled=true;
  fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset,nosent})}).then(r=>r.json())
    .then(d=>{ if(d.started) poll(); else {alert(d.msg||'busy');document.getElementById('runbtn').disabled=false;} });
}
function poll(){
  const s=document.getElementById('status'); s.classList.add('show');
  fetch('/status').then(r=>r.json()).then(d=>{
    s.textContent=d.log.join('\\n'); s.scrollTop=s.scrollHeight;
    if(d.running) setTimeout(poll,1500);
    else { s.textContent+='\\n\\n[done rc='+d.rc+'] reloading…'; setTimeout(()=>location.href='/',1800); }
  });
}
fetch('/status').then(r=>r.json()).then(d=>{ if(d.running){document.getElementById('runbtn').disabled=true;poll();} });
document.querySelectorAll('thead th').forEach((th,i)=>th.addEventListener('click',e=>{e.stopPropagation();sortBy(th,i);}));
function sortBy(th,i){
  const tb=th.closest('table').querySelector('tbody'); if(!tb)return;
  const rows=[...tb.rows].filter(r=>r.cells.length>3);
  const num=v=>{const n=parseFloat(v.replace(/[^0-9.\\-]/g,''));return isNaN(n)?v.toLowerCase():n;};
  const asc=tb.dataset.s==i+'a'; tb.dataset.s=i+(asc?'d':'a');
  rows.sort((x,y)=>{const a=num(x.cells[i].innerText),b=num(y.cells[i].innerText);return(a<b?-1:a>b?1:0)*(asc?-1:1);});
  rows.forEach(r=>tb.appendChild(r));
}
</script>"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, render_main(q.get("day", [None])[0]))
        elif u.path == "/ticker":
            t = (q.get("t", [""])[0] or "").upper()
            self._send(200, render_ticker(t))
        elif u.path == "/status":
            self._send(200, json.dumps(
                {"running": _run["running"], "rc": _run["rc"],
                 "log": _run["log"][-250:]}), "application/json")
        elif u.path.startswith("/report/"):
            rp = ROOT / "out" / f"report_{u.path.rsplit('/',1)[-1]}.html"
            self._send(200, rp.read_text(encoding="utf-8")) if rp.exists() \
                else self._send(404, "no report")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self._send(404, "not found")
            return
        n = int(self.headers.get("Content-Length", 0))
        p = json.loads(self.rfile.read(n) or "{}")
        extra = ["--preset", p.get("preset", "quick")]
        if p.get("nosent"):
            extra.append("--no-sentiment")
        ok = _start_run(extra)
        self._send(200, json.dumps(
            {"started": ok, "msg": "" if ok else "a run is already in progress"}),
            "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"Dashboard at http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
