// No browser UI or API calls: reproduce the missing-toHex failure in fresh
// runtimes, then exercise the shipped compatibility worker and PDF parser.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const { version } = require('pdfjs-dist/package.json');
const worker = new URL(`../public/pdfjs/${version}/legacy/pdf.worker.min.mjs`, import.meta.url);
const scenario = process.argv[2];

if (!['modern', 'legacy', 'worker'].includes(scenario)) {
  const shipped = await readFile(worker);
  assert.deepEqual(shipped, await readFile(require.resolve('pdfjs-dist/legacy/build/pdf.worker.min.mjs')));
  const viewer = await readFile(new URL('../components/pdf-source-viewer.tsx', import.meta.url), 'utf8');
  assert.ok(viewer.includes("await import('pdfjs-dist/legacy/build/pdf.mjs')"));
  assert.ok(viewer.includes('/legacy/pdf.worker.min.mjs'));
  for (const name of ['modern', 'worker', 'legacy']) {
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url), name, ...process.argv.slice(2)], { encoding: 'utf8' });
    assert.equal(result.status, 0, `${name}: ${result.stdout}\n${result.stderr}`);
    process.stdout.write(result.stdout);
  }
} else {
  for (const key of ['toHex', 'toBase64', 'setFromHex', 'setFromBase64']) delete Uint8Array.prototype[key];
  for (const key of ['fromHex', 'fromBase64']) delete Uint8Array[key];
  assert.equal(typeof Uint8Array.prototype.toHex, 'undefined');
  if (scenario === 'worker') {
    // Separate process: main-thread polyfills cannot hide missing worker ones.
    const { WorkerMessageHandler } = await import(worker.href);
    assert.equal(typeof WorkerMessageHandler.setup, 'function');
    assert.equal(new Uint8Array([0, 15, 255]).toHex(), '000fff');
    assert.equal(new Uint8Array([0, 15, 255]).toBase64(), 'AA//');
    console.log('PASS: shipped worker supplies compatibility methods independently');
  } else {
    const { DOMMatrix, ImageData, Path2D, createCanvas } = await import('@napi-rs/canvas');
    Object.assign(globalThis, { DOMMatrix, ImageData, Path2D });
    const entry = scenario === 'legacy' ? 'pdfjs-dist/legacy/build/pdf.mjs' : 'pdfjs-dist/build/pdf.mjs';
    const pdfjs = await import(entry);
    pdfjs.GlobalWorkerOptions.workerSrc = scenario === 'legacy' ? worker.href
      : pathToFileURL(require.resolve('pdfjs-dist/build/pdf.worker.min.mjs')).href;
    const data = process.argv[3] ? new Uint8Array(await readFile(process.argv[3])) : fixture();
    const task = pdfjs.getDocument({ data, useSystemFonts: true });
    try {
      if (scenario === 'modern') {
        await assert.rejects(task.promise, /toHex is not a function/);
        console.log('PASS: original missing-toHex PDF failure reproduced');
      } else {
        const doc = await task.promise;
        assert.ok(doc.numPages > 0);
        assert.match(doc.fingerprints[0], /^[0-9a-f]+$/);
        for (let i = 1; i <= doc.numPages; i++) {
          const page = await doc.getPage(i);
          await page.getTextContent();
          const viewport = page.getViewport({ scale: .25 });
          const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
          await page.render({ canvas, canvasContext: canvas.getContext('2d'), viewport }).promise;
          page.cleanup();
        }
        console.log(`PASS: compatible parser, fingerprints, text and rendering (${doc.numPages} pages)`);
      }
    } finally { await task.destroy(); }
  }
}

function fixture() {
  const stream = 'BT /F1 12 Tf 10 40 Td (POSCO specification) Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 80] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
  ];
  let text = '%PDF-1.4\n';
  const offsets = [0];
  for (const [i, object] of objects.entries()) {
    offsets.push(text.length);
    text += `${i + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = text.length;
  text += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) text += `${String(offset).padStart(10, '0')} 00000 n \n`;
  text += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return new TextEncoder().encode(text);
}
