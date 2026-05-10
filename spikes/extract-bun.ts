/**
 * Spike: pdf-parse v2 (Bun) PDF extraction for D&D Basic Rules PDF.
 *
 * Usage:
 *   bun add pdf-parse
 *   bun run spikes/extract-bun.ts <path-to-pdf> [--out=output.jsonl]
 *
 * Outputs one JSON line per page-block (same schema as pdfplumber spike):
 *   { page, block_index, heading_level, heading_text, raw_text, tables }
 *
 * Key finding: pdf-parse v2 handles 2-column D&D layout natively (no column
 * splitting code needed). Table extraction detects table regions but returns
 * empty cells for visual/text-column tables (D&D PDFs use text-column layout,
 * not structural PDF tables). See dnd-extraction-spike.md for comparison.
 */

import { readFile, writeFile } from "fs/promises";
import { resolve } from "path";
import { PDFParse } from "pdf-parse";

const CHAPTER_RE = /^(chapter\s+\d+[:.–—]|part\s+\d+\s*[—–-]|appendix\s+[a-z][:.–—]|\d+th-level|\d+nd-level|\d+rd-level|\d+st-level)/i;
const SPELL_NAME_RE = /^[A-Z][a-zA-Z'\s-]{2,40}$(?:\n[0-9]+(?:st|nd|rd|th)-level)?/m;

interface Block {
  page: number;
  block_index: number;
  heading_level: 1 | 2 | null;
  heading_text: string | null;
  raw_text: string;
  tables: string[][][];
}

function detectHeading(text: string): [1 | 2 | null, string | null] {
  const firstLine = text.trim().split("\n")[0].trim();
  if (CHAPTER_RE.test(firstLine)) return [1, firstLine];
  // Spell names: Title Case line followed by "Nth-level school" or "cantrip"
  const lines = text.trim().split("\n");
  if (
    lines.length >= 2 &&
    /^[A-Z][a-zA-Z'\s\-/]{2,40}$/.test(lines[0].trim()) &&
    /^(cantrip|[0-9]+(?:st|nd|rd|th)-level)/i.test(lines[1]?.trim() ?? "")
  ) {
    return [2, lines[0].trim()];
  }
  return [null, null];
}

async function extract(pdfPath: string): Promise<Block[]> {
  const buffer = await readFile(pdfPath);
  const parser = new PDFParse({ data: buffer });

  // Get all pages at once — result.pages[] has { text, num } per page
  const textResult = await parser.getText();
  const pages = (textResult as { pages: Array<{ text: string; num: number }> }).pages;
  await parser.destroy();

  const results: Block[] = [];

  for (const page of pages) {
    const pageNum = page.num;
    const text = page.text?.trim() ?? "";
    if (!text) continue;

    // Strip the page header line (format: "84\nD&D Player's Basic Rules v0.2 | Chapter N: Title\n")
    const cleaned = text.replace(/^\d+\nD&D Player's Basic Rules[^\n]*\n/, "").trim();

    const rawBlocks = cleaned.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);

    for (let i = 0; i < rawBlocks.length; i++) {
      const [level, headingText] = detectHeading(rawBlocks[i]);
      results.push({
        page: pageNum,
        block_index: i,
        heading_level: level,
        heading_text: headingText,
        raw_text: rawBlocks[i],
        tables: [], // D&D tables are visual layout, not structural PDF tables — captured in raw_text
      });
    }
  }

  return results;
}

async function main() {
  const args = process.argv.slice(2);
  const pdfArg = args.find((a) => !a.startsWith("--"));
  const outArg = args.find((a) => a.startsWith("--out="))?.split("=")[1] ?? "raw-extract-bun.jsonl";

  if (!pdfArg) {
    console.error("Usage: bun run spikes/extract-bun.ts <path-to-pdf> [--out=output.jsonl]");
    process.exit(1);
  }

  const pdfPath = resolve(pdfArg);
  console.log(`Extracting: ${pdfPath}`);

  const blocks = await extract(pdfPath);
  console.log(`Extracted ${blocks.length} blocks from ${blocks.at(-1)?.page ?? 0} pages`);

  const lines = blocks.map((b) => JSON.stringify(b)).join("\n") + "\n";
  await writeFile(outArg, lines, "utf-8");
  console.log(`Written to: ${outArg}`);

  console.log("\n=== SPOT CHECK: Chapters (heading_level=1) ===");
  blocks.filter((b) => b.heading_level === 1).slice(0, 15).forEach((c) =>
    console.log(`  p${String(c.page).padStart(3, "0")}: ${c.heading_text}`)
  );

  console.log("\n=== SPOT CHECK: Detected spell names (heading_level=2) ===");
  blocks.filter((b) => b.heading_level === 2).slice(0, 15).forEach((b) =>
    console.log(`  p${String(b.page).padStart(3, "0")}: ${b.heading_text}`)
  );

  console.log("\n=== SPOT CHECK: Augury spell block ===");
  blocks.filter((b) => b.raw_text.includes("Augury")).slice(0, 2).forEach((b) =>
    console.log(`  p${String(b.page).padStart(3, "0")} block ${b.block_index}:\n${b.raw_text.slice(0, 400)}`)
  );

  console.log(`\nTotal blocks: ${blocks.length}`);
  console.log(`Blocks with heading_level=1: ${blocks.filter((b) => b.heading_level === 1).length}`);
  console.log(`Blocks with heading_level=2 (spell names): ${blocks.filter((b) => b.heading_level === 2).length}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
