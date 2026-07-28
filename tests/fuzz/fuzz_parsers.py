#!/usr/bin/env python3
"""Fuzz-Ziele für die Parser, die UNGETRUSTETEN Input verarbeiten:

  * kapa.ldap._iter_tlv   – der handgeschriebene BER/TLV-Decoder; frisst rohe
                            Bytes direkt von einem LDAPS-Server (also potenziell
                            von einem bösartigen oder kaputten AD)
  * kapa.ldap._dn_to_cn   – zerlegt Distinguished Names aus AD-Antworten
  * kapa.collect.parse_tag_json – liest JSON aus vROps-Tag-Eigenschaften

Ziel: keine Eingabe darf eine UNERWARTETE Ausnahme auslösen (nur die sauberen
Ablehnungen ValueError/IndexError/… sind ok) – sonst ist es ein Fund.

Zwei Modi, dasselbe Ziel `_one_input`:

  * Coverage-guided mit Atheris (für OSS-Fuzz / ClusterFuzzLite / lokal):
        pip install atheris
        python3 tests/fuzz/fuzz_parsers.py            # läuft endlos

  * Standardbibliothek (kein Atheris nötig, für den Smoke-Lauf und die CI):
        python3 tests/fuzz/fuzz_parsers.py --stdlib
        KAPA_FUZZ_ITER=20000 python3 tests/fuzz/fuzz_parsers.py --stdlib

Nur Standardbibliothek im stdlib-Modus – passt zur Projektlinie; Atheris ist
reines Entwickler-/CI-Werkzeug, keine Laufzeit-Abhängigkeit der App.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from kapa.ldap import _iter_tlv, _dn_to_cn          # noqa: E402
from kapa.collect import parse_tag_json             # noqa: E402

# Atheris ist optional: ist es da, fuzzen wir coverage-guided; sonst greift
# der Standardbibliothek-Modus. Der Import steht bewusst auf Modulebene
# (echtes Fuzz-Werkzeug, kein Alibi).
try:
    import atheris
except ImportError:
    atheris = None

# Saubere, erwartete Ablehnungen kaputter Eingaben – KEIN Fund.
EXPECTED = (ValueError, IndexError, KeyError, TypeError, UnicodeError,
            OverflowError, RecursionError)


def _one_input(data):
    """Ein Fuzz-Durchlauf über alle drei Parser mit denselben Rohbytes."""
    try:
        list(_iter_tlv(data))
    except EXPECTED:
        pass
    s = data.decode("latin1")
    try:
        _dn_to_cn(s)
    except EXPECTED:
        pass
    try:
        parse_tag_json(s)
    except EXPECTED:
        pass


def _atheris_main():
    def target(raw):
        _one_input(bytes(raw))
    atheris.Setup(sys.argv, target)
    atheris.Fuzz()


def _stdlib_main():
    import random
    seed = int(os.environ.get("KAPA_FUZZ_SEED", "0")) or None
    rng = random.Random(seed)
    n = int(os.environ.get("KAPA_FUZZ_ITER", "3000"))
    for _ in range(n):
        length = rng.randint(0, 64)
        _one_input(bytes(rng.randrange(256) for _ in range(length)))
    print(f"stdlib-Fuzz: {n} Runden ohne unerwarteten Absturz ✅")
    return 0


if __name__ == "__main__":
    if atheris is not None and "--stdlib" not in sys.argv:
        _atheris_main()
    else:
        if atheris is None and "--stdlib" not in sys.argv:
            print("Atheris nicht installiert -> Standardbibliothek-Fuzz "
                  "(pip install atheris fuer coverage-guided Fuzzing).")
        sys.exit(_stdlib_main())
