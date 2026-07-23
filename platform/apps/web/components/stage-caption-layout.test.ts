import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const globalCss = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

describe("television-style stage caption layout contract", () => {
  it("keeps the authoritative caption on exactly one clipped line", () => {
    expect(globalCss).toMatch(
      /\.subtitle-stage p\s*\{[^}]*overflow:\s*hidden;[^}]*white-space:\s*nowrap;[^}]*text-overflow:\s*ellipsis;/s,
    );
  });

  it("keeps mobile captions readable instead of shrinking them to footnote size", () => {
    expect(globalCss).toMatch(
      /@media\s*\(max-width:\s*760px\)[\s\S]*?\.subtitle-stage p\s*\{[^}]*font-size:\s*14px;[^}]*line-height:\s*1\.5;/s,
    );
  });
});
