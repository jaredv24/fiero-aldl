"""Session report: one self-contained HTML page to keep, print or send.

Everything is inlined (styles and SVG charts), so the file opens anywhere and
can be emailed to a mechanic or posted to a forum.
"""

import html
import os
from datetime import datetime

SEV_COLORS = {"problem": "#c0392b", "watch": "#b9770e", "ok": "#1e8449", "info": "#5d6d7e",
              "fail": "#c0392b", "pass": "#1e8449", "yes": "#c0392b", "maybe": "#b9770e",
              "no": "#5d6d7e", "better": "#1e8449", "worse": "#c0392b"}

CSS = """
:root { --bg:#ffffff; --ink:#1d2329; --muted:#5d6d7e; --line:#d5dbe1; --card:#f6f8fa; --plot:#2471a3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16191d; --ink:#e6e9ec; --muted:#9aa5b1; --line:#333a42; --card:#1f2429; --plot:#5dade2; }
}
body { background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, Helvetica, Arial, sans-serif;
       max-width:980px; margin:0 auto; padding:24px 16px 60px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:19px; margin:30px 0 8px;
     border-bottom:1px solid var(--line); padding-bottom:4px; }
.muted { color:var(--muted); } .card { background:var(--card); border-radius:8px; padding:10px 14px; margin:8px 0; }
.tag { display:inline-block; font-weight:700; font-size:12px; color:#fff; border-radius:4px;
       padding:1px 7px; margin-right:8px; text-transform:uppercase; }
table { border-collapse:collapse; width:100%; } td, th { text-align:left; padding:5px 8px;
       border-bottom:1px solid var(--line); vertical-align:top; } th { font-size:13px; color:var(--muted); }
svg { width:100%; height:auto; display:block; } .axis { fill:var(--muted); font-size:11px; }
.plot { fill:none; stroke:var(--plot); stroke-width:1.6; } .grid { stroke:var(--line); stroke-width:1; }
pre { white-space:pre-wrap; font:13px/1.45 Menlo, monospace; }
.answer { white-space:pre-wrap; }
@media print { body { max-width:none; } }
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def tag(label, key=None):
    color = SEV_COLORS.get((key or label or "").lower(), "#5d6d7e")
    return f'<span class="tag" style="background:{color}">{esc(label)}</span>'


def svg_chart(points, title, unit="", width=940, height=170):
    """Line chart of [(t, value)] as inline SVG (t in seconds)."""
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 2:
        return ""
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad_l, pad_r, pad_t, pad_b = 52, 10, 22, 22
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    step = max(1, len(pts) // 600)
    coords = " ".join(f"{pad_l + (t - t0) / ((t1 - t0) or 1) * w:.1f},"
                      f"{pad_t + (1 - (v - lo) / (hi - lo)) * h:.1f}" for t, v in pts[::step])
    fmt = (lambda x: f"{x:.0f}") if hi - lo >= 20 else (lambda x: f"{x:.2f}")
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">'
            f'<text class="axis" x="{pad_l}" y="14" style="font-weight:700">{esc(title)}'
            f'{" (" + esc(unit) + ")" if unit else ""}</text>'
            f'<line class="grid" x1="{pad_l}" y1="{pad_t}" x2="{pad_l + w}" y2="{pad_t}"/>'
            f'<line class="grid" x1="{pad_l}" y1="{pad_t + h}" x2="{pad_l + w}" y2="{pad_t + h}"/>'
            f'<text class="axis" x="{pad_l - 6}" y="{pad_t + 4}" text-anchor="end">{fmt(hi)}</text>'
            f'<text class="axis" x="{pad_l - 6}" y="{pad_t + h + 4}" text-anchor="end">{fmt(lo)}</text>'
            f'<text class="axis" x="{pad_l}" y="{height - 4}">0 s</text>'
            f'<text class="axis" x="{pad_l + w}" y="{height - 4}" text-anchor="end">{t1 - t0:.0f} s</text>'
            f'<polyline class="plot" points="{coords}"/></svg>')


def build(data):
    """HTML for a report. `data` keys (all optional except vehicle):
    vehicle, source, when, findings [(severity, title, detail, hint)], charts [(title, unit, points)],
    codes (text), tests [(name, status, headline, details)], leak [(spot, reacted, details)],
    leak_summary, compare (verdict, rows [(name, before, after, unit, verdict)], notes),
    history [(when, kind, name, status, summary)], claude [(question, answer)], notes.
    """
    when = data.get("when") or datetime.now()
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>{esc(data['vehicle'])} report</title><style>{CSS}</style></head><body>",
           f"<h1>{esc(data['vehicle'])}</h1>",
           f"<div class='muted'>Diagnostic session report · {when:%B %d, %Y %H:%M}"
           f"{' · ' + esc(data['source']) if data.get('source') else ''}</div>",
           "<p class='muted'>Made with Fiero ALDL. Findings point to where to look; they aren't a "
           "diagnosis. Normal values come from the vehicle profile or are labeled as general rules.</p>"]

    if data.get("findings"):
        out.append("<h2>What the data shows</h2>")
        for sev, title, detail, hint in data["findings"]:
            out.append(f"<div class='card'>{tag(sev)}<b>{esc(title)}</b>"
                       + (f"<div>{esc(detail)}</div>" if detail else "")
                       + (f"<div class='muted'><i>{esc(hint)}</i></div>" if hint else "") + "</div>")
    if data.get("charts"):
        out.append("<h2>Charts</h2>")
        for title, unit, pts in data["charts"]:
            out.append(svg_chart(pts, title, unit))
    if data.get("codes"):
        out.append(f"<h2>Trouble codes</h2><pre>{esc(data['codes'].strip())}</pre>")
    if data.get("tests"):
        out.append("<h2>Guided tests this session</h2>")
        for name, status, headline, details in data["tests"]:
            out.append(f"<div class='card'>{tag(status)}<b>{esc(name)}</b> — {esc(headline)}"
                       + "".join(f"<div class='muted'>{esc(d)}</div>" for d in details or []) + "</div>")
    if data.get("leak"):
        out.append("<h2>Leak hunt (spray test)</h2><table><tr><th>Spot</th><th>Reacted?</th>"
                   "<th>What changed</th></tr>")
        for spot, reacted, details in data["leak"]:
            out.append(f"<tr><td>{esc(spot)}</td><td>{tag(reacted)}</td><td>{esc(details)}</td></tr>")
        out.append(f"</table><p><b>{esc(data.get('leak_summary', ''))}</b></p>")
    if data.get("compare"):
        verdict, rows, notes = data["compare"]
        out.append(f"<h2>Before / after</h2><p><b>{esc(verdict)}</b></p><table><tr><th>Reading</th>"
                   "<th>Before</th><th>After</th><th></th></tr>")
        for name, b, a, unit, v in rows:
            out.append(f"<tr><td>{esc(name)}</td><td>{esc(b)} {esc(unit)}</td><td>{esc(a)} {esc(unit)}</td>"
                       f"<td>{tag(v) if v else ''}</td></tr>")
        out.append("</table>" + "".join(f"<p class='muted'>{esc(n)}</p>" for n in notes))
    if data.get("history"):
        out.append("<h2>Vehicle history (tests and hands-on checks)</h2><table><tr><th>When</th>"
                   "<th>Test / check</th><th>Result</th><th>Summary</th></tr>")
        for when_, kind, name, status, summary in data["history"]:
            out.append(f"<tr><td>{esc(when_)}</td><td>{esc(name)} <span class='muted'>({esc(kind)})"
                       f"</span></td><td>{tag(status)}</td><td>{esc(summary)}</td></tr>")
        out.append("</table>")
        if data.get("trends"):
            out.append("".join(f"<p>{esc(t)}</p>" for t in data["trends"]))
    if data.get("claude"):
        out.append("<h2>Claude's analysis</h2>")
        for q, a in data["claude"]:
            out.append(f"<div class='card'><div class='muted'><b>Q:</b> {esc(q)}</div>"
                       f"<div class='answer'>{esc(a)}</div></div>")
    if data.get("notes"):
        out.append(f"<h2>Notes</h2><p>{esc(data['notes'])}</p>")
    out.append("</body></html>")
    return "\n".join(out)


def save(folder, data):
    os.makedirs(folder, exist_ok=True)
    stamp = (data.get("when") or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = "".join(c if c.isalnum() else "-" for c in data["vehicle"]).strip("-")[:40]
    path = os.path.join(folder, f"report-{name}-{stamp}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(data))
    return path
