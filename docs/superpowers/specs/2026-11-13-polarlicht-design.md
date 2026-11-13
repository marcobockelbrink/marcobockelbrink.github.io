# Startseite im Polarlicht-Design

## Warum
Marco möchte eine modernere, ansprechendere Startseite („modernes cooles UI“).
Ausgewählt aus drei Entwürfen: **A · Polarlicht** (shadcn-Stil, Bento-Raster)
mit dem **Kopf aus B · Werkstatt** (Terminal-Fenster).

## Ziele
- Kopf: schmale Glas-Leiste (Marke, Menü, „● 2 Projekte live“), darunter ein
  Terminal-Fenster (Ampelpunkte, `~/marco — zsh`) mit Befehlszeilen, großer
  Überschrift mit Farbverlauf, blinkendem Cursor, Knöpfen und dem Porträt rechts.
- Hintergrund: grün-violettes Licht und feines Gitter hinter dem Kopf.
- Zahlenleiste als dreigeteilter Kasten.
- Projekte als Bento-Raster: Screenshots als Kachelhintergrund mit Schleier,
  Badges oben links („● live“, Art), Lichtkegel folgt der Maus, Code-Kacheln
  für Projekte ohne Bild.
- einfachstoppen als große Kachel; Comic, Web-App und Zeittor/Kamera als
  kleine Links unten; der Comic-Film spielt auf Klick in einem Dialog.
- Alle zehn Projekte bleiben mit ihren Texten, Links und Hinweisen erhalten.

## Nicht-Ziele / Anti-Ziele
- Keine externen Bibliotheken, kein Build, keine fremden Server (Impressum).
- Keine Verschlechterung bei Barrierefreiheit: Fokusrahmen, Alt-Texte,
  `prefers-reduced-motion` (Cursor, Lichtkegel, Zoom aus), Handy-Breite ohne
  Querscrollen, Kontraste lesbar.

## Ansätze
1. MUI / shadcn / nyxhora als React-Bibliothek – verworfen: braucht Build und
   Pages-Action, MUI wirkt nach Google, nyxhora ist Beta ohne Pflege.
2. **shadcn-Stil von Hand in der bestehenden `index.html`** – gewählt (Marco:
   „externe Bibliotheken müssen nicht sein“).

## Festgelegt
- Eine Datei `index.html`, eigenes CSS, ein paar Zeilen JavaScript
  (Lichtkegel, Film-Dialog). Schriften und Bilder wie bisher aus dem Repo.
- Alte Seite gesichert als Tag `startseite-v1`.

## Dem Bauenden überlassen
- Genaue Größe der Kacheln je Projekt, Badge-Texte aus den bisherigen Etiketten.
- Inhalt der Code-Kacheln (kurze, ehrliche Beispielzeilen).
