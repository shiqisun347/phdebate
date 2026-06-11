import fs from "node:fs";
import path from "node:path";

const docsDir = "docs";
const files = fs.readdirSync(docsDir).filter((file) => file.endsWith(".md"));
const failures = [];

for (const file of files) {
  const fullPath = path.join(docsDir, file);
  const text = fs.readFileSync(fullPath, "utf8");

  const linkRe = /\[[^\]]+\]\(([^)]+)\)/g;
  for (const match of text.matchAll(linkRe)) {
    const href = match[1];
    if (/^(https?:|#)/.test(href)) continue;
    const target = href.split("#")[0];
    if (!target) continue;
    const resolved = path.normalize(path.join(path.dirname(fullPath), target));
    if (!fs.existsSync(resolved)) failures.push(`${fullPath}: missing link ${href}`);
  }

  const jsonRe = /```json\n([\s\S]*?)```/g;
  for (const match of text.matchAll(jsonRe)) {
    try {
      JSON.parse(match[1]);
    } catch (error) {
      const line = text.slice(0, match.index).split("\n").length;
      failures.push(`${fullPath}:${line}: invalid JSON fence: ${error.message}`);
    }
  }
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log("Docs links and JSON fences are valid.");

