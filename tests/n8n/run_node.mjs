// Runs a Stage 4 n8n Code node locally against this service's output, since n8n itself is read-only.
//
//   node tests/n8n/run_node.mjs sort     <upstream.json> <items.json>
//   node tests/n8n/run_node.mjs comments <sort-output.json> <items.json>
//
// <upstream.json> is the single JSON item the node reads through $('Parse Keywords') or
// $('Sort Reddit Results'); <items.json> is the HTTP node's output (the array this service returns).
// The .js files next to this script are verbatim copies of the node code; see README.md in this folder.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const NODES = {
  sort: { file: "sort_reddit_results.js", upstream: "Parse Keywords" },
  comments: { file: "fetch_reddit_comments.js", upstream: "Sort Reddit Results" },
};

const [mode, upstreamPath, itemsPath] = process.argv.slice(2);
const node = NODES[mode];
if (!node || !upstreamPath || !itemsPath) {
  console.error("usage: node run_node.mjs sort|comments <upstream.json> <items.json>");
  process.exit(2);
}

const upstream = JSON.parse(readFileSync(upstreamPath, "utf8"));
let items = JSON.parse(readFileSync(itemsPath, "utf8"));
// n8n splits a top-level array into one item per element; an error body arrives as a single item.
if (!Array.isArray(items)) items = [items];
// An empty array with alwaysOutputData=true reaches the next node as one empty item.
const wrapped = (items.length ? items : [{}]).map((json) => ({ json }));

const code = readFileSync(join(here, node.file), "utf8");
const $input = { first: () => wrapped[0], all: () => wrapped };
const $ = (name) => {
  if (name !== node.upstream) throw new Error(`node referenced unexpected upstream node: ${name}`);
  return { first: () => ({ json: upstream }) };
};

const fn = new Function("$input", "$", `return (async () => {\n${code}\n})();`);
const out = await fn($input, $);
process.stdout.write(JSON.stringify(out[0].json, null, 2));
