/*
 * Markdown, rendered small and safely.
 *
 * The agent's `<display>` channel is Markdown by contract — the prompt tells it to
 * put tables, code, file listings, long numbers and paths there — and it was being
 * shown as preformatted text, so a table arrived as a row of pipes and bold arrived
 * with its asterisks. This renders it.
 *
 * Two rules, both deliberate:
 *
 * 1. **Nothing is trusted.** The text comes from a model that has been reading
 *    files and web pages, so it can contain anything those contained. Every piece
 *    of text is escaped, and the only markup that reaches the page is markup this
 *    file builds itself. There is no raw-HTML passthrough, and a link is only a
 *    link if its scheme is http or https.
 * 2. **It is a subset, not a specification.** Headings, paragraphs, lists, tables,
 *    fences, blockquotes, rules, and inline code/bold/italic/links. No nested lists,
 *    no reference links, no footnotes, no HTML. A construct this does not know is
 *    shown as the text it is, which is the failure that costs least.
 *
 * It is a plain string function with no DOM, so its escaping can be tested directly
 * rather than by looking at a page.
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** The text, with everything HTML would read as markup made inert. */
function escapeText(text) {
  return String(text).replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

/** A link target worth having, or null when it is not one this page will follow. */
function safeHref(url) {
  const trimmed = String(url).trim().replace(/[\u0000-\u001f\u007f]/g, "");
  if (!/^https?:\/\//i.test(trimmed)) return null;
  // A URL with quotes in it would break out of the attribute; escaping handles the
  // value, but refusing is clearer than emitting a link nobody meant.
  if (/["'<>]/.test(trimmed)) return null;
  return trimmed;
}

// One pass, one alternative per inline construct. Code spans come first so that
// `**this**` inside backticks stays literal, and links before emphasis so that a
// bracketed label with asterisks in it does not lose them to the wrong rule.
const INLINE =
  /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(__[^_\n]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]\n]+\]\([^()\s]+\))/;

/** Inline markup, with everything that is not markup escaped on the way out. */
function inline(raw) {
  let out = "";
  let rest = String(raw);
  for (;;) {
    const match = INLINE.exec(rest);
    if (!match) {
      out += escapeText(rest);
      break;
    }
    out += escapeText(rest.slice(0, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      out += `<code>${escapeText(token.slice(1, -1))}</code>`;
    } else if (token.startsWith("**") || token.startsWith("__")) {
      out += `<strong>${inline(token.slice(2, -2))}</strong>`;
    } else if (token.startsWith("*") || token.startsWith("_")) {
      out += `<em>${inline(token.slice(1, -1))}</em>`;
    } else {
      const link = /^\[([^\]]+)\]\(([^()\s]+)\)$/.exec(token);
      const href = link ? safeHref(link[2]) : null;
      out += href
        ? `<a href="${escapeText(href)}" target="_blank" rel="noopener noreferrer">${inline(link[1])}</a>`
        : escapeText(token);
    }
    rest = rest.slice(match.index + token.length);
  }
  return out;
}

const FENCE = /^\s*(`{3,}|~{3,})\s*[\w+#.-]*\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/;
const RULE = /^\s{0,3}([-*_])[ \t]*(\1[ \t]*){2,}$/;
const BULLET = /^\s{0,3}[-*+]\s+/;
const NUMBERED = /^\s{0,3}\d+[.)]\s+/;

function isTableRow(line) {
  const trimmed = line.trim();
  return trimmed.startsWith("|") && trimmed.indexOf("|", 1) !== -1;
}

function isSeparatorRow(line) {
  if (!isTableRow(line)) return false;
  const cells = splitCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell.replace(/\s/g, "")));
}

function splitCells(line) {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => cell.trim());
}

function table(lines, start) {
  const header = splitCells(lines[start]);
  let next = start + 1;
  let hasSeparator = false;
  if (next < lines.length && isSeparatorRow(lines[next])) {
    hasSeparator = true;
    next += 1;
  }
  const rows = [];
  while (next < lines.length && isTableRow(lines[next])) {
    rows.push(splitCells(lines[next]));
    next += 1;
  }
  const cells = (tag, values) =>
    values.map((value) => `<${tag}>${inline(value)}</${tag}>`).join("");
  const head = `<tr>${cells("th", header)}</tr>`;
  const body = rows.map((row) => `<tr>${cells("td", row)}</tr>`).join("");
  // A "table" of one row with no separator under it is far more likely to be text
  // that happens to contain a pipe, so it is left to the paragraph path.
  if (!hasSeparator && !rows.length) return null;
  return { html: `<table class="md__table"><thead>${head}</thead><tbody>${body}</tbody></table>`, end: next };
}

/** Render Markdown to an HTML string. Every tag in the result is built here. */
export function markdownToHtml(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1][0].repeat(3);
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^\\s*${marker}`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence
      out.push(`<pre class="md__code"><code>${escapeText(body.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level} class="md__h">${inline(heading[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      out.push('<hr class="md__hr" />');
      i += 1;
      continue;
    }

    if (isTableRow(line) && i + 1 < lines.length && (isSeparatorRow(lines[i + 1]) || isTableRow(lines[i + 1]))) {
      const rendered = table(lines, i);
      if (rendered) {
        out.push(rendered.html);
        i = rendered.end;
        continue;
      }
    }

    if (/^\s{0,3}>\s?/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote class="md__quote">${inline(quoted.join("\n"))}</blockquote>`);
      continue;
    }

    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = NUMBERED.test(line);
      const items = [];
      while (i < lines.length && (ordered ? NUMBERED.test(lines[i]) : BULLET.test(lines[i]))) {
        const item = [lines[i].replace(ordered ? NUMBERED : BULLET, "")];
        i += 1;
        // A continuation is an indented line that is not itself a new item.
        while (
          i < lines.length &&
          lines[i].trim() &&
          /^\s{2,}/.test(lines[i]) &&
          !BULLET.test(lines[i]) &&
          !NUMBERED.test(lines[i])
        ) {
          item.push(lines[i].trim());
          i += 1;
        }
        items.push(`<li>${inline(item.join("\n"))}</li>`);
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag} class="md__list">${items.join("")}</${tag}>`);
      continue;
    }

    // A paragraph: consecutive ordinary lines. The line breaks are kept, because
    // half of what lands here is a listing where the breaks are the structure.
    const paragraph = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      !RULE.test(lines[i]) &&
      !isTableRow(lines[i]) &&
      !/^\s{0,3}>\s?/.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i])
    ) {
      paragraph.push(lines[i]);
      i += 1;
    }
    out.push(`<p class="md__p">${inline(paragraph.join("\n")).replace(/\n/g, "<br />")}</p>`);
  }

  return out.join("");
}
