// Prüft index.html ohne Abhängigkeiten: node scripts/pruefen.mjs
// Bricht mit Exit 1 ab, wenn ein Projektlink, ein Text, ein Alt-Text oder
// ein Bauteil des Designs fehlt oder etwas von einem fremden Server geladen wird.
import { readFileSync, readdirSync } from "node:fs";

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
  "https://apps.apple.com/de/app/id6805995624",
  "https://play.google.com/store/apps/details?id=de.mtbbielefeld.app",
]) muss(`href="${ziel}"`, "Link");

for (const text of [
  "Kein Link: nicht öffentlich.", "<h3>Social Media Manager</h3>",
  "Termine, Aufgaben, Listen, Rezepte und feste Orte, mit Abgleich zu Outlook.",
  "Für Instagram und LinkedIn", "auf Instagram und LinkedIn",
  "Film herunterladen (MP4, 1,9 MB)", 'aria-label="Comic-Film zu einfachstoppen, ohne Ton"',
]) muss(text, "Text");

for (const [, id] of html.matchAll(/href="#([^"]+)"/g))
  if (!html.includes(`id="${id}"`)) fehler.push(`Sprungziel fehlt: #${id}`);

const live = (html.match(/class="b live"/g) ?? []).length;
if (!html.includes(`data-zaehle="${live}">${live}</b><span>live im Netz`))
  fehler.push(`Zähler „live im Netz“ passt nicht zu ${live} live-Badges`);

if (html.includes("steht in keinem App Store")) fehler.push("Veraltet: MTB-App steht inzwischen in den Stores");

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

for (const teil of ['class="fenster"', 'class="bento"', "<dialog", 'class="dock"', 'id="lampe"', "data-zaehle=", "prefers-reduced-motion"])
  muss(teil, "Bauteil");
// Landmarken: Projekte gehören in <main>, die Fußzeile steht danach (contentinfo).
const mainAuf = html.indexOf("<main"), mainZu = html.indexOf("</main>");
const projekte = html.indexOf('id="projekte"'), fuss = html.indexOf("<footer");
if (!(mainAuf < projekte && projekte < mainZu)) fehler.push("Projekte stehen nicht in <main>");
if (!(fuss > mainZu)) fehler.push("<footer> steht nicht nach </main>");
if (/<section[^>]*>(?:(?!<\/section>)[\s\S])*<footer/.test(html)) fehler.push("<footer> steckt in einer <section>");
// Was nur mit JavaScript eingeblendet wird, muss im Druck sichtbar sein.
if (html.includes(".js .einblenden") && !/@media print/.test(html)) fehler.push("@media print fehlt für eingeblendete Kacheln");
if (/innerHTML|eval\(|document\.write/.test(html)) fehler.push("Unsicheres DOM-Schreiben (innerHTML/eval/document.write)");

// Abschnitt „So wird geprüft“: grob, ohne Interna – die Seite ist öffentlich.
muss('id="pruefung"', "Abschnitt");
muss('<h2 id="pruefung-titel">So wird geprüft</h2>', "Überschrift");
for (const text of ["Tests zuerst", "KI-Code-Review", "Sicherheits-Review", "SonarQube", "Renovate", "bereit zum Merge"])
  muss(text, "Prüfschritt");
const pruefung = html.indexOf('id="pruefung"');
if (pruefung !== -1 && !(html.indexOf("</section>", projekte) < pruefung && pruefung < mainZu))
  fehler.push("Abschnitt Prüfung steht nicht nach den Projekten in <main>");

// Keine Interna auf der öffentlichen Seite und in der mitveröffentlichten Doku.
// Hier stehen nur allgemeine Muster – diese Datei ist selbst öffentlich.
// Konkrete Namen (Rechner, Firmen, private Repos) stehen je Zeile in
// scripts/verboten.local.txt; die Datei ist per .gitignore ausgeschlossen.
const muster = [
  /\blocalhost\b|\b[\w-]+\.local\b(?!\.)/i,                               // lokale Rechner
  /\b[a-z][\w.-]*:\d{2,5}\b/i,                                            // Rechner:Port
  /\b[A-Z][A-Z0-9_]*_(?:TOKEN|SECRET|KEY|PASSWORD)\b/,                    // Secret-Namen
  /\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b/,         // private Netze
];
let lokal = [];
try {
  lokal = readFileSync(new URL("./verboten.local.txt", import.meta.url), "utf8")
    .split("\n").map((z) => z.trim().toLowerCase()).filter((z) => z && !z.startsWith("#"));
} catch { /* fehlt auf fremden Rechnern – dann nur die Muster */ }
const oeffentlich = { "index.html": html };
for (const datei of readdirSync(new URL("../docs", import.meta.url), { recursive: true }))
  if (datei.endsWith(".md")) oeffentlich[`docs/${datei}`] = readFileSync(new URL(`../docs/${datei}`, import.meta.url), "utf8");
for (const [datei, inhalt] of Object.entries(oeffentlich)) {
  for (const m of muster) { const t = inhalt.match(m); if (t) fehler.push(`Interna in ${datei}: ${t[0]}`); }
  for (const wort of lokal) if (inhalt.toLowerCase().includes(wort)) fehler.push(`Interna in ${datei}: Eintrag aus verboten.local.txt`);
}

if (fehler.length) {
  console.error(fehler.join("\n"));
  process.exit(1);
}
console.log("index.html: alles da.");
