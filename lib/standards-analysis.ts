import type { Candidate } from '@/lib/api';

export interface StandardReferenceCheck {
  raw: string;
  foundInKcs: boolean;
}

export interface StandardsAnalysis {
  references: StandardReferenceCheck[];
  measurements: StandardReferenceCheck[];
  hasProtectedContent: boolean;
  requiresConfirmation: boolean;
}

const STANDARD_PATTERNS = [
  /\bKS\s*[A-Z](?:\s*[A-Z])?\s*[-–—]?\s*\d{3,}(?:[-–—]\d+)?\b/giu,
  /\bKCS\s*\d{2}\s*\d{2}\s*\d{2}(?:\s*[-–—]?\s*\d+(?:\.\d+)*)?\b/giu,
  /\b(?:ASTM|JIS|ISO|EN|DIN|AISC|AWS)\s*[A-Z0-9][A-Z0-9./()\-\s]{1,18}\d\b/giu,
];

const MEASUREMENT_PATTERN =
  /(?<![\p{L}\d])(?:\d+(?:[.,]\d+)?(?:\s*[~～-]\s*\d+(?:[.,]\d+)?)?)\s*(?:mm²|mm2|㎟|mm|cm|㎝|m²|m2|㎡|m³|m3|㎥|m|kgf\/cm²|kgf\/㎠|kg\/m³|kg\/m3|kg|g|MPa|kPa|Pa|N\/mm²|N\/mm2|kN|N|%|℃|°C|회|개소|시간|분|초)(?![\p{L}\d])/giu;

function canonical(value: string) {
  return value
    .normalize('NFKC')
    .toUpperCase()
    .replace(/[–—]/g, '-')
    .replace(/\s+/g, '')
    .replace(/,/g, '.');
}

function uniqueMatches(text: string, patterns: RegExp[]) {
  const seen = new Map<string, string>();
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const raw = match[0].trim();
      const key = canonical(raw);
      if (key && !seen.has(key)) seen.set(key, raw);
    }
  }
  return [...seen.entries()].map(([key, raw]) => ({ key, raw }));
}

export function analyzeStandards(
  sourceText: string,
  candidates: Candidate[],
): StandardsAnalysis {
  const candidateText = candidates
    .map(
      (candidate) =>
        `${candidate.kcs_code} ${candidate.kcs_clause} ${candidate.title} ${candidate.content}`,
    )
    .join('\n');
  const normalizedCandidates = canonical(candidateText);

  const references = uniqueMatches(sourceText, STANDARD_PATTERNS).map(
    ({ key, raw }) => ({
      raw,
      foundInKcs: normalizedCandidates.includes(key),
    }),
  );
  const measurements = uniqueMatches(sourceText, [MEASUREMENT_PATTERN]).map(
    ({ key, raw }) => ({
      raw,
      foundInKcs: normalizedCandidates.includes(key),
    }),
  );
  const hasProtectedContent = references.length > 0 || measurements.length > 0;
  // A KCS occurrence is useful evidence, but it does not prove that the referenced
  // external standard itself is still current. Any external standard therefore
  // needs a human confirmation; measurements need it only when KCS differs.
  const requiresConfirmation =
    references.length > 0 || measurements.some((item) => !item.foundInKcs);

  return {
    references,
    measurements,
    hasProtectedContent,
    requiresConfirmation,
  };
}
