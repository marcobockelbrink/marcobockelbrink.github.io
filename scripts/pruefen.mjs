// Prüft index.html ohne Abhängigkeiten: node scripts/pruefen.mjs
// Bricht mit Exit 1 ab, wenn ein Projektlink, ein Alt-Text oder ein
// Bauteil des Designs fehlt oder etwas von einem fremden Server geladen wird.
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const fehler = [];

const adressen = [
  "https://einfachstoppen.de", "https://ki-espresso.de", 'href="vmware/"',
  "https://family.bockelbrink.net", "https://mylittlebikepark.de",
  "zitadel-netbird-sync", "claude-matrix-bot", "db-wifi-check",
  "https://einfachstoppen.de/bauen", "einfachstoppen-tor", 'href="impressum/"',
  "bilder/einfachstoppen-comic.mp4",
];
for (const a of adressen) if (!html.includes(a)) fehler.push(`Adresse fehlt: ${a}`);

for (const img of html.match(/<img\b[^>]*>/g) ?? [])
  if (!/\balt=/.test(img)) fehler.push(`Bild ohne alt: ${img.slice(0, 80)}`);

// Geladene Dateien (script, link, img, video, source) nur vom eigenen Server
// oder der eigenen Besucherzählung.
const laden = /<(?:script|link|img|video|source)\b[^>]*\b(?:src|href|poster)="(https?:\/\/[^"]+)"/g;
for (const [, url] of html.matchAll(laden))
  if (new URL(url).host !== "statistik.bockelbrink.net") fehler.push(`Fremder Server: ${url}`);

for (const teil of ['class="fenster"', 'class="bento"', "<dialog"])
  if (!html.includes(teil)) fehler.push(`Bauteil fehlt: ${teil}`);

if (fehler.length) {
  console.error(fehler.join("\n"));
  process.exit(1);
}
console.log("index.html: alles da.");
