#!/usr/bin/env python3
"""Mail-Versand (SMTP) und Reservierungs-Mails: Text/HTML-Rendering,
editierbare Vorlage ({{platzhalter}}), Standard-Vorlage und -Betreff.
Aus aria_kapa.py ausgelagert (v3.0)."""
import sys
from datetime import datetime, timedelta

from kapa.util import _html_escape


def send_mail(args, subject, body, extra_to=(), to_override=None, html=None):
    """Report-Mail über den konfigurierten SMTP-Server (--smtp-server).

    to_override: exakte Empfängerliste (aus den Mail-Regeln). Ist sie gesetzt,
    wird --smtp-to NICHT zusätzlich angeschrieben. html: optionale HTML-Fassung
    (multipart/alternative; Clients ohne HTML zeigen den Klartext)."""
    import smtplib
    from email.message import EmailMessage
    if not args.smtp_server:
        return
    if to_override is not None:
        seen, to = set(), []
        for t in to_override:
            t = str(t or "").strip()
            if "@" in t and t not in seen:
                seen.add(t)
                to.append(t)
    else:
        to = [t.strip() for t in (args.smtp_to or "").split(",") if t.strip()]
        to += [t for t in extra_to if t and "@" in t and t not in to]
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = args.smtp_from or "kapa-dashboard@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    host, _, port = args.smtp_server.partition(":")
    with smtplib.SMTP(host, int(port or 25), timeout=15) as smtp:
        if args.smtp_tls:
            smtp.starttls()
        if args.smtp_user:
            smtp.login(args.smtp_user, args.smtp_password or "")
        smtp.send_message(msg)


def _res_valid_date(r, res_ttl):
    if r.get("created"):
        try:
            d = datetime.fromisoformat(r["created"]) + \
                timedelta(days=(res_ttl - 1 if res_ttl > 0 else 30))
            return d.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return ""


def _res_rows(r, res_ttl):
    """Feldliste (Label, Wert) für die Reservierungs-Mail."""
    return [
        ("ID", r.get("id") or "–"),
        ("Anfrage", r.get("name") or "?"),
        ("Change / Jira", r.get("change") or "–"),
        ("Cluster", r.get("cluster") or "?"),
        ("vCPU", r.get("vcpu", 0)),
        ("RAM", f"{r.get('ram_gb', 0)} GB"),
        ("Storage", f"{r.get('storage_gb') or 0} GB"),
        ("Team", r.get("abteilung") or "–"),
        ("Beantragt", f"von {r.get('von') or '–'} am {r.get('created') or '–'}"),
        ("Gültig bis", _res_valid_date(r, res_ttl) or "–"),
    ]


def reservation_mail_body(r, action, admin, res_ttl):
    """Klartext-Fassung (Fallback für Clients ohne HTML)."""
    approvals = r.get("approvals") or []
    freigaben = ("\n" + "\n".join(
        f"             - {a.get('team') or '?'}: {a.get('by') or '?'} "
        f"am {a.get('on') or '?'}" for a in approvals)) if approvals else " –"
    lines = [f"{label + ':':<14}{value}" for label, value in _res_rows(r, res_ttl)]
    return (f"Kapazitätsreservierung {action}\n\n"
            + "\n".join(lines) + "\n"
            f"{'Freigaben:':<14}{freigaben}\n"
            f"{'Kommentar:':<14}{r.get('comment') or '–'}\n\n"
            f"{action} von {admin} am {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")


# Im Mail-Template (und Betreff) nutzbare Variablen: {{name}} usw.
MAIL_VARS = [
    ("action", "Ereignis (beantragt / genehmigt / abgelehnt / wartet auf Freigabe …)"),
    ("name", "Bezeichnung / Projekt"),
    ("id", "Kapa-ID"),
    ("change", "Change / Jira-Ticket"),
    ("cluster", "Ziel-Cluster"),
    ("source", "vROps-Quelle"),
    ("vcpu", "vCPU-Anzahl"),
    ("ram_gb", "RAM (inkl. „GB“)"),
    ("storage_gb", "Storage (inkl. „GB“)"),
    ("von", "Anforderer"),
    ("team", "Team / Abteilung des Anforderers"),
    ("created", "Gilt ab (Anlagedatum)"),
    ("valid_until", "Gültig bis"),
    ("approvals", "Freigaben (Liste der Team-Freigaben)"),
    ("comment", "letzter Kommentar"),
    ("admin", "ausführende Person"),
    ("date", "Zeitpunkt der Mail"),
    ("current_team", "aktuell zuständiges Team (bei „Team ist dran“)"),
]

DEFAULT_MAIL_SUBJECT = "Kapazitätsreservierung {{action}}: {{name}} ({{cluster}})"

_ML = ("padding:7px 14px 7px 0;color:#6b7280;white-space:nowrap;"
       "border-bottom:1px solid #eef0f3;vertical-align:top")
_MV = "padding:7px 0;border-bottom:1px solid #eef0f3;vertical-align:top"
DEFAULT_MAIL_TEMPLATE = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
    'Helvetica,Arial,sans-serif;color:#1f2937;font-size:14px;line-height:1.5;max-width:600px">\n'
    '  <div style="font-size:17px;font-weight:600;margin:0 0 2px">Kapazitätsreservierung {{action}}</div>\n'
    '  <div style="color:#6b7280;font-size:13px;margin:0 0 16px">{{name}} &middot; {{cluster}}</div>\n'
    '  <table style="border-collapse:collapse;width:100%;border-top:1px solid #eef0f3">\n'
    f'    <tr><td style="{_ML}">ID</td><td style="{_MV}">{{{{id}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Change / Jira</td><td style="{_MV}">{{{{change}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Cluster</td><td style="{_MV}">{{{{cluster}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">vCPU</td><td style="{_MV}">{{{{vcpu}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">RAM</td><td style="{_MV}">{{{{ram_gb}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Storage</td><td style="{_MV}">{{{{storage_gb}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Team</td><td style="{_MV}">{{{{team}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Beantragt</td><td style="{_MV}">von {{{{von}}}} am {{{{created}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Gültig bis</td><td style="{_MV}">{{{{valid_until}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Freigaben</td><td style="{_MV}">{{{{approvals}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Kommentar</td><td style="{_MV}">{{{{comment}}}}</td></tr>\n'
    '  </table>\n'
    '  <div style="color:#9ca3af;font-size:12px;margin-top:16px">{{action}} von {{admin}} am {{date}}</div>\n'
    '  <div style="color:#c3c8d0;font-size:11px;margin-top:4px">VMware Kapazitätsplanung</div>\n'
    '</div>')


def _mail_values(r, action, admin, res_ttl, team=None, html=True):
    """Werte für die Vorlagen-Variablen. html=True escaped für die HTML-Mail,
    html=False für den (plain) Betreff."""
    # html=False landet im Mail-Betreff: CR/LF dort immer zu Leerzeichen falten,
    # sonst lehnt EmailMessage den Header ab und der Versand scheitert still.
    e = _html_escape if html else (
        lambda x: " ".join(str(x if x is not None else "").split()))
    approvals = r.get("approvals") or []
    if html:
        appr = "<br>".join(
            f"{_html_escape(a.get('team') or '?')} · {_html_escape(str(a.get('by') or '?'))} "
            f"am {_html_escape(str(a.get('on') or '?'))}" for a in approvals) or "–"
    else:
        appr = ", ".join(f"{a.get('team') or '?'}: {a.get('by') or '?'}"
                         for a in approvals) or "–"
    return {
        "action": e(action), "id": e(r.get("id") or "–"), "name": e(r.get("name") or "?"),
        "change": e(r.get("change") or "–"), "cluster": e(r.get("cluster") or "?"),
        "source": e(r.get("source") or "–"), "vcpu": e(r.get("vcpu", 0)),
        "ram_gb": e(f"{r.get('ram_gb', 0)} GB"),
        "storage_gb": e(f"{r.get('storage_gb') or 0} GB"),
        "von": e(r.get("von") or "–"), "team": e(r.get("abteilung") or "–"),
        "created": e(r.get("created") or "–"),
        "valid_until": e(_res_valid_date(r, res_ttl) or "–"),
        "approvals": appr, "comment": e(r.get("comment") or "–"),
        "admin": e(admin or "System"),
        "date": e(datetime.now().strftime("%d.%m.%Y %H:%M")),
        "current_team": e(team or "–"),
    }


def render_template(tpl, values):
    """Platzhalter {{var}} in einer Vorlage durch die Werte ersetzen."""
    out = tpl
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def reservation_mail_html(r, action, admin, res_ttl, template=None, team=None):
    """HTML-Mail aus der (ggf. angepassten) Vorlage rendern."""
    return render_template(template or DEFAULT_MAIL_TEMPLATE,
                           _mail_values(r, action, admin, res_ttl, team, html=True))

