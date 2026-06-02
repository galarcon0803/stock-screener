"""Local dashboard for the Stock Sentiment Tracker.

A zero-dependency (stdlib-only) web UI to view scored results and launch runs.

    python dashboard.py            # serve at http://127.0.0.1:8787
    python dashboard.py --port 9000

Features:
  - Summary cards (counts by tier, last run time, latest cost)
  - Sortable table of the latest day's scored tickers with tier badges, live
    prices, sentiment mix, and a link to the top driving post
  - "Run pipeline" button: kicks off main.py in the background (with options)
    and shows live status; the page auto-refreshes results when it finishes
  - Link to the generated HTML email report

Reads data/sentiment.db. Nothing here calls external APIs directly — runs are
delegated to main.py as a subprocess.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

# --------------------------------------------------------------------------- #
# Background run state (single run at a time)
# --------------------------------------------------------------------------- #
_run_lock = threading.Lock()
_run_state = {"running": False, "started": None, "log": [], "rc": None}


def _start_run(args_list: list[str]) -> bool:
    """Launch main.py as a subprocess, streaming output into _run_state['log']."""
    if not _run_lock.acquire(blocking=False):
        return False
    if _run_state["running"]:
        _run_lock.release()
        return False

    _run_state.update(running=True, started=time.time(), log=[], rc=None)
    _run_lock.release()

    def worker():
        cmd = [sys.executable, str(ROOT / "main.py"), *args_list]
        _run_state["log"].append(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                _run_state["log"].append(line.rstrip())
                _run_state["log"][:] = _run_state["log"][-400:]  # cap memory
            proc.wait()
            _run_state["rc"] = proc.returncode
        except Exception as exc:  # noqa: BLE001
            _run_state["log"].append(f"ERROR: {exc}")
            _run_state["rc"] = -1
        finally:
            _run_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection | None:
    if not Path(config.DB_PATH).exists():
        return None
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    # Tolerate a DB file that exists but has no schema yet (pre-first-run).
    has = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_scores'"
    ).fetchone()
    if not has:
        c.close()
        return None
    return c


def _latest_date(c) -> str | None:
    row = c.execute("SELECT MAX(score_date) d FROM ticker_scores").fetchone()
    return row["d"] if row and row["d"] else None


def _scored(c, day: str) -> list[dict]:
    rows = c.execute(
        """
        SELECT s.*, snap.price, snap.price_change_1d, snap.price_change_5d,
               snap.price_change_30d, snap.volume_ratio, snap.market_cap
        FROM ticker_scores s
        LEFT JOIN stock_snapshots snap
               ON snap.ticker = s.ticker AND snap.snapshot_date = s.score_date
        WHERE s.score_date = ?
        ORDER BY s.conviction_score DESC
        """,
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]


def _sentiment_mix(c, ticker: str, day: str) -> dict:
    rows = c.execute(
        """SELECT sentiment_label, COUNT(*) n FROM reddit_mentions
           WHERE ticker = ? GROUP BY sentiment_label""",
        (ticker,),
    ).fetchall()
    return {r["sentiment_label"] or "?": r["n"] for r in rows}


def _summary(c, day: str) -> dict:
    rows = c.execute(
        "SELECT tier, COUNT(*) n FROM ticker_scores WHERE score_date=? GROUP BY tier",
        (day,),
    ).fetchall()
    counts = {r["tier"]: r["n"] for r in rows}
    total_mentions = c.execute(
        "SELECT COUNT(*) n FROM reddit_mentions WHERE date(captured_at)=?",
        (day,),
    ).fetchone()["n"]
    return {"counts": counts, "total_mentions": total_mentions}


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
TIER_COLORS = {"ACT": "#f85149", "WATCH": "#d29922",
               "MONITOR": "#58a6ff", "NOISE": "#6e7681"}


def _chg(v) -> str:
    if v is None:
        return '<span class="muted">—</span>'
    color = "#3fb950" if v >= 0 else "#f85149"
    return f'<span style="color:{color}">{v:+.1f}%</span>'


def _big(v) -> str:
    if v is None:
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"${v/div:.1f}{unit}"
    return f"${v:,.0f}"


def render_page() -> str:
    c = _conn()
    if c is None:
        return _shell("<div class='empty'>No database yet. Click "
                      "<b>Run pipeline</b> to generate your first report.</div>")
    day = _latest_date(c)
    if not day:
        return _shell("<div class='empty'>No scored data yet. Click "
                      "<b>Run pipeline</b> to start.</div>")

    summ = _summary(c, day)
    scored = _scored(c, day)
    counts = summ["counts"]

    def _tier_card(t):
        color = TIER_COLORS.get(t, "#6e7681")
        return (f"<div class='card' style='border-top:3px solid {color}'>"
                f"<div class='num'>{counts.get(t,0)}</div>"
                f"<div class='lbl'>{t}</div></div>")

    cards = "".join(_tier_card(t) for t in ("ACT", "WATCH", "MONITOR", "NOISE"))
    cards = (
        f"<div class='card'><div class='num'>{len(scored)}</div>"
        f"<div class='lbl'>SCORED</div></div>"
        f"<div class='card'><div class='num'>{summ['total_mentions']}</div>"
        f"<div class='lbl'>MENTIONS</div></div>" + cards
    )

    rows = ""
    for s in scored:
        mix = _sentiment_mix(c, s["ticker"], day)
        bull = mix.get("bullish_dd", 0) + mix.get("bullish_general", 0)
        fomo = mix.get("bullish_fomo", 0)
        bear = mix.get("bearish", 0)
        tier = s.get("tier", "NOISE")
        badge = (f"<span class='badge' style='background:{TIER_COLORS.get(tier)}'>"
                 f"{tier}</span>")
        post = ""
        if s.get("top_post_url"):
            title = html.escape((s.get("top_post_title") or "post")[:60])
            post = f"<a href='{html.escape(s['top_post_url'])}' target='_blank'>{title}</a>"
        yahoo = f"https://finance.yahoo.com/quote/{s['ticker']}"
        rows += (
            f"<tr>"
            f"<td><a href='{yahoo}' target='_blank' class='tkr'>{s['ticker']}</a></td>"
            f"<td>{badge}</td>"
            f"<td class='r'><b>{s['conviction_score']:.0f}</b></td>"
            f"<td class='r'>{s.get('signal_score',0):.0f}</td>"
            f"<td class='r'>{s.get('opportunity_score',0):.0f}</td>"
            f"<td class='r'>{s.get('mention_count_24h',0)}</td>"
            f"<td class='senti'><span class='b'>{bull}▲</span> "
            f"<span class='f'>{fomo}🚀</span> <span class='br'>{bear}▼</span></td>"
            f"<td class='r'>{('$%.2f'%s['price']) if s.get('price') else '—'}</td>"
            f"<td class='r'>{_chg(s.get('price_change_1d'))}</td>"
            f"<td class='r'>{_chg(s.get('price_change_5d'))}</td>"
            f"<td class='r'>{_big(s.get('market_cap'))}</td>"
            f"<td>{post}</td>"
            f"</tr>"
        )

    report_link = ""
    rp = ROOT / "out" / f"report_{day}.html"
    if rp.exists():
        report_link = (f" · <a href='/report/{day}' target='_blank'>"
                       f"open email report ↗</a>")

    body = f"""
      <div class='bar'>
        <div><b>Latest scored:</b> {day}{report_link}</div>
        <div class='controls'>
          <label>Subreddits:
            <select id='preset'>
              <option value='quick'>Quick (4 subs)</option>
              <option value='full'>Full (14 subs)</option>
            </select>
          </label>
          <label><input type='checkbox' id='nosent'> skip sentiment (free)</label>
          <button id='runbtn' onclick='startRun()'>▶ Run pipeline</button>
        </div>
      </div>
      <div id='status' class='status'></div>
      <div class='cards'>{cards}</div>
      <table>
        <thead><tr>
          <th>Ticker</th><th>Tier</th><th class='r'>Conv</th><th class='r'>Sig</th>
          <th class='r'>Opp</th><th class='r'>Ment</th><th>Sentiment</th>
          <th class='r'>Price</th><th class='r'>1d</th><th class='r'>5d</th>
          <th class='r'>Mkt Cap</th><th>Top post</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    """
    c.close()
    return _shell(body)


def _shell(body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Sentiment Dashboard</title>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<style>
  body{{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px}}
  header{{padding:16px 24px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:12px}}
  header h1{{font-size:18px;margin:0}}
  .wrap{{padding:20px 24px;max-width:1300px;margin:0 auto}}
  .bar{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:16px}}
  .controls{{display:flex;align-items:center;gap:14px}}
  select,button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 10px;font-size:13px}}
  button{{background:#238636;border:none;cursor:pointer;font-weight:600}}
  button:disabled{{background:#30363d;cursor:not-allowed}}
  .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 18px;min-width:90px;text-align:center}}
  .card .num{{font-size:24px;font-weight:700}}
  .card .lbl{{font-size:11px;color:#8b949e;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden}}
  th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #21262d;white-space:nowrap}}
  th{{background:#1c2128;color:#8b949e;font-size:12px;cursor:pointer}}
  td.r,th.r{{text-align:right}}
  .tkr{{color:#e6edf3;font-weight:700;text-decoration:none}}
  .tkr:hover{{color:#58a6ff}}
  .badge{{color:#0d1117;font-weight:700;font-size:11px;padding:2px 7px;border-radius:9px}}
  a{{color:#58a6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
  .muted{{color:#6e7681}}
  .senti .b{{color:#3fb950}} .senti .f{{color:#d29922}} .senti .br{{color:#f85149}}
  .status{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-family:monospace;font-size:12px;white-space:pre-wrap;max-height:220px;overflow:auto;display:none}}
  .status.show{{display:block}}
  .empty{{padding:60px;text-align:center;color:#8b949e}}
</style></head><body>
<header><h1>📈 Stock Sentiment Dashboard</h1>
  <span class='muted'>local · reads sentiment.db</span></header>
<div class='wrap'>{body}</div>
<script>
function startRun(){{
  const preset=document.getElementById('preset').value;
  const nosent=document.getElementById('nosent').checked;
  document.getElementById('runbtn').disabled=true;
  fetch('/run',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{preset:preset,nosent:nosent}})}})
    .then(r=>r.json()).then(d=>{{ if(d.started) poll(); else alert(d.msg||'busy'); }});
}}
function poll(){{
  const s=document.getElementById('status'); s.classList.add('show');
  fetch('/status').then(r=>r.json()).then(d=>{{
    s.textContent=d.log.join('\\n'); s.scrollTop=s.scrollHeight;
    if(d.running){{ setTimeout(poll,1500); }}
    else{{ s.textContent+='\\n\\n[done rc='+d.rc+'] reloading…';
           setTimeout(()=>location.reload(),1800); }}
  }});
}}
// resume polling if a run is already going when the page loads
fetch('/status').then(r=>r.json()).then(d=>{{ if(d.running){{ document.getElementById('runbtn').disabled=true; poll(); }} }});
// simple client-side column sort
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>sortBy(i));
function sortBy(i){{
  const tb=document.querySelector('tbody'); if(!tb)return;
  const rows=[...tb.rows];
  const num=v=>{{const n=parseFloat(v.replace(/[^0-9.\\-]/g,''));return isNaN(n)?v.toLowerCase():n;}};
  const asc=tb.dataset.s==i+'a'; tb.dataset.s=i+(asc?'d':'a');
  rows.sort((x,y)=>{{const a=num(x.cells[i].innerText),b=num(y.cells[i].innerText);
    return (a<b?-1:a>b?1:0)*(asc?-1:1);}});
  rows.forEach(r=>tb.appendChild(r));
}}
</script></body></html>"""


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, render_page())
        elif path == "/status":
            self._send(200, json.dumps({
                "running": _run_state["running"],
                "rc": _run_state["rc"],
                "log": _run_state["log"][-200:],
            }), "application/json")
        elif path.startswith("/report/"):
            day = path.rsplit("/", 1)[-1]
            rp = ROOT / "out" / f"report_{day}.html"
            if rp.exists():
                self._send(200, rp.read_text(encoding="utf-8"))
            else:
                self._send(404, "report not found")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self._send(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or "{}")
        args = ["--no-email", "--no-outcomes"]
        if payload.get("nosent"):
            args.append("--no-sentiment")
        # Subreddit preset is applied via an env flag main.py understands.
        preset = payload.get("preset", "quick")
        env_args = ["--preset", preset]
        started = _start_run(args + env_args)
        self._send(200, json.dumps(
            {"started": started, "msg": "" if started else "a run is already in progress"}
        ), "application/json")


def main():
    p = argparse.ArgumentParser(description="Sentiment dashboard")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}"
    print(f"Dashboard at {url}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
