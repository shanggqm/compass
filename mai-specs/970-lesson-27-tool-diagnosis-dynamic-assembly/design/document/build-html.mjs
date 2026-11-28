import { mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { applyCourseReader } from "../../../../scripts/course-design/reader.mjs";
import { renderReview } from "../../../../.agents/skill/my-design-to-html/scripts/render.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Compute exact line-level LCS diff between beforeText and afterText.
 * Returns { delSet: Set<1-based lineNumber>, addSet: Set<1-based lineNumber> }.
 */
function computeLineDiff(beforeText, afterText) {
  const norm = (line) => line.trim().replace(/,$/, "");
  const a = beforeText.replace(/\n$/, "").split("\n");
  const b = afterText.replace(/\n$/, "").split("\n");
  const an = a.map(norm);
  const bn = b.map(norm);
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    const row = dp[i];
    const nextRow = dp[i + 1];
    const ai = an[i];
    for (let j = m - 1; j >= 0; j--) {
      if (ai === bn[j]) {
        row[j] = nextRow[j + 1] + 1;
      } else {
        row[j] = nextRow[j] >= row[j + 1] ? nextRow[j] : row[j + 1];
      }
    }
  }
  const delSet = new Set();
  const addSet = new Set();
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (an[i] === bn[j]) {
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      delSet.add(i + 1);
      i++;
    } else {
      addSet.add(j + 1);
      j++;
    }
  }
  while (i < n) {
    delSet.add(i + 1);
    i++;
  }
  while (j < m) {
    addSet.add(j + 1);
    j++;
  }
  return { delSet, addSet };
}

function measureWireBytes(codeText) {
  try {
    const parsed = JSON.parse(codeText);
    if (!parsed || parsed._status === "not_in_baseline_catalog") return 0;
    const copy = structuredClone(parsed);
    const examples = copy.examples;
    delete copy.examples;
    if (Array.isArray(examples) && examples.length > 0 && copy.function) {
      copy.function.description += `\n调用示例：\n${JSON.stringify(examples)}`;
    }
    return Buffer.byteLength(JSON.stringify(copy), "utf8");
  } catch {
    return Buffer.byteLength(codeText, "utf8");
  }
}

function decorateCodeLines(codeBlockHtml, changedSet, diffClass) {
  let lineNo = 0;
  return codeBlockHtml.replace(/<span class="code-line">/g, () => {
    lineNo += 1;
    return changedSet.has(lineNo)
      ? `<span class="code-line ${diffClass}">`
      : '<span class="code-line">';
  });
}

const DIFF_STYLE = `
<style id="schema-diff-styles">
  .schema-diff-pair {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px;
    align-items: stretch;
    margin: 18px 0 34px;
  }
  .schema-diff-pair > .code-block {
    margin: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    background: #14251e;
    border-color: #2b473b;
  }
  .schema-diff-pair > .code-block pre {
    flex: 1;
    max-height: 480px;
    overflow-x: hidden;
    overflow-y: auto;
    font-size: 12.5px;
    line-height: 1.72;
    padding: 14px 0;
  }
  .schema-diff-pair > .code-block.expanded pre {
    max-height: none;
  }
  .schema-diff-pair > .code-block pre > code {
    display: block;
    min-width: 0;
    width: 100%;
  }
  .schema-diff-pair .code-bar {
    flex-wrap: wrap;
    gap: 6px 10px;
    padding: 8px 12px;
  }
  .schema-diff-pair .code-line {
    display: grid;
    grid-template-columns: 3.2ch minmax(0, 1fr);
    column-gap: 10px;
    align-items: start;
    padding: 1px 14px 1px 8px;
  }
  .schema-diff-pair .line-num {
    width: auto;
    margin: 0;
  }
  .schema-diff-pair .line-content {
    display: block;
    position: relative;
    padding-left: 2ch;
    min-width: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .schema-diff-badge {
    display: inline-block;
    padding: 2px 7px;
    margin-right: 8px;
    border-radius: 4px;
    font: 600 10.5px/1.4 var(--mono);
    letter-spacing: 0.03em;
    vertical-align: 1px;
  }
  .schema-diff-badge--before {
    background: rgba(235, 106, 98, 0.2);
    border: 1px solid rgba(235, 106, 98, 0.48);
    color: #ffb8b2;
  }
  .schema-diff-badge--after {
    background: rgba(88, 201, 130, 0.22);
    border: 1px solid rgba(88, 201, 130, 0.52);
    color: #b5f3ca;
  }
  .schema-diff-stat {
    display: inline-block;
    margin-left: 8px;
    font: 500 11px/1.4 var(--mono);
    opacity: 0.92;
  }
  .schema-diff-stat--del {
    color: #ffaba3;
  }
  .schema-diff-stat--add {
    color: #9ce8b6;
  }
  .code-line.diff-del {
    background: rgba(229, 83, 75, 0.17);
    box-shadow: inset 3px 0 0 #e5534b;
  }
  .code-line.diff-add {
    background: rgba(63, 185, 80, 0.18);
    box-shadow: inset 3px 0 0 #3fb950;
  }
  .code-line.diff-del .line-num {
    color: #ffaba3;
  }
  .code-line.diff-add .line-num {
    color: #8ce5a6;
  }
  .code-line.diff-del .line-content::before {
    content: "-";
    position: absolute;
    left: 0;
    top: 0;
    color: #ff948a;
    user-select: none;
    font-weight: 700;
  }
  .code-line.diff-add .line-content::before {
    content: "+";
    position: absolute;
    left: 0;
    top: 0;
    color: #7ee787;
    user-select: none;
    font-weight: 700;
  }
  @media (max-width: 720px) {
    .schema-diff-pair {
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
    }
  }
  @media print {
    .schema-diff-pair {
      grid-template-columns: minmax(0, 1fr);
    }
    .schema-diff-pair > .code-block {
      background: #f4f5f2;
      border-color: #ccc;
    }
    .code-line.diff-del {
      background: #fdeceb;
    }
    .code-line.diff-add {
      background: #eaf8ee;
    }
  }
</style>`;

const DIFF_SYNC_SCRIPT = `
<script>
(() => {
  document.querySelectorAll(".schema-diff-pair").forEach((pair) => {
    const blocks = [...pair.querySelectorAll(".code-block")];
    if (blocks.length !== 2) return;
    pair.querySelectorAll("[data-expand]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const current = btn.closest(".code-block");
        const isExpanded = current.classList.contains("expanded");
        blocks.forEach((b) => {
          b.classList.toggle("expanded", isExpanded);
          const otherBtn = b.querySelector("[data-expand]");
          if (otherBtn) {
            otherBtn.textContent = isExpanded ? "收起代码" : "展开代码";
            otherBtn.setAttribute("aria-expanded", String(isExpanded));
          }
        });
      });
    });
  });
})();
</script>`;

function transformSideBySideDiffs(html) {
  const payloadMatch = html.match(
    /<script type="application\/json" id="document-data">([\s\S]*?)<\/script>/u,
  );
  if (!payloadMatch) throw new Error("Missing #document-data JSON payload");
  const { codes } = JSON.parse(payloadMatch[1]);

  const pairPattern =
    /(<div class="code-block" id="code-(\d+)">\s*<div class="code-bar"><span>\[BEFORE\] ([\s\S]*?)<\/span>[\s\S]*?<\/pre><\/div>)\s*(<div class="code-block" id="code-(\d+)">\s*<div class="code-bar"><span>\[AFTER\] ([\s\S]*?)<\/span>[\s\S]*?<\/pre><\/div>)/gu;

  let pairCount = 0;
  const transformed = html.replace(
    pairPattern,
    (_, beforeBlock, beforeIdxStr, beforeLabel, afterBlock, afterIdxStr, afterLabel) => {
      pairCount += 1;
      const beforeIdx = Number(beforeIdxStr);
      const afterIdx = Number(afterIdxStr);
      const beforeRaw = codes[beforeIdx] ?? "";
      const afterRaw = codes[afterIdx] ?? "";
      const { delSet, addSet } = computeLineDiff(beforeRaw, afterRaw);
      const bBytes = measureWireBytes(beforeRaw);
      const aBytes = measureWireBytes(afterRaw);
      const bTok = bBytes ? Math.ceil(bBytes / 4) : 0;
      const aTok = Math.ceil(aBytes / 4);
      const beforeStat =
        bBytes > 0
          ? `${bBytes.toLocaleString()} B (~${bTok} tok) · −${delSet.size} 行`
          : `基线未内置 · −${delSet.size} 行`;
      const pct =
        bBytes > 0 ? Math.round(((bBytes - aBytes) / bBytes) * 100) : 0;
      const pctText = pct >= 0 ? `−${pct}%` : `+${Math.abs(pct)}%`;
      const afterStat =
        bBytes > 0
          ? `${aBytes.toLocaleString()} B (~${aTok} tok · ${pctText}) · +${addSet.size} 行`
          : `${aBytes.toLocaleString()} B (~${aTok} tok · 新增) · +${addSet.size} 行`;

      const decoratedBefore = decorateCodeLines(
        beforeBlock.replace(
          `<span>[BEFORE] ${beforeLabel}</span>`,
          `<span><strong class="schema-diff-badge schema-diff-badge--before">BEFORE · 原 Schema</strong>${beforeLabel}<em class="schema-diff-stat schema-diff-stat--del">${beforeStat}</em></span>`,
        ),
        delSet,
        "diff-del",
      );

      const decoratedAfter = decorateCodeLines(
        afterBlock.replace(
          `<span>[AFTER] ${afterLabel}</span>`,
          `<span><strong class="schema-diff-badge schema-diff-badge--after">AFTER · 极简契约</strong>${afterLabel}<em class="schema-diff-stat schema-diff-stat--add">${afterStat}</em></span>`,
        ),
        addSet,
        "diff-add",
      );

      return `<div class="schema-diff-pair" role="group" aria-label="工具 Schema 改造前后对比">${decoratedBefore}${decoratedAfter}</div>`;
    },
  );

  if (pairCount === 0) return html;
  return transformed
    .replace("</head>", () => `${DIFF_STYLE}</head>`)
    .replace("</body>", () => `${DIFF_SYNC_SCRIPT}</body>`);
}

export async function buildEdition(configFile) {
  const resolvedConfig = path.resolve(configFile);
  const config = JSON.parse(await readFile(resolvedConfig, "utf8"));
  const directory = path.dirname(resolvedConfig);
  const dest = path.resolve(directory, config.output);
  const scratch = await mkdtemp(path.join(os.tmpdir(), "compass-l27-reader-"));
  try {
    const temporary = path.join(scratch, "reader.html");
    await renderReview(resolvedConfig, { output: temporary });
    let html = await applyCourseReader(await readFile(temporary, "utf8"), {
      lesson: config.lesson,
      title: config.title,
      subtitle: config.subtitle ?? "",
      cover:
        config.cover && {
          ...config.cover,
          file: path.resolve(directory, config.cover.file),
        },
    });
    html = transformSideBySideDiffs(html);
    const staged = `${dest}.${process.pid}.tmp`;
    try {
      await writeFile(staged, html, { flag: "wx" });
      await rename(staged, dest);
    } finally {
      await rm(staged, { force: true });
    }
    return dest;
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
}

if (
  process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
) {
  if (process.argv.length !== 3) {
    throw new Error("Usage: node build-html.mjs path/to/edition.json");
  }
  console.log(await buildEdition(process.argv[2]));
}
