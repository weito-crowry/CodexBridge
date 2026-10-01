import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const output = join(import.meta.dirname, "dist");
let html = await readFile(join(output, "index.html"), "utf8");
for (const match of [...html.matchAll(/<script([^>]*?)src="([^"]+\.js)"([^>]*)><\/script>/g)]) {
  const [, before, source, after] = match;
  const script = await readFile(join(output, source.replace(/^\.\//, "")), "utf8");
  const replacement = `<script${before}${after}>${script}</script>`;
  html = html.replace(match[0], () => replacement);
}
for (const match of [...html.matchAll(/<link[^>]*href="([^"]+\.css)"[^>]*>/g)]) {
  const source = match[1];
  const css = await readFile(join(output, source.replace(/^\.\//, "")), "utf8");
  const replacement = `<style>${css}</style>`;
  html = html.replace(match[0], () => replacement);
}
html = html.replace(/<link rel="modulepreload"[^>]*>/g, "");
html = `${html.replace(/\r\n?/g, "\n").replace(/\n+$/, "")}\n`;
const destination = join(import.meta.dirname, "..", "..", "src", "codex_bridge", "assets", "codexbridge_setup_app.html");
await writeFile(destination, html, "utf8");
