import {
  ConflictPair, ContextResponse, Graph, GraphEdge, GraphNode, Result,
} from './types';

/**
 * Merge the shared plane's answer with the private plane's, on this machine.
 *
 * The two never meet on a server. The company service ranks and synthesizes shared
 * threads; the local connector ranks the person's private threads; this function is
 * the only place the two lists exist side by side, and it runs in the editor.
 *
 * ## Why ranking is by position, not by score
 *
 * The two `score` fields are not on the same scale and cannot be made so. A shared
 * semantic score comes out of reciprocal rank fusion — a rank-based number around
 * 0.016 that is meaningless as a magnitude — and then through an LLM reranker. A
 * private semantic score is a cosine similarity around 0.8. Sorting the concatenation
 * by `score` would put every private result above every shared one for reasons that
 * have nothing to do with relevance.
 *
 * So the merge uses the one thing both planes genuinely agree on: **the order each
 * produced**. Exact matches (asserted structurally, identically on both sides) come
 * first, then semantic ones, and within each tier the two lists are interleaved by
 * rank, shared first on a tie. It is explainable in a sentence to the person reading
 * it, which a blended pseudo-score would not be.
 *
 * ## Why citations have to be renumbered
 *
 * The shared synthesis cites its evidence as `[1]`, `[2]`. Interleaving moves those
 * results, so every citation in the synthesis, every conflict pair and every graph
 * node's `citation` is remapped to its new position. Skipping this is not a cosmetic
 * bug: a synthesis that cites `[2]` while `[2]` is now an unrelated private thread
 * attributes a claim to evidence that does not support it.
 */

/** Same conversation seen from both planes? Permalinks are the stable identity. */
function identity(result: Result): string {
  return result.permalink || `${result.channel_name}:${result.id}`;
}

function interleave<T>(a: T[], b: T[]): T[] {
  const out: T[] = [];
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if (i < a.length) { out.push(a[i]); }
    if (i < b.length) { out.push(b[i]); }
  }
  return out;
}

function remapCitations(text: string, map: Map<number, number>): string {
  return text.replace(/\[(\d+)\]/g, (match, digits: string) => {
    const moved = map.get(Number(digits));
    return moved ? `[${moved}]` : match;
  });
}

function mergeGraphs(
  shared: Graph | undefined,
  local: Graph | undefined,
  sharedCitations: Map<number, number>,
  localCitations: Map<number, number>,
): Graph {
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];
  const seenEdges = new Set<string>();

  const add = (graph: Graph | undefined, citations: Map<number, number>): void => {
    if (!graph) { return; }
    for (const node of graph.nodes) {
      const citation = typeof node.data?.citation === 'number'
        ? citations.get(node.data.citation as number)
        : undefined;
      const remapped: GraphNode = citation
        ? { ...node, data: { ...node.data, citation } }
        : node;
      // A node present in both planes keeps the first (shared) copy: its scope says
      // "Workspace", which is the more consequential fact about who can see it.
      if (!nodes.has(node.id)) { nodes.set(node.id, remapped); }
    }
    for (const edge of graph.edges) {
      const key = `${edge.source}->${edge.target}:${edge.type}`;
      if (seenEdges.has(key)) { continue; }
      seenEdges.add(key);
      edges.push(edge);
    }
  };

  add(shared, sharedCitations);
  add(local, localCitations);
  return { nodes: [...nodes.values()], edges };
}

function prefixTimings(response: ContextResponse | undefined, prefix: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [stage, ms] of Object.entries(response?.timing_ms ?? {})) {
    out[`${prefix}.${stage}`] = ms;
  }
  return out;
}

export interface FusionInput {
  shared?: ContextResponse;
  local?: ContextResponse;
  /** Set when the local connector was asked and failed, so the UI can say so. */
  localError?: string;
}

export function fuse({ shared, local, localError }: FusionInput): ContextResponse {
  const sharedResults = shared?.results ?? [];
  const localOnly = (local?.results ?? []).filter((candidate) => {
    const key = identity(candidate);
    return !sharedResults.some((existing) => identity(existing) === key);
  });

  const isExact = (r: Result): boolean => r.match_type === 'exact';
  const merged = [
    ...interleave(sharedResults.filter(isExact), localOnly.filter(isExact)),
    ...interleave(sharedResults.filter((r) => !isExact(r)), localOnly.filter((r) => !isExact(r))),
  ];

  const sharedCitations = new Map<number, number>();
  const localCitations = new Map<number, number>();
  merged.forEach((result, index) => {
    const sharedIndex = sharedResults.indexOf(result);
    if (sharedIndex >= 0) {
      sharedCitations.set(sharedIndex + 1, index + 1);
      return;
    }
    const localIndex = (local?.results ?? []).indexOf(result);
    if (localIndex >= 0) { localCitations.set(localIndex + 1, index + 1); }
  });

  const conflicts: ConflictPair[] = (shared?.conflicts ?? []).map((pair) => ({
    ...pair,
    a: sharedCitations.get(pair.a) ?? pair.a,
    b: sharedCitations.get(pair.b) ?? pair.b,
  }));

  const messages = [shared?.message, local?.message].filter(
    (m): m is string => Boolean(m),
  );
  // Both planes finding nothing is one fact, not two sentences saying it twice.
  const message = merged.length > 0
    ? (localError ? `Private results unavailable: ${localError}` : null)
    : [...new Set(messages)].join(' ') || null;

  return {
    synthesis: shared?.synthesis ? remapCitations(shared.synthesis, sharedCitations) : null,
    results: merged,
    blame: shared?.blame ?? local?.blame ?? {
      authors: [], dominant_sha: null, all_shas: [], pr_number: null, pr_numbers: [],
      commit_date: null, commit_ts: null, uncommitted: false, commits: [],
    },
    graph: mergeGraphs(shared?.graph, local?.graph, sharedCitations, localCitations),
    conflicts,
    timing_ms: { ...prefixTimings(shared, 'shared'), ...prefixTimings(local, 'local') },
    message,
    error: shared?.error ?? null,
  };
}

/** How many of each plane ended up in the merged list — shown in the footer. */
export function scopeCounts(results: Result[]): { workspace: number; private: number } {
  return {
    workspace: results.filter((r) => r.scope !== 'user_private').length,
    private: results.filter((r) => r.scope === 'user_private').length,
  };
}
