# Startseite Polarlicht – Umsetzungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `index.html` im Polarlicht-Design (Entwurf A) mit Terminal-Kopf (Entwurf B) neu gestalten.

**Architecture:** Eine statische Datei mit eingebettetem CSS und wenig JavaScript, kein Build. Vorlage ist der freigegebene Entwurf `entwurf-a-mit-b-v2.html` (Scratchpad); Inhalte (Texte, Alt-Texte, Links, Hinweise) kommen aus der heutigen `index.html`.

**Tech Stack:** HTML, CSS, Vanilla-JS, `<dialog>`.

**Spec:** `docs/superpowers/specs/2026-11-13-polarlicht-design.md`

## Global Constraints
- Keine externen Bibliotheken, keine fremden Server außer `statistik.bockelbrink.net` (Umami-Skript bleibt unverändert).
- Schriften nur aus `schriften/`, Bilder aus `bilder/`.
- Deutsche Texte, Klassen- und Variablennamen deutsch wie bisher (`--grund`, `--akzent` …).
- Alle 10 Projekte mit ihren bisherigen Links, Texten und Alt-Texten bleiben.

## Review Focus
1. Handy-Breite 390 px: kein Querscrollen, Terminal-Fenster bricht sauber um (Bild unter Text).
2. `prefers-reduced-motion`: Cursor blinkt nicht, kein Lichtkegel, kein Bild-Zoom.
3. Tastatur: jede Kachel und jeder Link mit sichtbarem Fokusrahmen; Dialog schließt mit Esc, Fokus kehrt zurück.
4. Lesbarkeit: Text über Screenshots hat genug Kontrast (Schleier), Badges lesbar.
5. Ohne JavaScript: Seite vollständig lesbar, Comic-Link führt zur MP4-Datei.

---

### Task 1: Neues Design in `index.html`

**Files:** Modify: `index.html` (gesamtes `<style>` und `<body>`), Create: `scripts/pruefen.mjs`

- [ ] **Step 1: Prüfskript schreiben** – `scripts/pruefen.mjs` (Node, ohne Abhängigkeiten) liest `index.html` und bricht mit Exit 1 ab, wenn: eine der Adressen `einfachstoppen.de`, `ki-espresso.de`, `vmware/`, `family.bockelbrink.net`, `mylittlebikepark.de`, `zitadel-netbird-sync`, `claude-matrix-bot`, `db-wifi-check`, `einfachstoppen.de/bauen`, `einfachstoppen-tor`, `impressum/` fehlt; ein `<img>` ohne `alt` ist; `https://` auf einen anderen Host als `statistik.bockelbrink.net`, `github.com` oder die Projektadressen in `src`/`href` von `<script>`, `<link>`, `<img>` zeigt; `class="fenster"`, `class="bento"` oder `<dialog` fehlt.
- [ ] **Step 2: Laufen lassen** – `node scripts/pruefen.mjs` → FAIL (fehlt `fenster`, `bento`, `<dialog`).
- [ ] **Step 3: Umsetzen** – CSS und Markup aus dem Entwurf übernehmen, auf die Tokens in `:root` umstellen; Kopf: Glas-Leiste + Terminal-Fenster mit `<h1>` als echter Überschrift; Zahlenleiste; Bento-Raster: einfachstoppen `gross` (Links: Comic ansehen → öffnet `<dialog>` mit dem Video, Fallback `href` auf die MP4; Web-App; Zeittor bauen; Kamera-App; Quellcode), ki-espresso und Kapazitätsplanung `breit`, Family, Bikepark, MTB Bielefeld und insta-manager normal mit Hinweis „Kein Link“, db-wifi-check mit Bild, zitadel-netbird-sync und claude-matrix-bot als Code-Kacheln. Kacheln mit Link sind `<a>`, ohne Link `<div>`. JS: Lichtkegel (`pointermove`, nur wenn keine reduzierte Bewegung) und Dialog öffnen/schließen. `@media (prefers-reduced-motion: reduce)` schaltet Animationen ab.
- [ ] **Step 4: Prüfen** – `node scripts/pruefen.mjs` → PASS; im Browser bei 1440 px und 390 px Screenshot, `document.documentElement.scrollWidth <= innerWidth`, Konsole ohne Fehler, Tab-Reihenfolge und Dialog per Tastatur.
- [ ] **Step 5: Commit** – `git add index.html scripts/pruefen.mjs docs/ && git commit -m "feat: Startseite im Polarlicht-Design mit Terminal-Kopf"`

### Task 2: Review und PR
- [ ] `code-reviewer`-Agent, dann `sicherheits-review`; Befunde beheben.
- [ ] `gh pr create` mit Beschreibung, Prüfergebnis und Review-Ergebnis. **Nicht mergen ohne Marcos „roll aus“** – Merge auf `main` veröffentlicht die Seite über GitHub Pages.
