#!/usr/bin/env node
// Lints every generated OpenAPI spec against the OWASP API Security Top 10
// (via Spectral + @stoplight/spectral-owasp-ruleset) and writes one report
// per repository at <repo>/api-security-audit/report.json — mirroring the
// other generated per-repo artifacts (db-schema, service-dependencies,
// dependency-alerts).
//
// Run from a checkout of the tenant's data repository (for DfE, atlas-dfe): it reads
// manifest.json and <repo>/open-api/*.json from the working directory and writes
// the reports there. The ruleset ships alongside this script; the PR summary is
// written to SUMMARY_FILE, outside the checkout, so it is never committed.
import { execFileSync } from 'node:child_process';
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const RULESET_PATH = process.env.RULESET_PATH || join(fileURLToPath(new URL('.', import.meta.url)), 'owasp.spectral.yaml');
const SUMMARY_FILE = process.env.SUMMARY_FILE || 'api-security-summary.md';
const SPECTRAL_VERSION = process.env.SPECTRAL_CLI_VERSION || '@stoplight/spectral-cli@^6';
const SEVERITY_LABELS = ['error', 'warning', 'info', 'hint'];

function repoSlug(repositoryUrl) {
  return repositoryUrl.replace(/\/$/, '').split('/').pop().replace(/\.git$/, '');
}

function severityLabel(value) {
  return SEVERITY_LABELS[value] || 'unknown';
}

function slugFromSource(source) {
  // Spectral reports `source` as an absolute path regardless of how the file was passed
  // on the CLI, so only the path segment immediately before `/open-api/` is the repo slug.
  const marker = '/open-api/';
  const index = source.indexOf(marker);
  if (index === -1) return null;
  return source.slice(0, index).split('/').pop();
}

function findSpecTargets() {
  const manifest = JSON.parse(readFileSync('manifest.json', 'utf8'));
  const targets = [];
  for (const entry of manifest) {
    const slug = repoSlug(entry['github-repo']);
    const dir = `${slug}/open-api`;
    let files = [];
    try {
      files = readdirSync(dir).filter((name) => name.endsWith('.json'));
    } catch {
      continue;
    }
    for (const file of files) targets.push(`${dir}/${file}`);
  }
  return targets;
}

function runSpectral(files) {
  const args = ['--yes', SPECTRAL_VERSION, 'lint', ...files, '--ruleset', RULESET_PATH, '--format', 'json'];
  try {
    return execFileSync('npx', args, { encoding: 'utf8', maxBuffer: 1024 * 1024 * 64 });
  } catch (error) {
    // Spectral exits non-zero when it finds error-severity results — the JSON is still on stdout.
    if (error.stdout) return error.stdout.toString();
    throw error;
  }
}

const targets = findSpecTargets();
if (!targets.length) {
  console.log('No generated OpenAPI specs found — nothing to audit.');
  process.exit(0);
}

const stdout = runSpectral(targets);
const results = stdout.trim() ? JSON.parse(stdout) : [];

const bySlug = new Map();
for (const target of targets) {
  const slug = target.split('/open-api/')[0];
  if (!bySlug.has(slug)) bySlug.set(slug, []);
}
for (const result of results) {
  const slug = slugFromSource(result.source || '');
  if (slug && bySlug.has(slug)) bySlug.get(slug).push(result);
}

const grandTotal = { error: 0, warning: 0, info: 0, hint: 0 };
const repoSummaries = [];

for (const [slug, findings] of bySlug) {
  const summary = { error: 0, warning: 0, info: 0, hint: 0 };
  const formatted = findings
    .map((finding) => ({
      code: finding.code,
      message: finding.message,
      severity: severityLabel(finding.severity),
      path: (finding.path || []).join('.'),
      source: relative(process.cwd(), finding.source || ''),
      line: finding.range?.start?.line != null ? finding.range.start.line + 1 : null
    }))
    .sort((a, b) => a.severity.localeCompare(b.severity) || a.source.localeCompare(b.source));
  formatted.forEach((finding) => { summary[finding.severity] = (summary[finding.severity] || 0) + 1; });
  Object.keys(summary).forEach((key) => { grandTotal[key] += summary[key]; });

  const report = {
    repository: slug,
    generatedAt: new Date().toISOString(),
    ruleset: '@stoplight/spectral-owasp-ruleset (OWASP API Security Top 10, 2023 edition)',
    specsAudited: targets.filter((target) => target.startsWith(`${slug}/open-api/`)).length,
    summary,
    findings: formatted
  };
  mkdirSync(`${slug}/api-security-audit`, { recursive: true });
  writeFileSync(`${slug}/api-security-audit/report.json`, `${JSON.stringify(report, null, 2)}\n`);
  repoSummaries.push({ slug, summary, total: findings.length });
}

repoSummaries.sort((a, b) => a.slug.localeCompare(b.slug));

const summaryLines = [
  `Automated OWASP API Security Top 10 audit of every generated OpenAPI spec, via [Spectral](https://github.com/stoplightio/spectral) and [@stoplight/spectral-owasp-ruleset](https://github.com/stoplightio/spectral-owasp-ruleset).`,
  '',
  `${targets.length} spec file(s) across ${bySlug.size} repositories · ${grandTotal.error} errors · ${grandTotal.warning} warnings · ${grandTotal.info} info`,
  '',
  '| Repository | Errors | Warnings | Info |',
  '| --- | --- | --- | --- |',
  ...repoSummaries.map((repo) => `| ${repo.slug} | ${repo.summary.error} | ${repo.summary.warning} | ${repo.summary.info} |`),
  '',
  'Generated by the `api-security-audit` workflow in talentconsulting-azure-foundry.'
];
writeFileSync(SUMMARY_FILE, `${summaryLines.join('\n')}\n`);

console.log(`Audited ${targets.length} spec file(s) across ${bySlug.size} repositories: ${grandTotal.error} errors, ${grandTotal.warning} warnings, ${grandTotal.info} info.`);

if (process.env.GITHUB_OUTPUT) {
  writeFileSync(process.env.GITHUB_OUTPUT, `errors=${grandTotal.error}\nwarnings=${grandTotal.warning}\n`, { flag: 'a' });
}
