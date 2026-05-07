#!/usr/bin/env node
/**
 * PostToolUse 훅 — Write/Edit 후 자동 품질 검사.
 *
 * 설정: .claude/settings.json 의 hooks.PostToolUse 에 등록.
 *
 * 동작:
 *   - templates/slides.*.json 저장 시 → quality-check.js --prompt <파일> 자동 실행
 *   - output/*/slide-*.png 저장 시 → 같은 폴더에 대해 quality-check.js --dir 자동 실행
 *
 * stdin 으로 PostToolUse payload 받음 (Claude Code 훅 규약).
 */
import { spawnSync } from 'node:child_process';
import { dirname, basename, join } from 'node:path';

function readStdin() {
  try {
    return require('node:fs').readFileSync(0, 'utf-8');
  } catch {
    return '';
  }
}

function run(script, args) {
  const r = spawnSync('node', [script, ...args], { encoding: 'utf-8' });
  process.stdout.write(r.stdout || '');
  process.stderr.write(r.stderr || '');
  return r.status;
}

function main() {
  const raw = readStdin();
  let payload = {};
  try {
    payload = JSON.parse(raw);
  } catch {}

  const toolInput = payload.tool_input || {};
  const filePath = toolInput.file_path || '';
  if (!filePath) return;

  const fname = basename(filePath);
  const dir = dirname(filePath);

  // 1) 프롬프트 JSON 저장 시
  if (/^slides\..+\.json$/.test(fname) || fname === 'slides.example.json') {
    console.log(`\n[hook] 프롬프트 JSON 감지 → 스키마 검증`);
    run(join('scripts', 'quality-check.js'), ['--prompt', filePath]);
    return;
  }

  // 2) 슬라이드 PNG 저장 시 — 마지막 장(slide-09)일 때만 폴더 전체 검증
  if (/^slide-09\.png$/.test(fname)) {
    console.log(`\n[hook] slide-09 감지 → 폴더 전체 검증`);
    run(join('scripts', 'quality-check.js'), ['--dir', dir]);
    return;
  }
}

main();
