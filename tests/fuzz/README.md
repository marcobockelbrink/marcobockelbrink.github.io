# Fuzzing — Parser für ungetrusteten Input

Fuzz-Ziele für die Stellen, an denen das Dashboard **fremde, nicht
vertrauenswürdige Daten** parst — allen voran der handgeschriebene
**BER/TLV-Decoder** in `kapa/ldap.py`, der rohe Bytes eines LDAPS-Servers
(also potenziell eines bösartigen oder fehlerhaften AD) verarbeitet.

## Ausführen

**Ohne Zusatzsoftware** (Standardbibliothek — läuft überall, auch in der CI
und im Smoke-Test):

```bash
python3 tests/fuzz/fuzz_parsers.py --stdlib
KAPA_FUZZ_ITER=50000 python3 tests/fuzz/fuzz_parsers.py --stdlib
```

**Coverage-guided mit [Atheris](https://github.com/google/atheris)** (findet
tiefer liegende Pfade; reines Entwickler-/CI-Werkzeug, **keine**
Laufzeit-Abhängigkeit der App):

```bash
pip install atheris
python3 tests/fuzz/fuzz_parsers.py        # läuft endlos, bis ein Fund kommt
```

Ein Absturz mit einer **unerwarteten** Ausnahme ist ein Fund; saubere
Ablehnungen (`ValueError`/`IndexError`/…) sind das gewünschte Verhalten.

## Kontinuierliches Fuzzing (optional)

Die Ziele sind Atheris-kompatibel und damit direkt für
[ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) oder OSS-Fuzz
verwendbar, falls dauerhaftes CI-Fuzzing gewünscht ist. Das bringt eine
Container-Build-Kette mit und ist bewusst noch nicht eingerichtet.
