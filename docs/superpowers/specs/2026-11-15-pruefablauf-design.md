# Startseite: Abschnitt „So wird geprüft“

**Stand:** 15.11.2026 · **Größe:** kleine Änderung

## Absicht
Interessierte mit technischem Verstand sollen auf einen Blick sehen, wie Marcos Projekte gebaut und geprüft werden: was wo läuft und was geprüft wird, ohne tief ins Detail zu gehen. Die Grundlage ist Marcos privates Architekturdokument zur CI, hier stark verkürzt.

## Ziele / Nicht-Ziele
- **Ziel:** ein knapper Abschnitt unter „Was ich gebaut habe“, im Terminal-Stil des Kopfes.
- **Nicht-Ziel:** keine Liste je Repo, keine Einstellungen, keine Links ins private Repo.
- **Auf keinen Fall:** Rechnernamen, Ports, Runner-Namen, Secret-Namen oder Firmen-Repos. Die Seite ist öffentlich.

## Ansätze
- A – Leiste mit vier Stationen: auf einen Blick klar, aber eher Standard.
- **B – Pipeline-Lauf im Terminal-Stil (gewählt):** greift den Terminal-Kopf auf und zeigt die richtige Tiefe, eine Zeile mit Häkchen je Prüfung.
- C – Karten: verworfen, weil sie die Projektkarten nur wiederholen würden.

## Entwurf
Überschrift „So wird geprüft“, ein Satz Einleitung, darunter ein Terminalfenster wie im Kopf:

```
$ git push
▸ lokal     Tests zuerst ✓  KI-Code-Review ✓  Sicherheits-Review ✓  signierter Commit ✓
▸ CI        eigener Runner + GitHub Actions
            SonarQube ✓  KI-Review im PR ✓  Geheimnisse ✓  Sicherheitslücken ✓  Images ✓
▸ laufend   Renovate: Updates nach Wartezeit, Sicherheits-Updates sofort
✔ bereit zum Merge
```

- Die Zeilen erscheinen beim Hineinscrollen nacheinander, über den vorhandenen IntersectionObserver. Bei `prefers-reduced-motion` stehen sie sofort komplett da.
- Auf dem Handy brechen die Häkchen-Gruppen um. Es gibt kein waagerechtes Scrollen.
- Alles inline, keine fremden Server. Farben aus den vorhandenen Tokens (die Seite hat nur ein dunkles Design).

## Tests
`scripts/pruefen.mjs` prüft zusätzlich:
- den neuen Abschnitt und seine Überschrift
- dass keine Interna auftauchen: allgemeine Muster (lokale Rechner, Rechner:Port, Secret-Namen, private Netze) in `index.html` und `docs/`, konkrete Namen nur aus der lokalen, nicht eingecheckten `scripts/verboten.local.txt`

## Dem Umsetzer überlassen
Genaue Abstände, Verzögerung der Zeilen, Feinheiten der Wortwahl.
