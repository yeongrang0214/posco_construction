// Generated public asset, deliberately bypassing Vite's JS/HMR transforms.
// Versioned URL keeps the PDF.js library and worker in sync after upgrades.
import { createRequire } from 'node:module';
import { copyFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { version } = require('pdfjs-dist/package.json');
const output = fileURLToPath(new URL(`../public/pdfjs/${version}/`, import.meta.url));
await mkdir(output, { recursive: true });
await copyFile(require.resolve('pdfjs-dist/build/pdf.worker.min.mjs'), path.join(output, 'pdf.worker.min.mjs'));
console.log(`PDF worker ready (${version}).`);
