#!/usr/bin/env node
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MARKER_START = '<!-- BEGIN GENERATED TOOL INVENTORY -->';
const MARKER_END = '<!-- END GENERATED TOOL INVENTORY -->';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const serverRoot = path.resolve(scriptDir, '..');
const sourcePath = path.join(serverRoot, 'src', 'index.ts');

function findMatchingBracket(source, startIndex) {
  let depth = 0;
  let quote = null;
  let escaped = false;
  let lineComment = false;
  let blockComment = false;

  for (let i = startIndex; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];

    if (lineComment) {
      if (ch === '\n') lineComment = false;
      continue;
    }

    if (blockComment) {
      if (ch === '*' && next === '/') {
        blockComment = false;
        i += 1;
      }
      continue;
    }

    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (ch === '\\') {
        escaped = true;
      } else if (ch === quote) {
        quote = null;
      }
      continue;
    }

    if (ch === '/' && next === '/') {
      lineComment = true;
      i += 1;
      continue;
    }

    if (ch === '/' && next === '*') {
      blockComment = true;
      i += 1;
      continue;
    }

    if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch;
      continue;
    }

    if (ch === '[') depth += 1;
    if (ch === ']') {
      depth -= 1;
      if (depth === 0) return i;
    }
  }

  throw new Error('Could not find the end of the TOOLS array');
}

function extractToolsArray(source) {
  const declaration = source.indexOf('const TOOLS = [');
  if (declaration === -1) {
    throw new Error('Could not find `const TOOLS = [` in src/index.ts');
  }

  const start = source.indexOf('[', declaration);
  const end = findMatchingBracket(source, start);
  return source.slice(start, end + 1);
}

function loadTools(arraySource) {
  const jsSource = arraySource.replace(/\bas const\b/g, '');
  const tools = Function(`"use strict"; return (${jsSource});`)();
  if (!Array.isArray(tools)) {
    throw new Error('TOOLS did not evaluate to an array');
  }
  return tools;
}

function oneLine(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function replacementFor(description) {
  const match = description.match(/use `([^`]+)` instead/i);
  return match ? match[1] : '';
}

function renderInventory(tools) {
  const rows = tools.map((tool) => {
    const description = oneLine(tool.description);
    return {
      name: String(tool.name),
      description,
      deprecated: /^DEPRECATED\b/i.test(description),
      replacement: replacementFor(description),
    };
  });

  const visible = rows.filter((row) => !row.deprecated);
  const deprecated = rows.filter((row) => row.deprecated);
  const lines = [
    MARKER_START,
    '## Tool inventory',
    '',
    '_Generated from `src/index.ts` with `node scripts/gen-tool-docs.mjs --write README.md`._',
    '',
    `ListTools exposes ${visible.length} tools. Deprecated aliases remain callable for compatibility but are hidden from ListTools.`,
    '',
    '| Tool | Deprecated | Description |',
    '|------|------------|-------------|',
  ];

  for (const row of visible) {
    lines.push(`| \`${row.name}\` | No | ${row.description} |`);
  }

  if (deprecated.length > 0) {
    lines.push('', '### Hidden deprecated aliases', '', '| Alias | Replacement | Description |', '|-------|-------------|-------------|');
    for (const row of deprecated) {
      lines.push(`| \`${row.name}\` | \`${row.replacement || 'n/a'}\` | ${row.description} |`);
    }
  }

  lines.push(MARKER_END);
  return lines.join('\n');
}

async function main() {
  const source = await readFile(sourcePath, 'utf8');
  const tools = loadTools(extractToolsArray(source));
  const section = renderInventory(tools);
  const args = process.argv.slice(2);

  if (args[0] === '--write') {
    const readmePath = path.resolve(serverRoot, args[1] || 'README.md');
    const readme = await readFile(readmePath, 'utf8');
    const start = readme.indexOf(MARKER_START);
    const end = readme.indexOf(MARKER_END);
    if (start === -1 || end === -1 || end < start) {
      throw new Error(`README is missing ${MARKER_START}/${MARKER_END} markers`);
    }
    const nextReadme = `${readme.slice(0, start)}${section}${readme.slice(end + MARKER_END.length)}`;
    await writeFile(readmePath, nextReadme);
    return;
  }

  console.log(section);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
