// Exercise the actual served worker without browser window/document globals.
// Run: node --experimental-vm-modules scripts/test-pdf-worker.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const { version } = require('pdfjs-dist/package.json');
const origin = 'http://localhost:3000';
const workerPath = `/pdfjs/${version}/pdf.worker.min.mjs`;
const original = await readFile(require.resolve('pdfjs-dist/build/pdf.worker.min.mjs'), 'utf8');
const built = process.argv.includes('--built');

const messages = [];
const listeners = [];
const globals = {
  console, URL, TextEncoder, TextDecoder, AbortController, structuredClone,
  setTimeout, clearTimeout,
  onmessage: null,
  postMessage: (message) => messages.push(message),
  addEventListener: (type, listener) => { if (type === 'message') listeners.push(listener); },
  removeEventListener() {},
};
globals.self = globals;
const context = vm.createContext(globals);
const modules = new Map();
async function load(url) {
  if (modules.has(url)) return modules.get(url);
  let source;
  if (built) {
    source = await readFile(new URL(`../dist/client${new URL(url).pathname}`, import.meta.url), 'utf8');
  } else {
    const result = await fetch(url);
    assert.equal(result.status, 200, `worker dependency must load: ${url}`);
    source = await result.text();
  }
  assert.equal(source, original, 'PDF worker must be served without JS transformations');
  assert.ok(!source.includes('/@vite/client'), 'window-only Vite client must not be imported by the PDF worker');
  const workerModule = new vm.SourceTextModule(source, { context, identifier: url });
  modules.set(url, workerModule);
  await workerModule.link((specifier) => load(new URL(specifier, url).href));
  return workerModule;
}
const entry = await load(new URL(workerPath, origin).href);
await entry.evaluate({ timeout: 10000 });
assert.equal(typeof entry.namespace.WorkerMessageHandler, 'function');
assert.ok(messages.some((message) => message.action === 'ready'), 'worker must announce ready');
const testData = vm.runInContext('new Uint8Array([1])', context);
for (const listener of listeners) {
  listener({ data: { sourceName: 'main', targetName: 'worker', action: 'test', data: testData } });
}
assert.ok(messages.some((message) => message.action === 'test' && message.data === true), 'worker must complete PDF.js handshake');
console.log(`PASS (${built ? 'production asset' : 'development URL'}): PDF worker starts without window/document and completes its handshake.`);
