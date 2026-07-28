# Security Policy

> 🇩🇪 Deutsche Fassung weiter unten.

## Reporting a vulnerability

Please **do not** open public issues for security problems. Report them
confidentially via **GitHub → Security → “Report a vulnerability”**
([Private Vulnerability Reporting](https://github.com/marcobockelbrink/vmware/security/advisories/new)).
We aim to respond within a few business days.

## Security review welcome

Independent eyes are appreciated. The most valuable place to look is the
**hand-written LDAP BER/DER decoder** in [`kapa/ldap.py`](kapa/ldap.py): it
parses raw bytes straight off an LDAPS socket (i.e. potentially from a
malicious or malfunctioning AD) using a small, self-contained parser. It is
already fuzzed — see [`tests/fuzz/`](tests/fuzz/) — but a careful manual review
is welcome. The sign-in flow (`_post_login`) and the outbound webhook signing
(HMAC-SHA256) are also good, well-bounded targets.

## Automated security checks

Every push to any branch, every pull request to `main`, plus a weekly run,
scan the code with several independent open-source scanners; results appear in
the repository’s **Security** tab:

| Scanner | Purpose |
|---|---|
| **CodeQL** | GitHub’s SAST — injection, path traversal, unsafe patterns |
| **Bandit** | Python security linter (subprocess, crypto, temp files …) |
| **Semgrep** | rule sets for Python, security audit and secrets |
| **Trivy** | vulnerabilities, misconfigurations and secrets (repo + Dockerfile) |
| **OpenSSF Scorecard** | supply-chain / repo hygiene with a score |
| **Dependabot** | up-to-date GitHub Actions and container base image |

## Design hardening

The application uses **only the Python standard library — no third-party
runtime dependencies**, which removes the supply-chain attack surface via
external packages. Further measures are summarised in the
[architecture docs](docs/ARCHITECTURE.en.md) (parameterised SQL access,
BER-encoded LDAP without filter injection, strict Content-Security-Policy,
`SameSite=Lax` sessions, escaping of all foreign data, protection against
CSV/formula injection). Commits and release tags are signed; the container
image ships a build-provenance attestation.

---

# Sicherheitsrichtlinie

## Schwachstelle melden

Bitte Sicherheitslücken **nicht** über öffentliche Issues melden, sondern
vertraulich über **GitHub → Security → „Report a vulnerability"**
([Private Vulnerability Reporting](https://github.com/marcobockelbrink/vmware/security/advisories/new)).
Wir bemühen uns um eine erste Rückmeldung innerhalb weniger Werktage.

## Security-Review willkommen

Unabhängige Augen sind ausdrücklich erwünscht. Am lohnendsten ist der
**handgeschriebene LDAP-BER/DER-Decoder** in [`kapa/ldap.py`](kapa/ldap.py):
Er parst rohe Bytes direkt vom LDAPS-Socket (also potenziell von einem
bösartigen oder fehlerhaften AD) mit einem kleinen, in sich geschlossenen
Parser. Er wird bereits gefuzzt ([`tests/fuzz/`](tests/fuzz/)), ein sorgfältiger
manueller Blick ist trotzdem willkommen. Auch der Anmelde-Pfad (`_post_login`)
und die HMAC-SHA256-Signatur der ausgehenden Webhooks sind gut umgrenzte Ziele.

## Automatische Sicherheitsprüfungen

Jeder Push auf jeden Branch, jeder Pull Request nach `main` sowie ein
wöchentlicher Lauf prüfen den Code mit mehreren unabhängigen, quelloffenen
Scannern (CodeQL, Bandit, Semgrep, Trivy, OpenSSF Scorecard, Dependabot);
die Ergebnisse landen im Reiter **Security** dieses Repos. Die Anwendung nutzt
**nur die Python-Standardbibliothek** — die Angriffsfläche über Dritt-Pakete
entfällt damit. Commits und Release-Tags sind signiert.
