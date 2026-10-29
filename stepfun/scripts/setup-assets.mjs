/**
 * Prepare the offline assets the browser needs:
 *   1. the FP32 and INT8 ONNX weights plus the tokenizer of all-MiniLM-L6-v2
 *   2. the ONNX Runtime wasm binaries, copied out of node_modules
 *
 * Everything lands under public/, so a later `npm run dev` or `npm run build`
 * serves it from the same origin and the app works without internet access.
 *
 *   node scripts/setup-assets.mjs [--mirror https://hf-mirror.com]
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createWriteStream } from 'node:fs';
import { pipeline as streamPipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';

const argIndex = process.argv.indexOf('--mirror');
const MIRROR = argIndex >= 0 && process.argv[argIndex + 1]
  ? process.argv[argIndex + 1]
  : (process.env.HF_ENDPOINT || 'https://hf-mirror.com');

const MODEL_FILES = [
  'config.json',
  'tokenizer.json',
  'tokenizer_config.json',
  'onnx/model.onnx',
  'onnx/model_quantized.onnx',
];

const ORT_FILES = [
  'ort-wasm-simd-threaded.asyncify.mjs',
  'ort-wasm-simd-threaded.asyncify.wasm',
  'ort-wasm-simd-threaded.jsep.mjs',
  'ort-wasm-simd-threaded.jsep.wasm',
  'ort-wasm-simd-threaded.jspi.mjs',
  'ort-wasm-simd-threaded.jspi.wasm',
  'ort-wasm-simd-threaded.mjs',
  'ort-wasm-simd-threaded.wasm',
];

function sizeOf(file) {
  return fs.existsSync(file) ? fs.statSync(file).size : 0;
}

async function download(url, destination) {
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status} for ${url}`);
  }
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.part`;
  await streamPipeline(Readable.fromWeb(response.body), createWriteStream(temporary));
  fs.renameSync(temporary, destination);
}

async function setupModels() {
  const base = path.join(ROOT, 'public', 'models', MODEL_ID);
  for (const relative of MODEL_FILES) {
    const destination = path.join(base, relative);
    if (sizeOf(destination) > 1024) {
      console.log(`skip  ${relative} (${(sizeOf(destination) / 1048576).toFixed(1)} MB)`);
      continue;
    }
    const url = `${MIRROR}/${MODEL_ID}/resolve/main/${relative}`;
    process.stdout.write(`fetch ${relative} … `);
    await download(url, destination);
    console.log(`${(sizeOf(destination) / 1048576).toFixed(1)} MB`);
  }
}

function setupOrt() {
  const source = path.join(ROOT, 'node_modules', 'onnxruntime-web', 'dist');
  const target = path.join(ROOT, 'public', 'ort');
  fs.mkdirSync(target, { recursive: true });
  for (const file of ORT_FILES) {
    const from = path.join(source, file);
    if (!fs.existsSync(from)) {
      console.log(`warn  ${file} is missing in onnxruntime-web, skipped`);
      continue;
    }
    fs.copyFileSync(from, path.join(target, file));
    console.log(`copy  ort/${file} (${(sizeOf(from) / 1048576).toFixed(1)} MB)`);
  }
}

async function main() {
  console.log(`mirror: ${MIRROR}`);
  await setupModels();
  setupOrt();
  console.log('\nassets ready under public/');
}

main().catch((error) => {
  console.error('FAILED:', error.message);
  process.exit(1);
});
