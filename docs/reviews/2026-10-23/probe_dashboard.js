// 执行生产页面脚本；DOM、麦克风、音频设备与 HTTP 均为测试替身。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const repo = process.env.REVIEW_REPO || path.resolve(__dirname, "../../..");
const html = fs.readFileSync(path.join(repo, "src/jarvis_gateway/web/dashboard.html"), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const results = [];
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
};

function context(options = {}) {
  const elements = {}, events = {}, permissions = [], tracks = [], contexts = [], uploads = [];
  const timers = new Map();
  let timerID = 0;
  function element(id) {
    if (elements[id]) return elements[id];
    const values = new Set();
    return elements[id] = {
      id, textContent: "", className: "", value: "", children: [], handlers: {},
      classList: {
        add: x => values.add(x), remove: x => values.delete(x),
        toggle(x, enabled) {
          if (enabled === undefined) enabled = !values.has(x);
          if (enabled) values.add(x); else values.delete(x);
          return enabled;
        },
        contains: x => values.has(x),
      },
      addEventListener(name, cb) { this.handlers[name] = cb; },
      appendChild(x) { this.children.push(x); },
    };
  }
  const node = () => ({ connect() {}, disconnect() {} });
  class AudioContext {
    constructor() {
      this.sampleRate = 48000;
      this.destination = {};
      this.closed = false;
      this.resuming = deferred();
      contexts.push(this);
    }
    resume() { return options.manualResume ? this.resuming.promise : Promise.resolve(); }
    createMediaStreamSource() { return node(); }
    createScriptProcessor() { return this.processor = node(); }
    createGain() { return { ...node(), gain: {} }; }
    createBuffer(channels, count, rate) {
      this.playbackRate = rate;
      return { getChannelData: () => new Float32Array(count) };
    }
    createBufferSource() {
      return { ...node(), start() { setImmediate(() => this.onended()); } };
    }
    close() { this.closed = true; return Promise.resolve(); }
  }
  const sandbox = {
    document: {
      getElementById: element, querySelectorAll: () => [], createElement: element,
      hidden: false, addEventListener: (name, cb) => { events["document:" + name] = cb; },
    },
    localStorage: { getItem: () => "", setItem() {}, removeItem() {} },
    navigator: { mediaDevices: { getUserMedia() {
      const pending = deferred();
      permissions.push({
        reject: pending.reject,
        grant() {
          const track = { stopped: false, stop() { this.stopped = true; } };
          tracks.push(track);
          pending.resolve({ getTracks: () => [track] });
        },
      });
      return pending.promise;
    } } },
    fetch: async (url, opts) => {
      if (!url.startsWith("/chat")) {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      uploads.push(opts);
      if (options.pendingUpload) await options.pendingUpload.promise;
      const meta = Buffer.from(JSON.stringify(options.meta || {
        typed: "模拟文本", reply: "已键入", mode: "raw", kind: "ok", speak: false,
      }));
      const wire = Buffer.alloc(4 + meta.length + 3200);
      wire.writeUInt32BE(meta.length);
      meta.copy(wire, 4);
      return {
        ok: true, status: 200,
        arrayBuffer: async () => wire.buffer.slice(wire.byteOffset, wire.byteOffset + wire.byteLength),
      };
    },
    setInterval() {},
    setTimeout(cb, ms) { timers.set(++timerID, { cb, ms }); return timerID; },
    clearTimeout(id) { timers.delete(id); },
    Float32Array, Int16Array, Uint8Array, DataView, TextDecoder, Date, Math, console,
    window: { AudioContext, addEventListener: (name, cb) => { events[name] = cb; } },
  };
  vm.runInNewContext(source, sandbox);
  return {
    elements, events, permissions, tracks, contexts, uploads, timers,
    start: () => elements["btn-mic"].handlers.mousedown(),
    release: () => events.mouseup(),
    samples(count = 4800) {
      contexts[0].processor.onaudioprocess({
        inputBuffer: { getChannelData: () => new Float32Array(count) },
      });
    },
    recording: () => elements["btn-mic"].classList.contains("recording"),
  };
}

async function main() {
  let c = context();
  let start = c.start();
  c.release();
  c.permissions[0].grant();
  await start;
  assert.equal(c.recording(), false);
  assert.equal(c.tracks[0].stopped, true);
  results.push({ probe: "release_before_permission_granted", passed: true });

  c = context();
  const key = () => c.events.keydown({
    code: "Space", target: { tagName: "BODY" }, preventDefault() {},
  });
  key(); key(); key();
  assert.equal(c.permissions.length, 1);
  c.permissions[0].grant();
  await tick();
  assert.equal(c.recording(), true); // 确认测试替身确实能检测到 recording 样式。
  await c.events.keyup({ code: "Space", preventDefault() {} });
  assert(c.tracks.every(t => t.stopped));
  assert.equal(c.recording(), false);
  results.push({ probe: "repeated_keydown_during_permission", passed: true });

  // 第一次 resume 在取消后才返回（成功/失败均测试），不得清理第二次录音。
  for (const rejectOld of [false, true]) {
    c = context({ manualResume: true });
    const first = c.start();
    c.permissions[0].grant();
    await tick();
    c.events.blur();
    const second = c.start();
    c.permissions[1].grant();
    await tick();
    c.contexts[1].resuming.resolve();
    await second;
    if (rejectOld) c.contexts[0].resuming.reject(new Error("旧上下文被关闭"));
    else c.contexts[0].resuming.resolve();
    await first;
    assert.equal(c.recording(), true);
    assert.equal(c.contexts[1].closed, false);
    assert.equal(c.tracks[1].stopped, false);
    c.events.blur();
    assert(c.tracks.every(t => t.stopped));
    assert.equal(c.uploads.length, 0);
  }
  results.push({ probe: "stale_audio_context_cannot_close_new_recording", passed: true });

  c = context();
  const first = c.start();
  c.events.blur();
  const second = c.start();
  c.permissions[1].grant();
  await second;
  c.permissions[0].reject(new Error("旧权限请求失败"));
  await first;
  assert.equal(c.recording(), true);
  c.events["document:visibilitychange"](); // 可见时不取消。
  assert.equal(c.recording(), true);
  c.events.touchcancel();
  assert(c.tracks.every(t => t.stopped));
  results.push({ probe: "stale_permission_failure_and_touch_cancel", passed: true });

  const pendingUpload = deferred();
  c = context({ pendingUpload });
  start = c.start();
  c.permissions[0].grant();
  await start;
  c.samples();
  const stop = c.release();
  assert.equal(c.elements["btn-mic"].disabled, true);
  await c.start();
  assert.equal(c.permissions.length, 1);
  pendingUpload.resolve();
  await stop;
  assert.equal(c.elements["btn-mic"].disabled, false);
  assert.equal(c.uploads.length, 1);
  assert(c.tracks.every(t => t.stopped));
  results.push({ probe: "upload_blocks_additional_recording", passed: true });

  c = context({ meta: { typed: "", reply: "键入失败", mode: "raw", kind: "error" } });
  start = c.start();
  c.permissions[0].grant();
  await start;
  c.samples();
  await c.release();
  assert.match(c.elements["mic-status"].textContent, /处理失败.*键入失败/);
  results.push({ probe: "binary_failure_metadata_is_shown", passed: true });

  c = context({ meta: { typed: "", reply: "模拟问答", mode: "raw", kind: "ok", speak: true } });
  start = c.start();
  c.permissions[0].grant();
  await start;
  c.samples();
  await c.release();
  assert.equal(c.contexts[1].playbackRate, 16000);
  assert(c.contexts.every(ctx => ctx.closed));
  results.push({ probe: "wake_word_audio_plays_outside_dialog_mode", passed: true });

  c = context();
  start = c.start();
  c.permissions[0].grant();
  await start;
  // 不触发任何定时器，模拟后台节流；音频样本硬上限仍然生效。
  for (let second = 0; second < 60; second++) c.samples(48000);
  await tick();
  assert.equal(c.uploads.length, 1);
  assert.equal(c.uploads[0].body.byteLength, 60 * 16000 * 2);
  assert(c.tracks.every(t => t.stopped));
  assert.equal(c.recording(), false);
  results.push({ probe: "sample_count_caps_recording_without_timer", passed: true });
  console.log(JSON.stringify(results, null, 2));
}
main().catch(err => { console.error(err); process.exitCode = 1; });
