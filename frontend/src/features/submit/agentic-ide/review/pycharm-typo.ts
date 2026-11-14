// A port of IntelliJ Community's TypoTolerantMatcher.kt, the matcher that completion builds when
// typo tolerance is on. The caller passes the pattern as CamelHumpMatcher.applyMiddleMatching
// makes it ("*" + prefix). Pattern and name index by UTF-16 unit, as Kotlin's Char does.

import {
  type Ch,
  containsMeaningfulInOrder,
  type Fragment,
  isDigit,
  isLetterOrDigit,
  isLower,
  isSeparatorChar,
  isUpper,
  isWildcardChar,
  isWordStart,
  type Mode,
  nextWord as nameUtilNextWord,
  // @ts-expect-error TS5097: node --experimental-strip-types needs the .ts extension to load this file in tests.
} from "./pycharm-match.ts";

/** MatchedFragment: a name range and the number of typos inside it. */
export type MatchedFragment = [from: number, to: number, errorCount: number];

const INT_MIN = -2147483648;
const INT_MAX = 2147483647;

// Kotlin's uppercaseChar keeps the char when the case mapping is not one char (for example, "ß").
const oneChar = (c: string, mapped: string) => (mapped.length === 1 ? mapped : c);
const isUpperAscii = (c: Ch) => c !== undefined && c >= "A" && c <= "Z";
const isLowerAscii = (c: Ch) => c !== undefined && c >= "a" && c <= "z";
const toLowerAscii = (c: Ch) => (isUpperAscii(c) ? c!.toLowerCase() : c);
const toUpperAscii = (c: Ch) => (isLowerAscii(c) ? c!.toUpperCase() : c);
const isAsciiChar = (c: Ch) => c !== undefined && c.charCodeAt(0) < 128;
const charEqualsIgnoreCase = (a: Ch, b: Ch) =>
  a === b || (a !== undefined && b !== undefined && (a.toUpperCase() === b.toUpperCase() || a.toLowerCase() === b.toLowerCase()));

function isAscii(s: string): boolean {
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) >= 128) return false;
  return true;
}

function indexOfAny(s: string, chars: string, start: number, end: number): number {
  for (let i = start; i < end; i++) if (chars.includes(s[i]!)) return i;
  return -1;
}

// AsciiUtils.nextWordAscii is NameUtilCore.nextWord restricted to ASCII, so one function serves both.
function nextWord(name: string, start: number): number {
  return start < name.length && isDigit(name[start]) ? start + 1 : nameUtilNextWord(name, start);
}

function appendRange(ranges: MatchedFragment[], range: MatchedFragment): MatchedFragment[] {
  if (ranges.length === 0) return [range];
  const last = ranges.at(-1)!;
  if (last[0] === range[1]) ranges[ranges.length - 1] = [range[0], last[1], range[2] + last[2]];
  else ranges.push(range);
  return ranges;
}

/** MinusculeMatcher.evaluateCaseMatching. */
function evaluateCaseMatching(
  pattern: string[],
  valuedStartMatch: boolean,
  p: number,
  humpStartMatchedUpperCase: boolean,
  nameIndex: number,
  afterGap: boolean,
  isHumpStart: boolean,
  nameChar: string,
  lower: boolean[],
  upper: boolean[],
): number {
  // A hump with nothing in the pattern to say the user meant a hump.
  if (afterGap && isHumpStart && lower[p]) return -10;
  if (nameChar === pattern[p]) {
    // The user pressed Shift on purpose.
    if (upper[p]) return 50;
    // The first letter's case tells classes apart in Java and others.
    if (nameIndex === 0 && valuedStartMatch) return 150;
    return isHumpStart ? 1 : 0;
  }
  if (isHumpStart) return -1;
  if (lower[p] && humpStartMatchedUpperCase) return -1;
  return 0;
}

const keyboard = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];

function neighbourKey(aChar: Ch, step: -1 | 1): string | null {
  const upper = isUpperAscii(aChar);
  const lc = upper ? toLowerAscii(aChar) : aChar;
  for (const line of keyboard) {
    for (let j = 0; j < line.length; j++) {
      if (line[j] !== lc) continue;
      const c = line[j + step];
      if (c === undefined) return null;
      return upper ? toUpperAscii(c)! : c;
    }
  }
  return null;
}

const leftMiss = (c: Ch) => neighbourKey(c, -1);
const rightMiss = (c: Ch) => neighbourKey(c, 1);

type PatternError = { kind: "typo"; correctChar: string } | { kind: "swap" } | { kind: "miss"; missedChar: string };
type ErrorWithIndex = { index: number; error: PatternError };

function applyError(pattern: Ch[], e: ErrorWithIndex): Ch[] {
  const err = e.error;
  if (err.kind === "typo") {
    pattern[e.index] = err.correctChar;
    return pattern;
  }
  if (err.kind === "swap") {
    const c = pattern[e.index];
    pattern[e.index] = pattern[e.index + 1];
    pattern[e.index + 1] = c;
    return pattern;
  }
  return [...pattern.slice(0, e.index), err.missedChar, ...pattern.slice(e.index)];
}

// A chain of states: each derived state adds the errors found from its derive index onward.
class ErrorState {
  private readonly base: ErrorState | null;
  private readonly deriveIndex: number;
  private affected: Set<number> | null = null;
  private allAffectedAfter = INT_MAX;
  private errors: ErrorWithIndex[] | null = null;
  private pattern: Ch[] | null = null;

  constructor(base: ErrorState | null = null, deriveIndex = 0) {
    this.base = base;
    this.deriveIndex = deriveIndex;
  }

  deriveFrom(index: number): ErrorState {
    return new ErrorState(this, index);
  }

  addError(index: number, error: PatternError): void {
    const e = { index, error };
    (this.errors ??= []).push(e);
    const affected = (this.affected ??= new Set());
    affected.add(index);
    if (error.kind === "swap") affected.add(index + 1);
    else if (error.kind === "miss") this.allAffectedAfter = Math.min(index, this.allAffectedAfter);
    if (this.pattern) this.pattern = applyError(this.pattern, e);
  }

  countErrors(start: number, end: number): number {
    let errors = 0;
    if (this.base && start < this.deriveIndex) errors += this.base.countErrors(start, this.deriveIndex);
    for (const e of this.errors ?? []) if (e.index >= start && e.index < end) errors++;
    return errors;
  }

  getChar(pattern: string[], index: number): Ch {
    this.pattern ??= this.applyErrors([...pattern], INT_MAX);
    return this.pattern[index];
  }

  applyErrors(pattern: Ch[], upToIndex: number): Ch[] {
    let result = pattern;
    if (this.base) result = this.base.applyErrors(pattern, Math.min(this.deriveIndex, upToIndex));
    for (const e of this.errors ?? []) if (e.index < upToIndex) result = applyError(result, e);
    return result;
  }

  affects(index: number): boolean {
    return this.localAffects(index) || (this.base !== null && this.base.affects(index));
  }

  private localAffects(index: number): boolean {
    return index >= this.allAffectedAfter || (this.affected !== null && this.affected.has(index));
  }

  getError(i: number): PatternError | null {
    if (this.errors && this.affected!.has(i)) {
      for (const e of this.errors) if (e.index === i) return e.error;
    }
    if (this.base && this.deriveIndex > i) return this.base.getError(i);
    return null;
  }

  private numMisses(end: number): number {
    let n = 0;
    if (this.errors && end > 0) for (const e of this.errors) if (e.index < end && e.error.kind === "miss") n++;
    return n + (this.base?.numMisses(this.deriveIndex) ?? 0);
  }

  length(pattern: string[]): number {
    return this.pattern ? this.pattern.length : pattern.length + this.numMisses(INT_MAX);
  }
}

type Found = { length: number; errorState: ErrorState };

type Compiled = {
  pattern: string[];
  mode: Mode;
  hardSeparators: string;
  mixedCase: boolean;
  hasSeparators: boolean;
  hasDots: boolean;
  lower: boolean[];
  upper: boolean[];
  separator: boolean[];
  toU: string[];
  toL: string[];
  meaningful: string[];
};

class Session {
  private readonly m: Compiled;
  private readonly name: string;
  private readonly typoAware: boolean;
  private readonly asciiName: boolean;
  private readonly allowTypos: boolean;

  constructor(m: Compiled, name: string, typoAware: boolean, asciiName: boolean) {
    this.m = m;
    this.name = name;
    this.typoAware = typoAware;
    this.asciiName = asciiName;
    this.allowTypos = typoAware && asciiName;
  }

  private charAt(i: number, es: ErrorState): Ch {
    return es.affects(i) ? es.getChar(this.m.pattern, i) : this.m.pattern[i];
  }

  private isWildcard(pi: number): boolean {
    return pi >= 0 && pi < this.m.pattern.length && isWildcardChar(this.m.pattern[pi]);
  }

  private equalsIgnoreCase(pi: number, es: ErrorState, nameChar: Ch): boolean {
    if (es.affects(pi)) {
      const pc = es.getChar(this.m.pattern, pi);
      return toLowerAscii(pc) === nameChar || toUpperAscii(pc) === nameChar;
    }
    return this.m.toL[pi] === nameChar || this.m.toU[pi] === nameChar;
  }

  private isLowerCase(i: number, es: ErrorState): boolean {
    return es.affects(i) ? isLowerAscii(es.getChar(this.m.pattern, i)) : this.m.lower[i] === true;
  }

  private isUpperCase(i: number, es: ErrorState): boolean {
    return es.affects(i) ? isUpperAscii(es.getChar(this.m.pattern, i)) : this.m.upper[i] === true;
  }

  private isDigit(i: number, es: ErrorState): boolean {
    return isDigit(es.affects(i) ? es.getChar(this.m.pattern, i) : this.m.pattern[i]);
  }

  private isWordSeparator(i: number, es: ErrorState): boolean {
    return es.affects(i) ? isSeparatorChar(es.getChar(this.m.pattern, i)) : this.m.separator[i] === true;
  }

  private patternLength(es: ErrorState): number {
    return es.length(this.m.pattern);
  }

  matchingFragments(): MatchedFragment[] | null {
    if (this.name.length < this.m.meaningful.length / 2) return null;
    // Typo mode works on ASCII names only.
    if (this.typoAware && !this.asciiName) return null;
    if (!this.typoAware && !containsMeaningfulInOrder(this.name, this.m.meaningful)) return null;
    return this.matchWildcards(0, 0, new ErrorState())?.reverse() ?? null;
  }

  private matchWildcards(patternIndex: number, nameIndex: number, es: ErrorState): MatchedFragment[] | null {
    let pi = patternIndex;
    if (nameIndex < 0) return null;
    if (!this.isWildcard(pi)) return pi === this.patternLength(es) ? [] : this.matchFragment(pi, nameIndex, es);
    do pi++;
    while (this.isWildcard(pi));
    if (pi === this.patternLength(es)) {
      // A trailing space matches when the pattern ends with the last word part, or only its first hump.
      if (
        this.isTrailingSpacePattern(es) &&
        nameIndex !== this.name.length &&
        (pi < 2 || !this.isUpperCaseOrDigit(pi - 2, es))
      ) {
        const space = this.name.indexOf(" ", nameIndex);
        return space >= 0 ? [[space, space + 1, 0]] : null;
      }
      return [];
    }
    return this.matchFragment(pi, nameIndex, es) ?? this.matchSkippingWords(pi, nameIndex, true, es);
  }

  private isTrailingSpacePattern(es: ErrorState): boolean {
    return this.isPatternChar(this.patternLength(es) - 1, " ", es);
  }

  private isUpperCaseOrDigit(pi: number, es: ErrorState): boolean {
    return this.isUpperCase(pi, es) || this.isDigit(pi, es);
  }

  private matchSkippingWords(pi: number, nameIndex: number, allowSpecialChars: boolean, es: ErrorState): MatchedFragment[] | null {
    const wordStartsOnly = !this.isPatternChar(pi - 1, "*", es) && !this.isWordSeparator(pi, es);
    let ni = nameIndex;
    let maxFoundLength = 0;
    for (;;) {
      ni = this.findNextPatternCharOccurrence(ni, pi, allowSpecialChars, wordStartsOnly, es);
      if (ni < 0) return null;
      const fragment = this.seemsLikeFragmentStart(pi, ni, es) ? this.maxMatchingFragment(pi, ni, es) : null;
      if (fragment === null) continue;
      // A fragment no longer than one already tried cannot match the rest of the pattern either.
      const len = fragment.length;
      if (len > maxFoundLength || (ni + len === this.name.length && this.isTrailingSpacePattern(es))) {
        if (!this.isMiddleMatch(pi, ni, es)) maxFoundLength = len;
        const ranges = this.matchInsideFragment(pi, ni, fragment);
        if (ranges) return ranges;
      }
    }
  }

  private findNextPatternCharOccurrence(
    startAt: number,
    pi: number,
    allowSpecialChars: boolean,
    wordStartsOnly: boolean,
    es: ErrorState,
  ): number {
    const next = wordStartsOnly ? this.indexOfWordStart(pi, startAt, es) : this.indexOfIgnoreCase(startAt + 1, pi, es);
    const m = this.m;
    // Pattern humps may match in words separated by hard separators; lowercase letters may not.
    if (!allowSpecialChars && !m.hasSeparators && !m.mixedCase && indexOfAny(this.name, m.hardSeparators, startAt, next) !== -1) {
      return -1;
    }
    // A typed dot does not skip other dots between humps, but one pattern dot may match several name dots.
    if (!allowSpecialChars && m.hasDots && !this.isPatternChar(pi - 1, ".", es) && indexOfAny(this.name, ".", startAt, next) !== -1) {
      return -1;
    }
    return next;
  }

  private seemsLikeFragmentStart(pi: number, ni: number, es: ErrorState): boolean {
    // Uppercase matches uppercase or a word start, or lowercase when the whole pattern has one case.
    return !this.isUpperCase(pi, es) || isUpper(this.name[ni]) || isWordStart(this.name, ni) || !this.m.mixedCase;
  }

  private charEquals(pi: number, ni: number, ignoreCase: boolean, allowTypos: boolean, es: ErrorState): boolean {
    const pc = this.charAt(pi, es);
    const nc = this.name[ni];
    if (pc === nc || (ignoreCase && this.equalsIgnoreCase(pi, es, nc))) return true;
    if (!this.allowTypos || !allowTypos) return false;
    if (es.countErrors(0, pi) > 0) return false;
    if (es.getError(pi - 1)?.kind === "swap") return false;
    for (const miss of [leftMiss(pc), rightMiss(pc)]) {
      if (miss !== null && (miss === nc || (ignoreCase && (toLowerAscii(miss) === nc || toUpperAscii(miss) === nc)))) {
        es.addError(pi, { kind: "typo", correctChar: miss });
        return true;
      }
    }
    if (this.patternLength(es) > pi + 1 && this.name.length > ni + 1) {
      const nextNameChar = this.name[ni + 1];
      const nextPatternChar = this.charAt(pi + 1, es);
      if (
        (pc === nextNameChar || (ignoreCase && this.equalsIgnoreCase(pi, es, nextNameChar))) &&
        (nextPatternChar === nc || (ignoreCase && this.equalsIgnoreCase(pi + 1, es, nc)))
      ) {
        es.addError(pi, { kind: "swap" });
        return true;
      }
    }
    if (this.name.length > ni + 1) {
      const nextNameChar = this.name[ni + 1];
      if (pc === nextNameChar || (ignoreCase && this.equalsIgnoreCase(pi, es, nextNameChar))) {
        es.addError(pi, { kind: "miss", missedChar: nc! });
        return true;
      }
    }
    return false;
  }

  private matchFragment(pi: number, ni: number, es: ErrorState): MatchedFragment[] | null {
    const fragment = this.maxMatchingFragment(pi, ni, es);
    return fragment ? this.matchInsideFragment(pi, ni, fragment) : null;
  }

  private maxMatchingFragment(pi: number, ni: number, baseErrorState: ErrorState): Found | null {
    const es = baseErrorState.deriveFrom(pi);
    if (!this.isFirstCharMatching(ni, pi, es)) return null;
    let i = 1;
    while (ni + i < this.name.length && pi + i < this.patternLength(es)) {
      if (!this.charEquals(pi + i, ni + i, true, true, es)) {
        if (this.isDigit(pi + i, es) && this.isDigit(pi + i - 1, es)) return null;
        break;
      }
      i++;
    }
    return { length: i, errorState: es };
  }

  private matchInsideFragment(pi: number, ni: number, fragment: Found): MatchedFragment[] | null {
    // Exact middle matches need 3 chars or more, to keep out irrelevant matches.
    const minFragment = this.isMiddleMatch(pi, ni, fragment.errorState) ? 3 : 1;
    return (
      this.improveCamelHumps(pi, ni, fragment.length, minFragment, fragment.errorState) ??
      this.findLongestMatchingPrefix(pi, ni, fragment.length, minFragment, fragment.errorState)
    );
  }

  private isMiddleMatch(pi: number, ni: number, es: ErrorState): boolean {
    return (
      this.isPatternChar(pi - 1, "*", es) &&
      !this.isWildcard(pi + 1) &&
      isLetterOrDigit(this.name[ni]) &&
      !isWordStart(this.name, ni)
    );
  }

  private findLongestMatchingPrefix(
    pi: number,
    ni: number,
    fragmentLength: number,
    minFragment: number,
    es: ErrorState,
  ): MatchedFragment[] | null {
    if (pi + fragmentLength >= this.patternLength(es)) {
      const errors = es.countErrors(pi, pi + fragmentLength);
      return errors === fragmentLength ? null : [[ni, ni + fragmentLength, errors]];
    }
    // The longest fragment may not let the rest of the pattern match; then try shorter ones.
    for (let i = fragmentLength; i >= minFragment || this.isWildcard(pi + i); i--) {
      const derived = es.deriveFrom(pi + i);
      const ranges = this.isWildcard(pi + i)
        ? this.matchWildcards(pi + i, ni + i, derived)
        : this.matchSkippingWords(pi + i, ni + i, false, derived);
      if (ranges) {
        const errors = es.countErrors(pi, pi + i);
        return errors === i ? null : appendRange(ranges, [ni, ni + i, errors]);
      }
    }
    return null;
  }

  // For "CU" and "CurrentUser", "Cu" already matches, but an uppercase "U" further on scores better.
  private improveCamelHumps(pi: number, ni: number, maxFragment: number, minFragment: number, es: ErrorState): MatchedFragment[] | null {
    for (let i = minFragment; i < maxFragment; i++) {
      if (this.isUpperCase(pi + i, es) && !this.charEquals(pi + i, ni + i, false, false, es)) {
        const further = es.deriveFrom(pi + i);
        const ranges = this.matchWildcards(pi + i, this.indexOfWordStart(pi + i, ni + i, further), further.deriveFrom(pi + i));
        if (ranges) {
          const errors = es.countErrors(pi, pi + i);
          return errors === i ? null : appendRange(ranges, [ni, ni + i, errors]);
        }
      }
    }
    return null;
  }

  private isFirstCharMatching(ni: number, pi: number, es: ErrorState): boolean {
    if (ni >= this.name.length) return false;
    if (!this.charEquals(pi, ni, true, true, es)) return false;
    return !(
      this.m.mode === "first-letter" &&
      (pi === 0 || (pi === 1 && this.isWildcard(0))) &&
      (this.isUpperCase(pi, es) || this.isLowerCase(pi, es)) &&
      this.isUpperCase(pi, es) !== isUpper(this.name[0])
    );
  }

  private isPatternChar(pi: number, c: string, es: ErrorState): boolean {
    return pi >= 0 && pi < this.patternLength(es) && this.charAt(pi, es) === c;
  }

  private indexOfWordStart(pi: number, startFrom: number, es: ErrorState): number {
    if (
      startFrom >= this.name.length ||
      (this.m.mixedCase && this.isLowerCase(pi, es) && !(pi > 0 && this.isWordSeparator(pi - 1, es)))
    ) {
      return -1;
    }
    let next = startFrom;
    for (;;) {
      next = nextWord(this.name, next);
      if (next >= this.name.length) return -1;
      if (this.charEquals(pi, next, true, true, es)) return next;
    }
  }

  private indexOfIgnoreCase(fromIndex: number, pi: number, es: ErrorState): number {
    const p = this.charAt(pi, es);
    if (this.asciiName && isAsciiChar(p)) {
      const i = this.indexIgnoringCaseAscii(fromIndex, p);
      if (i !== -1) return i;
      if (this.allowTypos) {
        for (const miss of [leftMiss(p), rightMiss(p)]) {
          const j = miss === null ? -1 : this.indexIgnoringCaseAscii(fromIndex, miss);
          if (j !== -1) return j;
        }
      }
      return -1;
    }
    for (let i = Math.max(fromIndex, 0); i < this.name.length; i++) if (charEqualsIgnoreCase(this.name[i], p)) return i;
    return -1;
  }

  private indexIgnoringCaseAscii(fromIndex: number, p: Ch): number {
    const pUpper = toUpperAscii(p);
    const pLower = toLowerAscii(p);
    for (let i = fromIndex; i < this.name.length; i++) {
      const c = this.name[i];
      if (c === p || toUpperAscii(c) === pUpper || toLowerAscii(c) === pLower) return i;
    }
    return -1;
  }
}

/** TypoTolerantMatcher: a MinusculeMatcher that also accepts one adjacent-key, swapped or missing letter. */
export class TypoTolerantMatcher {
  private readonly m: Compiled;

  constructor(pattern: string, mode: Mode, hardSeparators = "") {
    const p = (pattern.endsWith("* ") ? pattern.slice(0, -2) : pattern).split("");
    const meaningful: string[] = [];
    let seenNonWildcard = false;
    let seenLowerCase = false;
    let seenUpperCaseNotImmediatelyAfterWildcard = false;
    let hasSeparators = false;
    for (const c of p) {
      if (isLower(c)) seenLowerCase = true;
      if (seenNonWildcard && isUpper(c)) seenUpperCaseNotImmediatelyAfterWildcard = true;
      if (!isWildcardChar(c)) {
        seenNonWildcard = true;
        meaningful.push(oneChar(c, c.toLowerCase()), oneChar(c, c.toUpperCase()));
      }
      if (seenNonWildcard && isSeparatorChar(c)) hasSeparators = true;
    }
    this.m = {
      pattern: p,
      mode,
      hardSeparators,
      mixedCase: seenLowerCase && seenUpperCaseNotImmediatelyAfterWildcard,
      hasSeparators,
      hasDots: p.includes("."),
      lower: p.map(isLower),
      upper: p.map(isUpper),
      separator: p.map(isSeparatorChar),
      toU: p.map((c) => oneChar(c, c.toUpperCase())),
      toL: p.map((c) => oneChar(c, c.toLowerCase())),
      meaningful,
    };
  }

  get pattern(): string {
    return this.m.pattern.join("");
  }

  matches(name: string): boolean {
    return this.match(name) !== null;
  }

  /** The matched ranges with their typo counts, or null when the name does not match. */
  match(name: string): MatchedFragment[] | null {
    if (name.length < this.m.meaningful.length / 2) return null;
    const ascii = isAscii(name);
    return new Session(this.m, name, false, ascii).matchingFragments() ?? new Session(this.m, name, true, ascii).matchingFragments();
  }

  /** The deprecated matchingFragments: the matched ranges without typo counts. */
  matchingFragments(name: string): Fragment[] | null {
    return this.match(name)?.map(([from, to]): Fragment => [from, to]) ?? null;
  }

  matchingDegree(name: string, valueStartCaseMatch = false, fragments: MatchedFragment[] | null = this.match(name)): number {
    if (fragments === null) return INT_MIN;
    if (fragments.length === 0) return 0;
    const { pattern, lower, upper } = this.m;
    const first = fragments[0]!;
    const startMatch = first[0] === 0;
    const valuedStartMatch = startMatch && valueStartCaseMatch;
    let errors = 0;
    let matchingCase = 0;
    let p = -1;
    let skippedHumps = 0;
    let nextHumpStart = 0;
    let humpStartMatchedUpperCase = false;
    for (const range of fragments) {
      for (let i = range[0]; i < range[1]; i++) {
        const afterGap = i === range[0] && first !== range;
        let isHumpStart = false;
        while (nextHumpStart <= i) {
          if (nextHumpStart === i) isHumpStart = true;
          else if (afterGap) skippedHumps++;
          nextHumpStart = nextWord(name, nextHumpStart);
        }
        const c = name[i]!;
        const from = p + 1;
        p = pattern.findIndex((pc, k) => k >= from && charEqualsIgnoreCase(pc, c));
        if (p < 0) break;
        if (isHumpStart) humpStartMatchedUpperCase = c === pattern[p] && upper[p] === true;
        matchingCase += evaluateCaseMatching(pattern, valuedStartMatch, p, humpStartMatchedUpperCase, i, afterGap, isHumpStart, c, lower, upper);
      }
      errors += Math.trunc(2000 * (range[2] / (range[1] - range[0])) ** 2);
    }
    const startIndex = first[0];
    const afterSeparator = indexOfAny(name, this.m.hardSeparators, 0, startIndex) >= 0;
    const wordStart = startIndex === 0 || (isWordStart(name, startIndex) && !isWordStart(name, startIndex - 1));
    const finalMatch = fragments.at(-1)![1] === name.length;
    return (
      (wordStart ? 1000 : 0) +
      matchingCase -
      fragments.length -
      skippedHumps * 10 -
      errors +
      (afterSeparator ? 0 : 2) +
      (startMatch ? 1 : 0) +
      (finalMatch ? 1 : 0)
    );
  }
}
