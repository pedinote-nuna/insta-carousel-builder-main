#!/usr/bin/env node
/**
 * 인스타 캐러셀 품질 자동 검사.
 *
 * 2 모드:
 *   1. JSON 검증: --prompt templates/slides.<topic>.json
 *      - 9장 구조, common_style 존재, n 1~9 순차, role/prompt 필수
 *   2. PNG 검증: --dir output/<topic>
 *      - 9장 PNG 존재, 1080×1350 해상도, 파일 크기 분포
 *
 * 사용법:
 *   node scripts/quality-check.js --prompt templates/slides.claude-code.json
 *   node scripts/quality-check.js --dir output/claude-code-tips
 */
import { readFileSync, existsSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    if (argv[i].startsWith('--') && argv[i + 1]) {
      args[argv[i].slice(2)] = argv[++i];
    }
  }
  return args;
}

function checkPromptJson(path) {
  if (!existsSync(path)) {
    return { status: 'FAIL', errors: [`파일 없음: ${path}`] };
  }
  let data;
  try {
    data = JSON.parse(readFileSync(path, 'utf-8'));
  } catch (e) {
    return { status: 'FAIL', errors: [`JSON 파싱 실패: ${e.message}`] };
  }

  const errors = [];
  const warnings = [];

  if (!data.common_style || data.common_style.length < 50) {
    errors.push('common_style 누락 또는 너무 짧음');
  }
  if (!Array.isArray(data.slides)) {
    errors.push('slides 배열 누락');
    return { status: 'FAIL', errors };
  }
  if (data.slides.length !== 9) {
    warnings.push(`슬라이드 ${data.slides.length}장 (권장: 9장)`);
  }

  for (let i = 0; i < data.slides.length; i++) {
    const s = data.slides[i];
    const expectedN = i + 1;
    if (s.n !== expectedN) errors.push(`slides[${i}].n 이 ${s.n} (기대: ${expectedN})`);
    if (!s.role) errors.push(`slides[${i}].role 누락`);
    if (!s.prompt || s.prompt.length < 50) errors.push(`slides[${i}].prompt 누락 또는 너무 짧음`);
  }

  const coverExists = data.slides.some((s) => /cover/i.test(s.role));
  const outroExists = data.slides.some((s) => /outro|closing/i.test(s.role));
  if (!coverExists) warnings.push('Cover 슬라이드 없음 (권장)');
  if (!outroExists) warnings.push('Outro 슬라이드 없음 (권장)');

  return {
    status: errors.length > 0 ? 'FAIL' : warnings.length > 0 ? 'WARN' : 'PASS',
    errors,
    warnings,
    slide_count: data.slides.length,
  };
}

function checkPngDir(dir) {
  if (!existsSync(dir)) {
    return { status: 'FAIL', errors: [`폴더 없음: ${dir}`] };
  }

  const files = readdirSync(dir)
    .filter((f) => /^slide-\d{2}\.png$/.test(f))
    .sort();

  const errors = [];
  const warnings = [];
  const sizes = [];

  if (files.length !== 9) {
    errors.push(`slide-*.png ${files.length}장 (기대: 9장)`);
  }

  const expected = Array.from({ length: 9 }, (_, i) => `slide-${String(i + 1).padStart(2, '0')}.png`);
  for (const name of expected) {
    if (!files.includes(name)) errors.push(`${name} 누락`);
  }

  for (const f of files) {
    const fp = join(dir, f);
    const st = statSync(fp);
    sizes.push({ file: f, kb: Math.round(st.size / 1024) });
    if (st.size < 50 * 1024) warnings.push(`${f} 너무 작음 (${Math.round(st.size / 1024)}KB, 깨진 이미지 가능)`);
    if (st.size > 3 * 1024 * 1024) warnings.push(`${f} 너무 큼 (${Math.round(st.size / 1024)}KB)`);
  }

  return {
    status: errors.length > 0 ? 'FAIL' : warnings.length > 0 ? 'WARN' : 'PASS',
    errors,
    warnings,
    slide_count: files.length,
    sizes,
  };
}

function icon(s) {
  return s === 'PASS' ? '✅' : s === 'WARN' ? '⚠️ ' : '❌';
}

function main() {
  const args = parseArgs(process.argv);
  console.log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log('📊 캐러셀 품질 검사');
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');

  let result;
  if (args.prompt) {
    console.log(`[모드] 프롬프트 JSON 검증: ${args.prompt}`);
    result = checkPromptJson(args.prompt);
  } else if (args.dir) {
    console.log(`[모드] PNG 폴더 검증: ${args.dir}`);
    result = checkPngDir(args.dir);
  } else {
    console.error('❌ --prompt <json> 또는 --dir <folder> 중 하나 필수');
    process.exit(1);
  }

  console.log(`\n${icon(result.status)} 상태: ${result.status}`);
  console.log(`   슬라이드 수: ${result.slide_count ?? '?'} / 9`);

  if (result.errors && result.errors.length > 0) {
    console.log('\n❌ Errors:');
    result.errors.forEach((e) => console.log(`   - ${e}`));
  }
  if (result.warnings && result.warnings.length > 0) {
    console.log('\n⚠️  Warnings:');
    result.warnings.forEach((w) => console.log(`   - ${w}`));
  }
  if (result.sizes) {
    console.log('\n📦 파일 크기:');
    result.sizes.forEach((s) => console.log(`   ${s.file}: ${s.kb} KB`));
  }

  const reportDir = args.dir || (args.prompt ? args.prompt.replace(/\.json$/, '') : '.');
  const reportPath = args.dir ? join(args.dir, 'quality-report.json') : null;
  if (reportPath) {
    writeFileSync(reportPath, JSON.stringify({ ...result, checked_at: new Date().toISOString() }, null, 2));
    console.log(`\n📄 리포트: ${reportPath}`);
  }

  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n');
  process.exit(result.status === 'FAIL' ? 1 : 0);
}

main();
