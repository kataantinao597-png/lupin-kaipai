// V3.1 §3.3：单段重生成隔离性测试——验证 shipped 的 app.js 中 mergeRegenSegment
// 纯函数的真实行为：只替换指定段，其他段落绝不被触碰。
import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const src = readFileSync(path.join(root, "frontend/js/app.js"), "utf-8");
const start = src.indexOf("function mergeRegenSegment(");
const end = src.indexOf("/* V3.1-TEST-MARKER: mergeRegenSegment end */");
assert.ok(start !== -1 && end !== -1 && end > start, "未在 app.js 中找到 mergeRegenSegment");
const fnSrc = src.slice(start, end);
const mergeRegenSegment = new Function(fnSrc + "\nreturn mergeRegenSegment;")();

const segs = [
  { phase: "开场留人", start_minute: 0, end_minute: 5, script: ["A"], tips: [] },
  { phase: "讲品", start_minute: 5, end_minute: 25, script: ["B"], tips: ["t"] },
  { phase: "互动", start_minute: 25, end_minute: 35, script: ["C"], tips: [] },
];

// 1. 只替换第 1 段，其他段原样不动
const out = mergeRegenSegment(segs, 1, { phase: "讲品", start_minute: 5, end_minute: 25, script: ["B2"], tips: [] });
assert.equal(out.length, 3);
assert.deepEqual(out[1].script, ["B2"]);
assert.deepEqual(out[0], segs[0], "第 0 段被意外改动");
assert.deepEqual(out[2], segs[2], "第 2 段被意外改动");
assert.deepEqual(segs[1].script, ["B"], "原数组被污染");

// 2. 时间轴不一致 → 抛错拒绝写入
assert.throws(
  () => mergeRegenSegment(segs, 1, { phase: "讲品", start_minute: 5, end_minute: 30, script: ["X"], tips: [] }),
  /时间轴/,
);

// 3. 越界索引 → 抛错
assert.throws(() => mergeRegenSegment(segs, 9, { start_minute: 0, end_minute: 1 }), /越界/);

// 4. locked 等原段字段被保留（合并语义）
const out4 = mergeRegenSegment(
  [{ phase: "讲品", start_minute: 5, end_minute: 25, script: ["B"], locked: true }],
  0, { phase: "讲品", start_minute: 5, end_minute: 25, script: ["B2"], tips: [] });
assert.equal(out4[0].locked, true, "原段 locked 标记丢失");

console.log("mergeRegenSegment: 4/4 passed");
