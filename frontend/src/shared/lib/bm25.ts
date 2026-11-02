/**
 * A small lexical BM25 index for the search boxes (app top bar and docs).
 *
 * Kept import-free so the Node unit test can load it directly. There are no
 * embeddings on purpose: matches must appear on every keystroke, offline.
 */

export const BM25_K1 = 1.2;
export const BM25_B = 0.75;

/** A partial last token only expands when it has at least this many characters. */
const MIN_PREFIX_LENGTH = 2;
/** A prefix-only hit counts a little less than a whole-word hit. */
const PREFIX_DISCOUNT = 0.9;

/**
 * The most query words a box treats as a lookup. A longer query reads as a
 * sentence or a question, so Enter sends it to the agent instead.
 */
export const MAX_LOOKUP_TERMS = 3;
/** The lowest score a top match needs for Enter to open it. */
export const MIN_OPEN_SCORE = 1;

// Han and kana are written without spaces, so their runs are split into
// overlapping character pairs (the usual CJK bigram index).
const CJK_RUN = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
const HEBREW_WORD = /^\p{Script=Hebrew}+$/u;
// One-letter Hebrew prefixes (vav, he, bet, kaf, lamed, mem, shin): and, the,
// in, as, to, from, that. Code points, because Hebrew literals live in the catalogs.
const HEBREW_PREFIXES = String.fromCharCode(0x5d5, 0x5d4, 0x5d1, 0x5db, 0x5dc, 0x5de, 0x5e9);

/**
 * Split text into lowercase search tokens.
 *
 * Diacritics and Hebrew vowel points are removed, so "café" matches "cafe"
 * and pointed Hebrew matches plain Hebrew.
 */
export function tokenize(text: string): string[] {
  const words = text
    .normalize("NFKD")
    .replace(/\p{M}+/gu, "")
    .toLocaleLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
  const tokens: string[] = [];
  for (const word of words) {
    if (!CJK_RUN.test(word) || word.length < 2) {
      tokens.push(word);
      continue;
    }
    for (let i = 0; i < word.length - 1; i++) tokens.push(word.slice(i, i + 2));
  }
  return tokens;
}

/** The token, plus the token without a one-letter Hebrew prefix ("החשבון" → "חשבון"). */
function variants(token: string): string[] {
  if (token.length >= 4 && HEBREW_WORD.test(token) && HEBREW_PREFIXES.includes(token.charAt(0))) {
    return [token, token.slice(1)];
  }
  return [token];
}

export interface Bm25Field {
  text: string;
  /** How many times the field's words count (a title outweighs body text). */
  weight?: number;
}

export interface Bm25Doc<T> {
  item: T;
  fields: Bm25Field[];
}

export interface Bm25Index<T> {
  docs: Array<{ item: T; tf: Map<string, number>; length: number }>;
  df: Map<string, number>;
  avgLength: number;
  /** Every indexed term, for prefix expansion of the partial last word. */
  vocabulary: string[];
}

export interface Bm25Hit<T> {
  item: T;
  score: number;
  /** How many distinct query words this item matched. */
  matched: number;
}

/** Build an index over the documents' weighted fields. */
export function buildBm25Index<T>(docs: ReadonlyArray<Bm25Doc<T>>): Bm25Index<T> {
  const df = new Map<string, number>();
  let total = 0;
  const indexed = docs.map(({ item, fields }) => {
    const tf = new Map<string, number>();
    let length = 0;
    for (const field of fields) {
      const weight = field.weight ?? 1;
      for (const token of tokenize(field.text)) {
        for (const term of variants(token)) tf.set(term, (tf.get(term) ?? 0) + weight);
        length += weight;
      }
    }
    for (const term of tf.keys()) df.set(term, (df.get(term) ?? 0) + 1);
    total += length;
    return { item, tf, length };
  });
  return {
    docs: indexed,
    df,
    avgLength: indexed.length ? total / indexed.length : 0,
    vocabulary: [...df.keys()],
  };
}

/** The BM25 weight of one term in one document. */
function termScore<T>(index: Bm25Index<T>, doc: Bm25Index<T>["docs"][number], term: string): number {
  const tf = doc.tf.get(term);
  if (!tf) return 0;
  const n = index.df.get(term) ?? 0;
  const idf = Math.log(1 + (index.docs.length - n + 0.5) / (n + 0.5));
  const norm = 1 - BM25_B + BM25_B * (doc.length / (index.avgLength || 1));
  return (idf * tf * (BM25_K1 + 1)) / (tf + BM25_K1 * norm);
}

/**
 * Rank the documents for `query`, best first. Only documents that match at
 * least one query word are returned.
 *
 * The last word may be partial while the user types, so it also matches
 * indexed terms that start with it.
 */
export function searchBm25<T>(index: Bm25Index<T>, query: string): Array<Bm25Hit<T>> {
  const words = [...new Set(tokenize(query))];
  if (words.length === 0) return [];
  const lastWord = words[words.length - 1];
  const expanded = new Map<string, Array<{ term: string; factor: number }>>();
  for (const word of words) {
    const terms = variants(word).map((term) => ({ term, factor: 1 }));
    if (word === lastWord && word.length >= MIN_PREFIX_LENGTH) {
      for (const term of index.vocabulary) {
        if (term !== word && term.startsWith(word)) terms.push({ term, factor: PREFIX_DISCOUNT });
      }
    }
    expanded.set(word, terms);
  }

  const hits: Array<Bm25Hit<T>> = [];
  for (const doc of index.docs) {
    let score = 0;
    let matched = 0;
    for (const word of words) {
      // The best form of each word counts once, so a prefix that matches many
      // terms in one document does not outweigh a whole word.
      let best = 0;
      for (const { term, factor } of expanded.get(word) ?? []) {
        best = Math.max(best, termScore(index, doc, term) * factor);
      }
      if (best > 0) {
        score += best;
        matched += 1;
      }
    }
    if (matched > 0) hits.push({ item: doc.item, score, matched });
  }
  return hits.sort((a, b) => b.score - a.score || b.matched - a.matched);
}

/**
 * Whether Enter should open the top match instead of asking the agent.
 *
 * A strong match is a short lookup ("billing", "api key") whose every word
 * the top item matches. A sentence ("i want to start an optimization") goes
 * to the agent even when some of its words match a page.
 */
export function isStrongMatch<T>(query: string, top: Bm25Hit<T> | undefined): boolean {
  if (!top) return false;
  const words = new Set(tokenize(query)).size;
  if (words === 0 || words > MAX_LOOKUP_TERMS) return false;
  return top.matched === words && top.score >= MIN_OPEN_SCORE;
}
