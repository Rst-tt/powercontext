/**
 * Browser-side embedding.
 *
 * The model files live under /models (downloaded once into public/models), so the
 * inference never leaves the device: no text is sent anywhere before it has been
 * turned into a vector. WebGPU is preferred and WASM is used when the browser
 * cannot provide a GPU adapter.
 */
import { env, pipeline } from '@huggingface/transformers';

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = '/models/';
env.useBrowserCache = true;
// Serve the ONNX Runtime wasm binaries from this origin instead of a CDN, so the
// application also works on a machine without internet access.
env.backends.onnx.wasm.wasmPaths = '/ort/';

export const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';
export const MODEL_DIMENSION = 384;

/** Returns 'webgpu' when an adapter is available, otherwise 'wasm'. */
export async function detectBackend() {
  const forced = new URLSearchParams(location.search).get('backend');
  if (forced === 'wasm' || forced === 'webgpu') return forced;
  try {
    if (!('gpu' in navigator) || !navigator.gpu) return 'wasm';
    const adapter = await navigator.gpu.requestAdapter();
    return adapter ? 'webgpu' : 'wasm';
  } catch {
    return 'wasm';
  }
}

export class Embedder {
  constructor({ precision = 'int8' } = {}) {
    this.precision = precision;
    this.pipe = null;
    this.device = null;
    this.loadMs = 0;
  }

  /** fp32 is the reference build; int8 is the quantized build (q8 on wasm). */
  get dtype() {
    return this.precision === 'fp32' ? 'fp32' : 'q8';
  }

  async load({ onProgress } = {}) {
    const preferred = await detectBackend();
    const forced = new URLSearchParams(location.search).get('backend');
    const devices = forced
      ? (forced === 'webgpu' ? ['webgpu', 'wasm'] : ['wasm'])
      : (preferred === 'webgpu' ? ['webgpu', 'wasm'] : ['wasm']);
    const errors = [];

    for (const device of devices) {
      const started = performance.now();
      const dtype = device === 'webgpu' ? 'fp32' : this.dtype;
      try {
        this.pipe = await pipeline('feature-extraction', MODEL_ID, {
          device,
          // WebGPU runs the fp32 graph (fp16 weights are not shipped here);
          // the wasm backend uses the quantized graph unless fp32 was selected.
          dtype,
          progress_callback: (event) => onProgress?.(event),
        });
        this.device = device;
        this.dtypeUsed = dtype;
        this.loadMs = performance.now() - started;
        return { device, dtype, loadMs: this.loadMs, fellBack: device !== preferred };
      } catch (error) {
        errors.push(`${device}: ${error.message}`);
      }
    }
    throw new Error(`could not initialise the model (${errors.join('; ')})`);
  }

  async embed(texts, { batchSize = 8, onProgress } = {}) {
    if (!this.pipe) throw new Error('model is not loaded');
    const vectors = [];
    const started = performance.now();
    for (let offset = 0; offset < texts.length; offset += batchSize) {
      const batch = texts.slice(offset, offset + batchSize);
      const output = await this.pipe(batch, { pooling: 'mean', normalize: true });
      vectors.push(...output.tolist());
      onProgress?.(Math.min(offset + batch.length, texts.length), texts.length);
    }
    return { vectors, elapsedMs: performance.now() - started };
  }
}

/**
 * Split a document into overlapping chunks on paragraph / sentence boundaries.
 */
export function chunkText(text, { size = 360, overlap = 40 } = {}) {
  const clean = String(text).replace(/\r\n/g, '\n').trim();
  if (!clean) return [];
  const paragraphs = clean.split(/\n{2,}/).flatMap((block) => {
    if (block.length <= size) return [block];
    const sentences = block.split(/(?<=[.!?。！？])\s+/);
    const chunks = [];
    let buffer = '';
    for (const sentence of sentences) {
      if ((buffer + sentence).length > size && buffer) {
        chunks.push(buffer.trim());
        buffer = '';
      }
      buffer += sentence;
    }
    if (buffer.trim()) chunks.push(buffer.trim());
    return chunks;
  });

  const chunks = [];
  let current = '';
  for (const paragraph of paragraphs) {
    if ((current + '\n\n' + paragraph).length > size && current) {
      chunks.push(current.trim());
      current = current.slice(Math.max(0, current.length - overlap)) + '\n\n' + paragraph;
    } else {
      current = current ? `${current}\n\n${paragraph}` : paragraph;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks.filter((chunk) => chunk.length > 0);
}
