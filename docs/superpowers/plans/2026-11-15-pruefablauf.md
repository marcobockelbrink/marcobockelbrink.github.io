# Abschnitt „So wird geprüft“ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Neuer Abschnitt auf der Startseite, der im Terminal-Stil zeigt, wie Marcos Projekte geprüft werden.

**Architecture:** Alles in `index.html`: eine `<section>` nach `#projekte`, CSS mit den vorhandenen Tokens, Einblenden über einen eigenen kleinen IntersectionObserver. `scripts/pruefen.mjs` ist der Test.

**Tech Stack:** statisches HTML/CSS/JS, Node für `scripts/pruefen.mjs`, keine Abhängigkeiten.

**Spec:** `docs/superpowers/specs/2026-11-15-pruefablauf-design.md`

## Global Constraints

- Öffentliche Seite: keine Rechnernamen, Ports, Runner-Namen, Secret-Namen, Firmen-Repos.
- Keine fremden Server (`pruefen.mjs` prüft das bereits).
- Bei `prefers-reduced-motion: reduce` sofort alles sichtbar.
- Ohne JavaScript ist alles sichtbar: versteckt wird nur, wenn das Skript eine Klasse setzt.
- Kein waagerechtes Scrollen bei 360 px Breite.

## Review Focus

- **JS aus oder Observer fehlt:** Die Zeilen müssen trotzdem sichtbar sein. Abgedeckt durch die Regel „erst das Skript versteckt“, Sichtprüfung im Browser.
- **Schmales Handy (360 px):** Die Häkchen-Gruppen brechen um, nichts läuft über. Prüfung per `resize_page` und `scrollWidth <= innerWidth`.
- **Dunkel/Hell:** Der Kontrast im Terminal stimmt in beiden Modi. Sichtprüfung per Screenshot in beiden Farbschemata.
- **Screenreader:** Die Häkchen „✓“ werden nicht als Zeichensalat vorgelesen. Die Zeilen bekommen Text, die Symbole `aria-hidden`.
- **Verbotene Begriffe schleichen sich später ein:** abgedeckt durch die Negativliste in `pruefen.mjs`.

---

### Task 1: Abschnitt „So wird geprüft“

**Files:**
- Modify: `scripts/pruefen.mjs` (neue Prüfungen vor der Ausgabe)
- Modify: `index.html` (Section nach `</section>` von `#projekte`, CSS im `<style>`, JS im vorhandenen Skript)

**Interfaces:**
- Produces: `<section class="blatt" id="pruefung" aria-labelledby="pruefung-titel">` mit `<h2 id="pruefung-titel">So wird geprüft</h2>` und Zeilen `.lauf-zeile`.

- [ ] **Step 1: Failing test in `scripts/pruefen.mjs`**

```js
muss('id="pruefung"', "Abschnitt");
muss('<h2 id="pruefung-titel">So wird geprüft</h2>', "Überschrift");
for (const text of ["Tests zuerst", "KI-Code-Review", "Sicherheits-Review", "SonarQube", "Renovate", "bereit zum Merge"])
  muss(text, "Prüfschritt");
// keine Interna: allgemeine Muster + lokale scripts/verboten.local.txt (siehe Spec)
```

- [ ] **Step 2: Laufen lassen, Fehlschlag zeigen**

Run: `node scripts/pruefen.mjs`
Expected: Exit 1 mit „Abschnitt fehlt: id="pruefung"“, „Überschrift fehlt“ und „Prüfschritt fehlt“. Danach mit Marco besprechen und auf sein OK warten.

- [ ] **Step 3: Section, CSS, JS umsetzen**

Text exakt wie im Entwurf der Spec. Entscheidung bei der Umsetzung: vorhandenes Muster `.einblenden` wiederverwenden (deckt ohne JS, reduced motion und Druck schon ab), eigene ruhige `.lauf`-Box statt des kreisenden `.fenster`-Rands; Verzögerung der Zeilen im Abschnitt eigens nacheinander gesetzt.

- [ ] **Step 4: Test grün + Sichtprüfung**

Run: `set -o pipefail; node scripts/pruefen.mjs`
Expected: Exit 0. Danach im Browser bei 1280 px und 360 px die Punkte aus dem Review Focus prüfen.

- [ ] **Step 5: Review und Commit**

Erst `code-reviewer`, dann `sicherheits-review` (öffentliche Seite, Interna). Danach:

```bash
git add index.html scripts/pruefen.mjs docs/superpowers
git commit -m "feat: Abschnitt „So wird geprüft“ auf der Startseite"
```

Dann der PR. GitHub Pages veröffentlicht `main` sofort, also ist Mergen hier Ausrollen: erst auf Marcos Zuruf mergen.
