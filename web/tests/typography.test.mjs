import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PAGES = ["editorial-atlas.html", "admin.html"];
const TOKENS = readFileSync(path.join(WEB_DIR, "typography.css"), "utf8");

function extractCss(html) {
  return [...html.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");
}

test("字号只使用 --fs-* 令牌：最小 12px 且全部为偶数", () => {
  const defs = [...TOKENS.matchAll(/--fs-(\d+):(\d+)px/g)];
  assert.ok(defs.length >= 10, "typography.css 应定义完整的字号令牌");
  const allowed = new Set();
  for (const [, name, value] of defs) {
    const size = Number(value);
    assert.equal(Number(name), size, `令牌 --fs-${name} 与取值 ${value} 不一致`);
    assert.ok(size >= 12, `--fs-${name} 小于 12px`);
    assert.equal(size % 2, 0, `--fs-${name} 不是偶数`);
    allowed.add(`--fs-${name}`);
  }
  for (const page of PAGES) {
    const css = extractCss(readFileSync(path.join(WEB_DIR, page), "utf8"));
    const literal = [...css.matchAll(/font-size:(?!clamp\()([^;}\s]+)/g)].map((m) => m[1]);
    assert.ok(literal.length > 0, `${page} 未找到 font-size 声明`);
    for (const value of literal) {
      assert.match(value, /^var\(--fs-\d+\)$/, `${page} 存在非令牌字号：${value}`);
      assert.ok(allowed.has(value.slice(4, -1)), `${page} 使用了未定义令牌：${value}`);
    }
    for (const m of css.matchAll(/font-size:clamp\(([^)]*)\)/g)) {
      for (const px of m[1].match(/\d+px/g) || []) {
        const size = parseInt(px, 10);
        assert.ok(size >= 12 && size % 2 === 0, `${page} clamp 端点不合法：${px}`);
      }
    }
  }
});

test("字体族只使用共享令牌，中文与拉丁由 unicode-range 分工", () => {
  assert.match(TOKENS, /--font-stack:var\(--font-latin\),var\(--font-cjk\),serif/);
  assert.match(TOKENS, /font-family:"MA CJK"/);
  assert.match(TOKENS, /unicode-range:/);
  for (const page of PAGES) {
    const css = extractCss(readFileSync(path.join(WEB_DIR, page), "utf8"));
    const families = [...css.matchAll(/font-family:([^;}]+)/g)].map((m) => m[1].trim());
    assert.ok(families.length > 0, `${page} 未找到 font-family 声明`);
    for (const value of families) {
      assert.match(value, /^var\(--(font-stack|serif|mono|sans)\)$/, `${page} 存在未收编的字体族：${value}`);
    }
  }
});
