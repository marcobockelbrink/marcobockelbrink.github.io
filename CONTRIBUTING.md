# Mitmachen / Contributing

Danke fürs Reinschauen! 🙌 Dieses Dokument bringt dich in wenigen Minuten von
„geklont" zu „Änderung als Pull Request" — und nennt die paar Eigenheiten, die
das Projekt bewusst so hält.

> 🇬🇧 English: this guide is German (the project's primary language). The code
> comments and commit messages are German too; docs exist bilingually
> (`README.md`/`README.en.md`, `docs/ARCHITEKTUR.md`/`ARCHITECTURE.en.md`).

## Grundprinzipien (bitte beachten)

- **Nur Python-Standardbibliothek** — kein `pip`, kein `npm`, kein Build-Schritt.
  Das ist Absicht: Das Dashboard läuft auf jedem RHEL-Host ohne Paket-Zoo.
  Entwickler-/Test-Werkzeuge (z. B. `atheris` fürs Fuzzing, `python-pptx` für
  die Präsentation) sind ok, solange sie **keine Laufzeit-Abhängigkeit** werden.
- **Alles lesbarer Klartext im Repo** — kein kompiliertes/minifiziertes Artefakt.
- **Zweisprachig DE/EN**: Neue UI-Texte kommen ins `const I18N`-Wörterbuch in
  `frontend/index.html`; betroffene `.en`-Doku-Zwillinge mitpflegen.
- **Rechte immer serverseitig durchsetzen**, nicht nur im UI ausblenden.

## Loslegen

```bash
git clone https://github.com/marcobockelbrink/vmware.git
cd vmware
python3 aria_kapa.py --sample --serve      # Demo mit Beispieldaten, ohne vROps
# -> http://localhost:8080
```

Python 3.8+ genügt, sonst nichts. Für die englische Oberfläche den Browser auf
Englisch stellen oder `?lang=en` anhängen.

## Tests — vor jedem Pull Request grün

```bash
python3 -m py_compile aria_kapa.py kapa/*.py   # Syntax
python3 tests/smoke.py                         # ~123 End-to-End-Checks (HTTP)
python3 tests/unit.py                          # Rechenkern (Kapazitätsmathe)
python3 tests/frontend_check.py                # Headless-Browser: JS-Fehler + Rendering
```

`tests/smoke.py` ruft `unit.py` und einen kurzen Fuzz-Lauf gleich mit; die
gleichen Checks laufen in der CI. Braucht einen Chrome/Chromium im Pfad
(`CHROME_BIN` überschreibt).

**Echte AD-Anmeldung testen** (optional, braucht Docker):

```bash
cd tests/ad && docker compose up -d       # Samba-AD-DC, LDAPS auf :6360
cd ../.. && python3 tests/ldap_live.py     # echter LDAPS-Test
```

**Fuzzing** der Parser für ungetrusteten Input (BER-Decoder u. a.):

```bash
python3 tests/fuzz/fuzz_parsers.py --stdlib          # ohne Zusatzsoftware
pip install atheris && python3 tests/fuzz/fuzz_parsers.py   # coverage-guided
```

## Der Pull-Request-Flow

Substanzielle Änderungen laufen über einen PR — auch wenn du selbst mergen
darfst; so hängen die CI-Checks sichtbar dran und ein Kollege kann drüberschauen:

```bash
git checkout -b kurz-beschreibender-name
# … Änderung + Tests grün …
git push -u origin kurz-beschreibender-name
gh pr create            # CI läuft automatisch an
# … auf grüne Checks warten …
gh pr merge --squash --delete-branch
```

Über `CODEOWNERS` werden passende Reviewer automatisch angefragt. Kleine,
unkritische Hotfixes dürfen auch direkt auf `main` — deine Entscheidung pro
Änderung.

## Kleine, aber wichtige Konventionen

- **Commits & Tags signieren** (SSH-Signing) — dann erscheint „Verified".
- **Release?** Version in `kapa/constants.py` bumpen, Demo neu erzeugen
  (`python3 aria_kapa.py --sample --output kapa_dashboard_demo.html`), und die
  Image-Tags mitziehen (k8s-Manifest, Helm-`Chart.yaml`, Deploy-READMEs).
- **JS-Strings:** In deutschen Texten **keine geraden `"`** verwenden — die
  brechen den JS-String. Typografische `„…"` nehmen oder umformulieren.
  (`frontend_check` fängt solche Fehler ab.)
- Struktur & Entwurfsentscheidungen: siehe `docs/ARCHITEKTUR.md`.

## Lizenz

Mit einem Beitrag stimmst du zu, dass er unter der **MIT-Lizenz** dieses
Projekts steht (siehe `LICENSE`).
