#!/usr/bin/env python3
"""Frontend-Rauchtest: lädt die ausgelieferte Seite headless, führt das
eingebettete JavaScript aus (inkl. aller Verwaltungs-Reiter und der
datengetriebenen Render-Funktionen) und schlägt fehl, sobald ein
JavaScript-Fehler auftritt.

Fängt vor allem die Klasse „gerade \" bricht den JS-String" ab (die uns schon
zweimal erwischt hat) – solche Parse-Fehler killen das GESAMTE Skript und
tauchen sofort in der Browser-Konsole auf.

Nur Standardbibliothek. Braucht einen Chrome/Chromium im Pfad (Pfad überschreib-
bar via Umgebungsvariable CHROME_BIN). Läuft lokal wie in der CI.

    python3 tests/frontend_check.py
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "aria_kapa.py")

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium-browser", "chromium",
]

# Test-Harness: wird an die ausgelieferte Seite angehängt und übt möglichst viel
# JavaScript aus. Setzt am Ende den Seitentitel als eindeutiges Erfolgssignal –
# ist das Hauptskript kaputt (Parse-Fehler), sind die Funktionen undefiniert und
# der Titel bleibt auf FRONTEND_FAIL stehen.
HARNESS = r"""
<script>
(function () {
  var errs = [];
  window.addEventListener("error", function (e) {
    var m = (e && (e.error && e.error.message || e.message)) || "";
    if (m && !/Failed to load|net::|Loading (the|chunk)/i.test(m)) errs.push("window.onerror: " + m);
  });
  function need(n) { if (typeof window[n] !== "function") errs.push("fehlt: " + n + "()"); }
  ["render", "setView", "setAdmTab", "esc", "renderWebhooks",
   "renderVsanOverrides", "vsanOptions"].forEach(need);
  function run(label, fn) { try { fn(); } catch (e) { errs.push(label + ": " + (e && e.message || e)); } }
  if (typeof setAdmTab === "function")
    ["users","sel","mail","hooks","ann","auto","vis","storcfg","net","imp","tok","conf"]
      .forEach(function (k) { run("setAdmTab(" + k + ")", function () { setAdmTab(k); }); });
  if (typeof renderWebhooks === "function")
    run("renderWebhooks", function () {
      window.WEBHOOKS = [{ id: "a", url: "https://x/h", events: ["created"],
                           active: true, desc: "d", has_secret: true }];
      renderWebhooks(); });
  if (typeof renderVsanOverrides === "function")
    run("renderVsanOverrides", function () { renderVsanOverrides({ "C1": 0.5 }); });
  if (typeof vsanOptions === "function")
    run("vsanOptions", function () { vsanOptions(0.75); });
  if (typeof render === "function") run("render", function () { render(); });
  // Cluster-Detailkarte rendern — der Pfad card() war bisher ungetestet
  // (ein color()-Refactor-Bug wäre hier unentdeckt geblieben). Direkter
  // Aufruf mit echten Demo-Daten statt DOM-Klick: robust und trifft
  // exakt die Template-Logik inkl. Workload-/Storage-/Netzwerk-Teilen.
  if (typeof card === "function")
    run("card(CLUSTERS[0])", function () {
      var html = card(CLUSTERS[0], 0, false);
      if (!html || html.indexOf("card") < 0)
        throw new Error("card() lieferte kein Karten-Markup");
    });
  // Auslastungs-Balken: nicht nur „JS läuft", sondern auch WIRKLICH SICHTBAR.
  // Fängt CSS-Regressionen (fehlende .bar-fill/.bg-*-Regel -> Breite 0),
  // die einem reinen JS-Fehler-Check entgehen. Prüft die berechnete Breite
  // und die Ampelfarbe eines echten, gefüllten Balkens im DOM.
  run("Balken sichtbar (Breite + Farbe)", function () {
    var fills = [].slice.call(document.querySelectorAll(".bar .bar-fill"))
      .filter(function (el) {
        var w = (el.style.getPropertyValue("--w") || "").trim();
        return w && w !== "0%";   // ein Balken mit echter Soll-Füllung
      });
    if (!fills.length) throw new Error("kein gefüllter .bar-fill im DOM");
    var el = fills[0], cs = getComputedStyle(el);
    if (parseFloat(cs.width) < 1)
      throw new Error("Balken-Füllung hat 0px Breite (--w wirkt nicht; "
                      + ".bar-fill-Regel fehlt?)");
    var bg = cs.backgroundColor;
    if (!bg || bg === "rgba(0, 0, 0, 0)" || bg === "transparent")
      throw new Error("Balken-Füllung ohne Farbe (.bg-ok/warn/crit fehlt?)");
  });
  document.title = errs.length ? ("FRONTEND_FAIL: " + errs.join(" | ")) : "FRONTEND_OK";
})();
</script>
"""

# JS-Fehlersignaturen in der Chrome-stderr-Ausgabe (eindeutige Code-Bugs, keine
# Netzwerk-/Ressourcenfehler, die auf file:// erwartbar sind).
ERR_RX = re.compile(
    r"(SyntaxError|ReferenceError|is not defined|is not a function|Unexpected token)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def find_chrome():
    from shutil import which
    for c in CHROME_CANDIDATES:
        if not c:
            continue
        if os.path.isabs(c) and os.path.exists(c):
            return c
        w = which(c)
        if w:
            return w
    return None


def main():
    chrome = find_chrome()
    if not chrome:
        print("FEHLER: Kein Chrome/Chromium gefunden (CHROME_BIN setzen).")
        return 2

    port = free_port()
    data = tempfile.mkdtemp(prefix="kapa_fe_")
    proc = subprocess.Popen(
        [sys.executable, APP, "--serve", "--sample", "--bind", "127.0.0.1",
         "--port", str(port), "--data-dir", data],
        stdout=(logf := open(os.path.join(data, "app.log"), "w")),
        stderr=subprocess.STDOUT)
    html_path = os.path.join(data, "page.html")
    try:
        base = f"http://127.0.0.1:{port}"
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                    h = json.loads(r.read().decode())
                # WICHTIG: auf fertige Cluster-Daten warten — sonst wird die
                # Seite mit leerem __DATA__ geholt und alle datengetriebenen
                # Pfade (card(), Tabellen) laufen gegen eine leere Liste.
                if h.get("clusters", 0) > 0 and not h.get("refreshing"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("FEHLER: App nicht erreichbar oder Daten nicht bereit.")
            return 2

        # Ausgelieferte Seite holen und den Harness anhängen.
        with urllib.request.urlopen(base + "/", timeout=15) as r:
            page = r.read().decode("utf-8", "replace")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page + HARNESS)

        run = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", "--no-first-run",
             "--no-default-browser-check",
             "--enable-logging=stderr", "--v=1", "--dump-dom",
             "--virtual-time-budget=4000", "file://" + html_path],
            capture_output=True, text=True, timeout=60)
        dom, log = run.stdout, run.stderr

        # 1) Titel-Marker (Hauptsignal): JS lief und Harness ohne Fehler durch?
        m = re.search(r"<title>([^<]*)</title>", dom)
        title = (m.group(1) if m else "").strip()

        # 2) Konsolen-Fehler in der stderr-Ausgabe (Parse-/Referenzfehler).
        console_errs = [ln.strip() for ln in log.splitlines()
                        if "CONSOLE" in ln and ERR_RX.search(ln)
                        and "Failed to load" not in ln and "net::" not in ln]

        ok = (title == "FRONTEND_OK") and not console_errs
        if ok:
            print("✅ Frontend-Check bestanden: JS geladen, alle Reiter gerendert, "
                  "keine Konsolenfehler.")
            return 0
        print("❌ Frontend-Check FEHLGESCHLAGEN:")
        if title != "FRONTEND_OK":
            print("   Titel-Signal:", title or "(kein Titel – Skript evtl. gar nicht geladen)")
        for e in console_errs[:10]:
            # nur die eigentliche Meldung zeigen
            mm = re.search(r'"(.*)"', e)
            print("   Konsole:", mm.group(1) if mm else e)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        import shutil
        logf.close()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
