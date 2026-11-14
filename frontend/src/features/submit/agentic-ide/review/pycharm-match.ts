// A port of the IntelliJ Community completion matcher and sorter, as PyCharm runs it with its
// defaults: middle matching on, and "Match case: first letter only". Sources, by name:
// MinusculeMatcherImpl.kt, MinusculeMatcher.kt (calculateHumpedMatchingScore), NameUtilCore.kt,
// CamelHumpMatcher.java, PreferStartMatching.java, RealPrefixMatchingWeigher.java,
// LiftShorterItemsClassifier.java and BaseCompletionLookupArranger.java.

export type Fragment = [from: number, to: number];

// An index past either end gives undefined, which no class contains.
export type Ch = string | undefined;
const test = (re: RegExp) => (c: Ch) => c !== undefined && re.test(c);
export const isUpper = test(/\p{Lu}/u);
export const isLower = test(/\p{Ll}/u);
const isLetter = test(/\p{L}/u);
export const isDigit = test(/\p{Nd}/u);
export const isLetterOrDigit = (c: Ch) => isLetter(c) || isDigit(c);
export const isWildcardChar = (c: Ch) => c === " " || c === "*";
export const isSeparatorChar = (c: Ch) => c !== undefined && (/\s/.test(c) || "_-:+.".includes(c));

/** NameUtilCore.isWordStart, without the kana and ideograph rules. */
export function isWordStart(text: string, i: number): boolean {
  const cur = text[i];
  const prev = i > 0 ? text[i - 1] : undefined;
  if (isUpper(cur)) {
    // Inside an all-caps word, only the letter before a lowercase one starts a word.
    if (prev !== undefined && isUpper(prev)) return i + 1 < text.length && isLower(text[i + 1]);
    return true;
  }
  if (isDigit(cur)) return true;
  if (!isLetter(cur)) return false;
  return i === 0 || !isLetterOrDigit(text[i - 1]);
}

/** NameUtilCore.nextWord: the end of the hump that starts at `start`. */
export function nextWord(text: string, start: number): number {
  if (!isLetterOrDigit(text[start])) return start + 1;
  let i = start;
  while (i < text.length && isDigit(text[i])) i++;
  if (i > start) return i;
  while (i < text.length && isUpper(text[i])) i++;
  if (i > start + 1) {
    // A run of capitals is one hump; its last capital starts the next word when a letter follows.
    if (i === text.length || !isLetter(text[i])) return i;
    return i - 1;
  }
  if (i === start) i++;
  while (i < text.length && isLetter(text[i]) && !isWordStart(text, i)) i++;
  return i;
}

export type Mode = "first-letter" | "ignore-case";

/** MinusculeMatcherImpl with no hard separators. */
class MinusculeMatcher {
  private readonly p: string[];
  private readonly lower: boolean[];
  private readonly upper: boolean[];
  private readonly sep: boolean[];
  private readonly toU: string[];
  private readonly toL: string[];
  private readonly meaningful: string[] = [];
  private readonly mixedCase: boolean;
  private readonly hasDots: boolean;

  private readonly mode: Mode;

  constructor(pattern: string, mode: Mode) {
    this.mode = mode;
    this.p = [...(pattern.endsWith("* ") ? pattern.slice(0, -2) : pattern)];
    this.lower = this.p.map(isLower);
    this.upper = this.p.map(isUpper);
    this.sep = this.p.map(isSeparatorChar);
    this.toU = this.p.map((c) => c.toUpperCase());
    this.toL = this.p.map((c) => c.toLowerCase());
    let seenNonWildcard = false;
    let seenLower = false;
    let seenUpperAfter = false;
    for (const [k, c] of this.p.entries()) {
      if (this.lower[k]) seenLower = true;
      if (seenNonWildcard && this.upper[k]) seenUpperAfter = true;
      if (!isWildcardChar(c)) {
        seenNonWildcard = true;
        this.meaningful.push(c.toLowerCase(), c.toUpperCase());
      }
    }
    this.hasDots = this.p.includes(".");
    this.mixedCase = seenLower && seenUpperAfter;
  }

  match(name: string): Fragment[] | null {
    if (name.length < this.meaningful.length / 2) return null;
    if (this.p.length > 100) return this.matchBySubstring(name);
    if (!containsMeaningfulInOrder(name, this.meaningful)) return null;
    return this.matchWildcards(name, 0, 0)?.reverse() ?? null;
  }

  matchingDegree(name: string, valueStartCaseMatch: boolean, fragments: Fragment[] | null): number {
    return humpedScore(this.p, name, valueStartCaseMatch, fragments, this.lower, this.upper);
  }

  private matchBySubstring(name: string): Fragment[] | null {
    const count = this.meaningful.length / 2;
    if (name.length < count) return null;
    if (this.isPatternChar(0, "*")) {
      for (let i = 0; i <= name.length - count; i++) {
        const len = this.meaningfulAt(name, i);
        if (len !== 0) return [[i, i + len]];
      }
      return null;
    }
    const len = this.meaningfulAt(name, 0);
    return len !== 0 ? [[0, len]] : null;
  }

  private meaningfulAt(name: string, at: number): number {
    let m = 0;
    let pos = at;
    while (pos < name.length && m + 1 < this.meaningful.length) {
      const c = name[pos];
      if (c === this.meaningful[m] || c === this.meaningful[m + 1]) {
        m += 2;
        pos++;
      } else if (isWildcardChar(c)) pos++;
      else return 0;
    }
    return pos - at;
  }

  private matchWildcards(name: string, pi: number, ni: number): Fragment[] | null {
    if (ni < 0) return null;
    if (!this.isWildcard(pi)) return pi === this.p.length ? [] : this.matchFragment(name, pi, ni);
    do pi++;
    while (this.isWildcard(pi));
    if (pi === this.p.length) {
      // A trailing space matches when the pattern ends with the last word part, or only its first hump.
      if (
        this.isTrailingSpace() &&
        ni !== name.length &&
        (pi < 2 || !(this.upper[pi - 2] || isDigit(this.p[pi - 2])))
      ) {
        const space = name.indexOf(" ", ni);
        return space >= 0 ? [[space, space + 1]] : null;
      }
      return [];
    }
    return this.matchSkippingWords(name, pi, this.findNext(name, ni, pi), true);
  }

  private isTrailingSpace() {
    return this.isPatternChar(this.p.length - 1, " ");
  }

  private matchSkippingWords(name: string, pi: number, ni: number, allowSpecial: boolean): Fragment[] | null {
    let maxFound = 0;
    while (ni >= 0) {
      const len = this.seemsLikeFragmentStart(name, pi, ni) ? this.maxMatchingFragment(name, pi, ni) : 0;
      // A fragment no longer than one already tried cannot match the rest of the pattern either.
      if (len > maxFound || (ni + len === name.length && this.isTrailingSpace())) {
        if (!this.isMiddleMatch(name, pi, ni)) maxFound = len;
        const ranges = this.matchInsideFragment(name, pi, ni, len);
        if (ranges) return ranges;
      }
      const next = this.findNext(name, ni + 1, pi);
      ni = allowSpecial ? next : this.checkSpecialChars(name, ni + 1, next, pi);
    }
    return null;
  }

  private findNext(name: string, startAt: number, pi: number): number {
    return !this.isPatternChar(pi - 1, "*") && !this.sep[pi]
      ? this.indexOfWordStart(name, pi, startAt)
      : this.indexOfIgnoreCase(name, startAt, pi);
  }

  private checkSpecialChars(name: string, start: number, end: number, pi: number): number {
    if (end < 0) return -1;
    // A typed dot does not skip other dots between humps.
    if (this.hasDots && !this.isPatternChar(pi - 1, ".") && name.slice(start, end).includes(".")) return -1;
    return end;
  }

  private seemsLikeFragmentStart(name: string, pi: number, ni: number): boolean {
    return !this.upper[pi] || isUpper(name[ni]) || isWordStart(name, ni) || !this.mixedCase;
  }

  private charEquals(pc: Ch, pi: number, c: Ch): boolean {
    return pc === c || this.toL[pi] === c || this.toU[pi] === c;
  }

  private matchFragment(name: string, pi: number, ni: number): Fragment[] | null {
    const len = this.maxMatchingFragment(name, pi, ni);
    return len === 0 ? null : this.matchInsideFragment(name, pi, ni, len);
  }

  private maxMatchingFragment(name: string, pi: number, ni: number): number {
    if (!this.isFirstCharMatching(name, ni, pi)) return 0;
    let i = 1;
    while (ni + i < name.length && pi + i < this.p.length) {
      const c = name[ni + i];
      if (!this.charEquals(this.p[pi + i], pi + i, c)) {
        // A pattern "12" does not match "1x2" by skipping a digit.
        if (isDigit(this.p[pi + i]) && isDigit(this.p[pi + i - 1]) && isDigit(c)) return 0;
        break;
      }
      i++;
    }
    return i;
  }

  private matchInsideFragment(name: string, pi: number, ni: number, len: number): Fragment[] | null {
    // Exact middle matches need 3 chars or more, to keep out irrelevant matches.
    const min = this.isMiddleMatch(name, pi, ni) ? 3 : 1;
    return this.improveCamelHumps(name, pi, ni, len, min) ?? this.longestPrefix(name, pi, ni, len, min);
  }

  private isMiddleMatch(name: string, pi: number, ni: number): boolean {
    return (
      this.isPatternChar(pi - 1, "*") &&
      !this.isWildcard(pi + 1) &&
      isLetterOrDigit(name[ni]) &&
      !isWordStart(name, ni)
    );
  }

  private longestPrefix(name: string, pi: number, ni: number, len: number, min: number): Fragment[] | null {
    if (pi + len >= this.p.length) return [[ni, ni + len]];
    for (let i = len; i >= min || (i > 0 && this.isWildcard(pi + i)); i--) {
      let ranges: Fragment[] | null;
      if (this.isWildcard(pi + i)) ranges = this.matchWildcards(name, pi + i, ni + i);
      else {
        let next = this.findNext(name, ni + i + 1, pi + i);
        next = this.checkSpecialChars(name, ni + i, next, pi + i);
        ranges = next >= 0 ? this.matchSkippingWords(name, pi + i, next, false) : null;
      }
      if (ranges) return appendRange(ranges, ni, i);
    }
    return null;
  }

  // For "CU" and "CurrentUser", "Cu" already matches, but an uppercase "U" further on scores better.
  private improveCamelHumps(name: string, pi: number, ni: number, max: number, min: number): Fragment[] | null {
    for (let i = min; i < max; i++) {
      if (this.upper[pi + i] && this.p[pi + i] !== name[ni + i]) {
        const ranges = this.matchWildcards(name, pi + i, this.indexOfWordStart(name, pi + i, ni + i));
        if (ranges) return appendRange(ranges, ni, i);
      }
    }
    return null;
  }

  private isFirstCharMatching(name: string, ni: number, pi: number): boolean {
    if (ni >= name.length) return false;
    if (!this.charEquals(this.p[pi], pi, name[ni])) return false;
    return !(
      this.mode === "first-letter" &&
      (pi === 0 || (pi === 1 && this.isWildcard(0))) &&
      (this.upper[pi] || this.lower[pi]) &&
      this.upper[pi] !== isUpper(name[0])
    );
  }

  private isWildcard(pi: number): boolean {
    return pi >= 0 && pi < this.p.length && isWildcardChar(this.p[pi]);
  }

  private isPatternChar(pi: number, c: string): boolean {
    return this.p[pi] === c;
  }

  private indexOfWordStart(name: string, pi: number, from: number): number {
    if (from >= name.length || (this.mixedCase && this.lower[pi] && !(pi > 0 && this.sep[pi - 1]))) return -1;
    const special = !isLetterOrDigit(this.p[pi]);
    for (let i = from; ; i++) {
      i = this.indexOfIgnoreCase(name, i, pi);
      if (i < 0) return -1;
      if (special || isWordStart(name, i)) return i;
    }
  }

  private indexOfIgnoreCase(name: string, from: number, pi: number): number {
    for (let i = from; i < name.length; i++) if (name[i] === this.toU[pi] || name[i] === this.toL[pi]) return i;
    return -1;
  }
}

// The fragments come back from the end of the name, so a range that touches the next one merges into it.
function appendRange(ranges: Fragment[], from: number, length: number): Fragment[] {
  const last = ranges.at(-1);
  if (last && last[0] === from + length) last[0] = from;
  else ranges.push([from, from + length]);
  return ranges;
}

export function containsMeaningfulInOrder(name: string, chars: string[]): boolean {
  let ni = 0;
  for (let m = 0; m + 1 < chars.length; m += 2) {
    if (ni >= name.length) return false;
    const a = name.indexOf(chars[m]!, ni);
    if (a === ni) {
      ni = a + 1;
      continue;
    }
    const b = chars[m] === chars[m + 1] ? -1 : name.indexOf(chars[m + 1]!, ni);
    if (a < 0 && b < 0) return false;
    ni = (a >= 0 && b >= 0 ? Math.min(a, b) : Math.max(a, b)) + 1;
  }
  return true;
}

/** MinusculeMatcher.calculateHumpedMatchingScore with no hard separators. */
function humpedScore(
  pattern: string[],
  name: string,
  valueStartCaseMatch: boolean,
  fragments: Fragment[] | null,
  lower: boolean[],
  upper: boolean[],
): number {
  if (fragments === null) return Number.MIN_SAFE_INTEGER;
  if (fragments.length === 0) return 0;
  const first = fragments[0]!;
  const startMatch = first[0] === 0;
  const valuedStartMatch = startMatch && valueStartCaseMatch;
  let matchingCase = 0;
  let p = -1;
  let skippedHumps = 0;
  let nextHumpStart = 0;
  let humpStartMatchedUpper = false;
  outer: for (const range of fragments) {
    for (let i = range[0]; i < range[1]; i++) {
      const afterGap = i === range[0] && range !== first;
      let isHumpStart = false;
      while (nextHumpStart <= i) {
        if (nextHumpStart === i) isHumpStart = true;
        else if (afterGap) skippedHumps++;
        nextHumpStart =
          nextHumpStart < name.length && isDigit(name[nextHumpStart])
            ? nextHumpStart + 1
            : nextWord(name, nextHumpStart);
      }
      const c = name[i]!;
      p = indexOfIgnoreCase(pattern, c, p + 1);
      if (p < 0) break outer;
      if (isHumpStart) humpStartMatchedUpper = c === pattern[p] && upper[p] === true;
      if (afterGap && isHumpStart && lower[p]) matchingCase -= 10;
      else if (c === pattern[p]) {
        if (upper[p]) matchingCase += 50;
        else if (i === 0 && valuedStartMatch) matchingCase += 150;
        else if (isHumpStart) matchingCase += 1;
      } else if (isHumpStart) matchingCase -= 1;
      else if (lower[p] && humpStartMatchedUpper) matchingCase -= 1;
    }
  }
  const start = first[0];
  const wordStart = start === 0 || (isWordStart(name, start) && !isWordStart(name, start - 1));
  const finalMatch = fragments.at(-1)![1] === name.length;
  // With no hard separators, the "not after a separator" bonus of 2 always applies.
  return (wordStart ? 1000 : 0) + matchingCase - fragments.length - skippedHumps * 10 + 2 + (startMatch ? 1 : 0) + (finalMatch ? 1 : 0);
}

function indexOfIgnoreCase(pattern: string[], c: string, from: number): number {
  const l = c.toLowerCase();
  for (let i = from; i < pattern.length; i++) if (pattern[i]!.toLowerCase() === l) return i;
  return -1;
}

const skipUnderscores = (s: string) => s.length - s.replace(/^_+/, "").length;

const caseDiffers = (a: Ch, b: Ch) => isLower(a) !== isLower(b) || isUpper(a) !== isUpper(b);

/** CamelHumpMatcher with middle matching, in the FIRST_LETTER case mode. */
export class CamelHumpMatcher {
  private readonly main: MinusculeMatcher;
  private readonly anyCase: MinusculeMatcher;

  readonly prefix: string;

  constructor(prefix: string) {
    this.prefix = prefix;
    const pattern = prefix ? `*${  prefix.replaceAll(".", ". ").trim()}` : prefix;
    this.main = new MinusculeMatcher(pattern, "first-letter");
    this.anyCase = new MinusculeMatcher(pattern, "ignore-case");
  }

  prefixMatches(name: string): boolean {
    if (name.startsWith("_")) {
      const n = skipUnderscores(name);
      const p = skipUnderscores(this.prefix);
      if (n < name.length && p < this.prefix.length && caseDiffers(name[n], this.prefix[p])) return false;
    }
    return this.main.match(name) !== null;
  }

  isStartMatch(name: string): boolean {
    const ranges = this.anyCase.match(name);
    return ranges !== null && (ranges.length === 0 || skipUnderscores(name) >= ranges[0]![0]);
  }

  matchingFragments(name: string): Fragment[] | null {
    return this.main.match(name);
  }

  matchingDegree(name: string): number {
    const underscoreEnd = skipUnderscores(name);
    if (underscoreEnd > 0) {
      const ci = this.anyCase.match(name);
      if (ci && ci.length > 0) {
        const s = ci[0]![0];
        if (s > 0 && s <= underscoreEnd) {
          const rest = name.slice(s);
          return this.anyCase.matchingDegree(rest, true, this.anyCase.match(rest)) - 1;
        }
      }
    }
    return this.main.matchingDegree(name, true, this.main.match(name));
  }
}

// NaturalComparator: digit runs compare by value, and case breaks ties only.
const natural = new Intl.Collator("en", { numeric: true, sensitivity: "base" });
const naturalCompare = (a: string, b: string) => natural.compare(a, b) || (a < b ? -1 : a > b ? 1 : 0);

export type Ranked<T> = { item: T; startMatch: boolean; fragments: Fragment[] };

/**
 * Filter and order the items as PyCharm's lookup does: start matches first, then by matching
 * degree, then by name. Shorter items whose name prefixes a longer one follow it, and items
 * equal to the typed prefix go to the top, with the same case first.
 */
function compareRecency(a: number, b: number): number {
  return a === b ? 0 : a < b ? -1 : 1;
}

export function arrange<T>(
  prefix: string,
  items: T[],
  label: (item: T) => string,
  recency: (name: string) => number = () => Infinity,
): Array<Ranked<T>> {
  const matcher = new CamelHumpMatcher(prefix);
  const scored = items
    .filter((item) => matcher.prefixMatches(label(item)))
    .map((item) => {
      const name = label(item);
      return {
        item,
        name,
        startMatch: matcher.isStartMatch(name),
        degree: matcher.matchingDegree(name),
        fragments: matcher.matchingFragments(name) ?? [],
      };
    });
  type Scored = (typeof scored)[number];
  const byRelevance = (a: Scored, b: Scored) => b.degree - a.degree || naturalCompare(a.name, b.name);
  const ordered: Scored[] = [];
  for (const group of [scored.filter((s) => s.startMatch), scored.filter((s) => !s.startMatch)]) {
    group.sort(byRelevance);
    const done = new Set<Scored>();
    for (const longer of group) {
      if (done.has(longer)) continue;
      done.add(longer);
      ordered.push(longer);
      // LiftShorterItemsClassifier, liftBefore=false: shorter items whose name prefixes this one follow it.
      const shorter = group.filter(
        (s) => !done.has(s) && s.name.length > 0 && s.name.length < longer.name.length && longer.name.startsWith(s.name),
      );
      for (const s of shorter) done.add(s);
      ordered.push(...shorter);
    }
  }
  const exact = (s: Scored) => (s.name === prefix ? 0 : s.name.toLowerCase() === prefix.toLowerCase() ? 1 : 2);
  // LookupStatisticsWeigher: inside a start or middle group, a name picked more recently comes first.
  // A stable sort keeps the relevance order inside each rank.
  const group = (s: Scored) => (s.startMatch ? 0 : 1);
  return ordered
    .map((s, i) => ({ s, i, used: recency(s.name) }))
    .sort((a, b) => exact(a.s) - exact(b.s) || group(a.s) - group(b.s) || compareRecency(a.used, b.used) || a.i - b.i)
    .map(({ s }) => ({ item: s.item, startMatch: s.startMatch, fragments: s.fragments }));
}
