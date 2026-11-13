// Prüft index.html ohne Abhängigkeiten: node scripts/pruefen.mjs
// Bricht mit Exit 1 ab, wenn ein Projektlink, ein Text, ein Alt-Text oder
// ein Bauteil des Designs fehlt oder etwas von einem fremden Server geladen wird.
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const fehler = [];
const muss = (teil, was) => { if (!html.includes(teil)) fehler.push(`${was} fehlt: ${teil}`); };

// Links als Attribut, damit sichtbarer Text sie nicht vortäuscht.
for (const ziel of [
  "https://einfachstoppen.de", "https://einfachstoppen.de/bauen", "https://einfachstoppen.de/#/kamera",
  "https://github.com/marcobockelbrink/einfachstoppen-tor", "bilder/einfachstoppen-comic.mp4",
  "https://ki-espresso.de", "vmware/", "https://family.bockelbrink.net", "https://mylittlebikepark.de",
  "https://github.com/marcobockelbrink/kubernetes-fun/tree/main/db-wifi-check",
  "https://github.com/marcobockelbrink/zitadel-netbird-sync",
  "https://github.com/marcobockelbrink/claude-matrix-bot", "impressum/",
]) muss(`href="${ziel}"`, "Link");

for (const text of [
  "Kein Link: steht in keinem App Store.", "Kein Link: nur für den Verein.",
  "Termine, Aufgaben, Listen und Rezepte, mit Abgleich zu Outlook.",
  "Film herunterladen (MP4, 1,9 MB)", 'aria-label="Comic-Film zu einfachstoppen, ohne Ton"',
]) muss(text, "Text");

const titel = html.match(/<h3>[^<]+<\/h3>/g) ?? [];
if (titel.length !== 10) fehler.push(`Erwartet 10 Projekttitel, gefunden ${titel.length}`);

for (const img of html.match(/<img\b[^>]*>/g) ?? [])
  if (!/\salt="[^"]+"/.test(img)) fehler.push(`Bild ohne alt: ${img.slice(0, 80)}`);

// Geladene Dateien nur vom eigenen Server oder der eigenen Besucherzählung.
const erlaubt = (url) => new URL(url, "https://bockelbrink.net/").host === "statistik.bockelbrink.net";
const extern = (url) => /^(https?:)?\/\//.test(url);
for (const [tag] of html.matchAll(/<(?:script|link|img|video|source|audio|iframe|embed|object)\b[^>]*>/g)) {
  const istLink = /^<link\b/.test(tag) && !/rel="?(?:stylesheet|preload|icon|modulepreload)/.test(tag);
  if (istLink) continue;
  for (const [, wert] of tag.matchAll(/\s(?:src|href|poster|srcset|data)\s*=\s*["']?([^"'>]+)/g))
    for (const url of wert.split(",").map((s) => s.trim().split(/\s+/)[0]))
      if (extern(url) && !erlaubt(url)) fehler.push(`Fremder Server: ${url}`);
}
for (const [, url] of html.matchAll(/(?:url\(\s*["']?|@import\s+["'])([^"')]+)/g))
  if (extern(url)) fehler.push(`Fremder Server im CSS: ${url}`);

for (const teil of ['class="fenster"', 'class="bento"', "<dialog"]) muss(teil, "Bauteil");

if (fehler.length) {
  console.error(fehler.join("\n"));
  process.exit(1);
}
console.log("index.html: alles da.");
