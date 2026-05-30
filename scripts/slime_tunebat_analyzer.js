#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");

const SAMPLE_RATE = 44100;

const CAMELOT = {
  "A minor": "8A",
  "E minor": "9A",
  "B minor": "10A",
  "F# minor": "11A",
  "C# minor": "12A",
  "G# minor": "1A",
  "D# minor": "2A",
  "A# minor": "3A",
  "F minor": "4A",
  "C minor": "5A",
  "G minor": "6A",
  "D minor": "7A",
  "C major": "8B",
  "G major": "9B",
  "D major": "10B",
  "A major": "11B",
  "E major": "12B",
  "B major": "1B",
  "F# major": "2B",
  "C# major": "3B",
  "G# major": "4B",
  "D# major": "5B",
  "A# major": "6B",
  "F major": "7B"
};

function usage() {
  console.error("usage: node scripts/slime_tunebat_analyzer.js AUDIO_FILE [--json-out PATH]");
}

function parseArgs(argv) {
  const args = { input: null, jsonOut: null };
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--json-out") {
      args.jsonOut = argv[index + 1];
      index += 1;
    } else if (!args.input) {
      args.input = value;
    } else {
      throw new Error(`unexpected argument: ${value}`);
    }
  }
  if (!args.input) {
    usage();
    process.exit(2);
  }
  return args;
}

function loadEssentia() {
  try {
    return require("essentia.js");
  } catch (error) {
    throw new Error("missing optional dependency essentia.js; run `npm install` in the repo root");
  }
}

function decodeMonoFloat(input) {
  const ffmpegArgs = [
    "-v",
    "error",
    "-i",
    input,
    "-ac",
    "1",
    "-ar",
    String(SAMPLE_RATE),
    "-f",
    "f32le",
    "pipe:1"
  ];
  const raw = childProcess.execFileSync("ffmpeg", ffmpegArgs, { maxBuffer: 1024 * 1024 * 1024 });
  return new Float32Array(raw.buffer, raw.byteOffset, raw.length / 4);
}

function normalizeBpm(bpm) {
  if (!Number.isFinite(bpm) || bpm <= 0) {
    return null;
  }
  let value = bpm;
  while (value < 80) {
    value *= 2;
  }
  while (value > 180) {
    value /= 2;
  }
  return Math.round(value * 100) / 100;
}

function bpmCandidates(bpm) {
  if (!Number.isFinite(bpm) || bpm <= 0) {
    return [];
  }
  const values = [bpm / 2, bpm, bpm * 2]
    .filter((value) => value >= 50 && value <= 220)
    .map((value) => Math.round(value * 100) / 100);
  return [...new Set(values)];
}

function rms(audio) {
  if (!audio.length) {
    return 0;
  }
  let total = 0;
  for (const sample of audio) {
    total += sample * sample;
  }
  return Math.sqrt(total / audio.length);
}

function maxAbs(audio) {
  let peak = 0;
  for (const sample of audio) {
    const value = Math.abs(sample);
    if (value > peak) {
      peak = value;
    }
  }
  return peak;
}

function analyze(input) {
  const { Essentia, EssentiaWASM } = loadEssentia();
  const audio = decodeMonoFloat(input);
  const essentia = new Essentia(EssentiaWASM);
  const vector = essentia.arrayToVector(audio);
  try {
    const rhythm = essentia.RhythmExtractor2013(vector, 210, "multifeature", 50);
    const key = essentia.KeyExtractor(vector, true, 4096, 4096, 12, 3500, 60, 25, 0.2, "edma", SAMPLE_RATE);
    const detectedBpm = rhythm && Number.isFinite(rhythm.bpm) ? rhythm.bpm : null;
    const scale = key && key.scale ? String(key.scale) : "";
    const keyName = key && key.key ? `${key.key} ${scale}`.trim() : "";
    const durationSeconds = audio.length / SAMPLE_RATE;
    return {
      analyzer: "slime_tunebat_local_essentia_js",
      analyzer_url: "https://tunebat.com/Analyzer",
      input_path: input,
      filename: path.basename(input),
      sample_rate: SAMPLE_RATE,
      duration_seconds: Math.round(durationSeconds * 1000) / 1000,
      key: keyName,
      mode: scale,
      camelot: CAMELOT[keyName] || "",
      bpm: normalizeBpm(detectedBpm),
      bpm_candidates: bpmCandidates(detectedBpm),
      raw_bpm: detectedBpm === null ? null : Math.round(detectedBpm * 1000) / 1000,
      confidence: {
        bpm: rhythm && Number.isFinite(rhythm.confidence) ? rhythm.confidence : null,
        key: key && Number.isFinite(key.strength) ? key.strength : null
      },
      energy: Math.round(Math.min(1, rms(audio) * 8) * 1000) / 1000,
      peak: Math.round(maxAbs(audio) * 1000) / 1000,
      raw: {
        rhythm,
        key
      }
    };
  } finally {
    vector.delete();
    essentia.delete();
  }
}

function main() {
  const args = parseArgs(process.argv);
  if (!fs.existsSync(args.input)) {
    throw new Error(`file not found: ${args.input}`);
  }
  const result = analyze(args.input);
  const json = `${JSON.stringify(result, null, 2)}\n`;
  if (args.jsonOut) {
    fs.writeFileSync(args.jsonOut, json, "utf8");
  } else {
    process.stdout.write(json);
  }
}

try {
  main();
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
