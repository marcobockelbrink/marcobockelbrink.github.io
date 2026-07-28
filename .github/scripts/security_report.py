#!/usr/bin/env python3
"""Erzeugt aus den GitHub-Code-Scanning-Alerts einen Sicherheitsreport
(Markdown + selbst-enthaltenes HTML). Quelle ist die REST-API; im Workflow
liefert der GITHUB_TOKEN die Rechte. Ohne Fremd-Bibliotheken (nur Stdlib).

    python3 security_report.py <owner/repo> <out-basis-ohne-endung>
Benötigt die Umgebungsvariable GH_TOKEN (oder GITHUB_TOKEN)."""
from contextlib import suppress
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_REPOSITORY", "")
OUT = sys.argv[2] if len(sys.argv) > 2 else "security-report"


def gh_api(path):
    """Alle Seiten eines API-Endpunkts holen (via gh CLI, paginiert)."""
    out = subprocess.run(["gh", "api", "--paginate", path],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return []
    # --paginate hängt JSON-Arrays teils aneinander -> robust zusammenführen
    txt = out.stdout.strip()
    items = []
    for chunk in txt.replace("][", "]\n[").splitlines():
        chunk = chunk.strip()
        if chunk:
            with suppress(json.JSONDecodeError):
                items.extend(json.loads(chunk))
    return items


SEV_ORDER = {"critical": 0, "high": 1, "error": 1, "medium": 2,
             "warning": 3, "low": 4, "note": 5}
SEV_LABEL = {"critical": "Kritisch", "high": "Hoch", "error": "Hoch",
             "medium": "Mittel", "warning": "Warnung", "low": "Niedrig",
             "note": "Hinweis"}


def sev(a):
    return (a.get("rule", {}).get("security_severity_level")
            or a.get("rule", {}).get("severity") or "note").lower()


def main():
    alerts = [a for a in gh_api(f"repos/{REPO}/code-scanning/alerts?state=open")]
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    # Zusammenfassung je Tool
    tools = {}
    for a in alerts:
        t = a.get("tool", {}).get("name", "?")
        tools.setdefault(t, {}).setdefault(SEV_LABEL.get(sev(a), sev(a)), 0)
        tools[t][SEV_LABEL.get(sev(a), sev(a))] += 1

    sev_total = {}
    for a in alerts:
        lbl = SEV_LABEL.get(sev(a), sev(a))
        sev_total[lbl] = sev_total.get(lbl, 0) + 1

    # ---- Markdown ----
    md = [f"# Sicherheitsreport – {REPO}", "",
          f"*Stand: {now} · offene Code-Scanning-Funde: **{len(alerts)}***", ""]
    if not alerts:
        md.append("✅ **Keine offenen Funde.**")
    else:
        md += ["## Überblick nach Schweregrad", "",
               "| Schweregrad | Anzahl |", "|---|---:|"]
        for lbl in ("Kritisch", "Hoch", "Mittel", "Warnung", "Niedrig", "Hinweis"):
            if sev_total.get(lbl):
                md.append(f"| {lbl} | {sev_total[lbl]} |")
        md += ["", "## Je Scanner", "", "| Scanner | Funde (nach Schweregrad) |",
               "|---|---|"]
        for t in sorted(tools):
            parts = ", ".join(f"{n}× {l}" for l, n in sorted(
                tools[t].items(), key=lambda x: SEV_ORDER.get(x[0].lower(), 9)))
            md.append(f"| {t} | {parts} |")
        md += ["", "## Details (nach Schweregrad sortiert)", ""]
        for a in sorted(alerts, key=lambda x: SEV_ORDER.get(sev(x), 9)):
            loc = a.get("most_recent_instance", {}).get("location", {})
            where = f'{loc.get("path","?")}:{loc.get("start_line","?")}'
            rule = a.get("rule", {})
            md.append(f"- **[{SEV_LABEL.get(sev(a), sev(a))}]** "
                      f"`{a.get('tool',{}).get('name','?')}` · "
                      f"{html.unescape(rule.get('description') or rule.get('name') or rule.get('id',''))} "
                      f"· `{where}` · [Alert #{a.get('number')}]({a.get('html_url','')})")
    md_txt = "\n".join(md) + "\n"
    with open(OUT + ".md", "w", encoding="utf-8") as f:
        f.write(md_txt)

    # ---- HTML (druckbar -> PDF) ----
    def esc(x):
        return html.escape(str(x))
    rows = ""
    for a in sorted(alerts, key=lambda x: SEV_ORDER.get(sev(x), 9)):
        loc = a.get("most_recent_instance", {}).get("location", {})
        rule = a.get("rule", {})
        s = sev(a)
        color = {"critical": "#b91c1c", "high": "#dc2626", "error": "#dc2626",
                 "medium": "#d97706", "warning": "#ca8a04", "low": "#2563eb",
                 "note": "#6b7280"}.get(s, "#6b7280")
        rows += (f'<tr><td><span style="background:{color};color:#fff;padding:1px 7px;'
                 f'border-radius:6px;font-size:11px">{esc(SEV_LABEL.get(s, s))}</span></td>'
                 f'<td>{esc(a.get("tool",{}).get("name","?"))}</td>'
                 f'<td>{esc(html.unescape(rule.get("description") or rule.get("name") or rule.get("id","")))}</td>'
                 f'<td style="font-family:monospace;font-size:12px">{esc(loc.get("path","?"))}:{esc(loc.get("start_line","?"))}</td>'
                 f'<td><a href="{esc(a.get("html_url",""))}">#{esc(a.get("number"))}</a></td></tr>')
    sev_rows = "".join(f"<tr><td>{esc(l)}</td><td style='text-align:right'>{sev_total[l]}</td></tr>"
                       for l in ("Kritisch", "Hoch", "Mittel", "Warnung", "Niedrig", "Hinweis")
                       if sev_total.get(l))
    doc = f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>Sicherheitsreport {esc(REPO)}</title>
<style>
 body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:#1e293b;max-width:900px;margin:24px auto;padding:0 16px}}
 h1{{font-size:22px;margin:0 0 2px}} .sub{{color:#64748b;margin-bottom:20px}}
 table{{border-collapse:collapse;width:100%;margin:10px 0 24px;font-size:13px}}
 th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top}}
 th{{color:#64748b}} a{{color:#2563eb}}
 .ok{{background:#dcfce7;color:#166534;padding:10px 14px;border-radius:8px;display:inline-block}}
 @media print{{a{{color:#1e293b;text-decoration:none}}}}
</style></head><body>
<h1>Sicherheitsreport – {esc(REPO)}</h1>
<div class="sub">Stand: {esc(now)} · offene Code-Scanning-Funde: <b>{len(alerts)}</b></div>
{'<div class="ok">✅ Keine offenen Funde.</div>' if not alerts else ''}
{('<h2>Überblick nach Schweregrad</h2><table><tr><th>Schweregrad</th><th style="text-align:right">Anzahl</th></tr>' + sev_rows + '</table>') if alerts else ''}
{('<h2>Details</h2><table><tr><th>Schweregrad</th><th>Scanner</th><th>Regel</th><th>Ort</th><th>Alert</th></tr>' + rows + '</table>') if alerts else ''}
<p style="color:#94a3b8;font-size:12px">Erzeugt automatisch aus dem GitHub-Security-Tab · CodeQL, Bandit, Semgrep, Trivy, Scorecard.</p>
</body></html>"""
    with open(OUT + ".html", "w", encoding="utf-8") as f:
        f.write(doc)

    # Kurzfassung für die GitHub-Job-Zusammenfassung
    print(f"OFFENE_FUNDE={len(alerts)}")
    return md_txt


if __name__ == "__main__":
    main()
