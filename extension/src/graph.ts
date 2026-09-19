import { Graph, GraphEdge, GraphNode, NodeType } from './types';

// The provenance graph, drawn as a vertical timeline rather than a layered DAG.
//
// This view lives in a ~340px activity-bar sidebar, and the evidence it shows is
// inherently chronological: a ticket, then a PR, then the commit, then the threads
// that argued about it, then the incident. A layered DAG in a narrow column spreads
// that into scattered boxes; a single dated rail reads top-to-bottom the way the
// story actually happened, and every card is visibly attached to it.
//
// Two structural choices follow from that:
//   * Person and Ticket nodes are attributes of an event, not events of their own.
//     They render as chips inside their parent's card instead of as free-floating
//     rectangles hanging off the side.
//   * The rail carries chronology. The real graph edges are drawn as arcs in the
//     right gutter, unlabelled until you hover -- the structure is still there, it
//     just is not competing with the timeline for attention.
//
// Interactivity (pan, zoom, hover-to-trace, click-through) is wired up from view.ts's
// single webview <script> block, since a webview may only call `acquireVsCodeApi()`
// once and this markup has to share that script's scope.

const MARGIN = 10;
const RAIL_X = 18;
const DOT_R = 5;
const CARD_X = 40;
const ROW_GAP = 14;
const ANCHOR_GAP = 24;      // the selection is not a dated event; set it apart

// Arc lanes. Every arc used to bow by `min(48, 12 + span * 8)`, which saturated:
// on a four-PR supersede chain, ten of twenty-two arcs shared an identical 48px
// bow and fused into one unreadable band. Arcs are now assigned to discrete
// lanes by interval-overlap, so two arcs share a bow only when they cannot
// collide vertically.
const LANE_BASE = 18;       // bow of the innermost lane
const LANE_STEP = 16;       // spacing between lanes
const LANE_PAD = 16;        // breathing room past the outermost lane

interface Metrics {
  cardW: number;
  laneStep: number;
  labelsAtRest: boolean;
  maxLanes: number;
}

const COMPACT: Metrics = { cardW: 250, laneStep: LANE_STEP, labelsAtRest: false, maxLanes: 5 };
const FULL: Metrics = { cardW: 420, laneStep: 34, labelsAtRest: true, maxLanes: 14 };

export interface RenderOptions {
  /** Editor-tab timeline: wider cards, more lanes, labels on without hovering. */
  fullscreen?: boolean;
}

interface TypeStyle {
  cssClass: string;
  icon: string;
  description: string;
}

const TYPE_STYLE: Record<NodeType, TypeStyle> = {
  Code: { cssClass: 'n-code', icon: '\u{1F9E9}', description: 'Your selection' },
  Commit: { cssClass: 'n-commit', icon: '\u{1F517}', description: 'git blame result' },
  PullRequest: { cssClass: 'n-pr', icon: '\u{1F500}', description: 'Resolved from the commit' },
  SlackThread: { cssClass: 'n-slack', icon: '\u{1F4AC}', description: 'Retrieved evidence' },
  Ticket: { cssClass: 'n-ticket', icon: '\u{1F3AB}', description: 'Mock ticket tracker' },
  SentryIssue: { cssClass: 'n-sentry', icon: '\u{1F6A8}', description: 'Mock incident tracker' },
  Person: { cssClass: 'n-person', icon: '\u{1F464}', description: 'git blame author' },
};

/** Attributes of an event, not events in their own right -- these become chips. */
const SATELLITE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(['Person', 'Ticket']);

export const LEGEND_TYPES: { type: NodeType; style: TypeStyle }[] =
  (Object.keys(TYPE_STYLE) as NodeType[]).map((type) => ({ type, style: TYPE_STYLE[type] }));

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeAttr(value: string): string {
  return escapeHtml(value).replace(/'/g, '&#39;');
}

function truncate(value: string, limit: number): string {
  const flat = value.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}

/** `ts` is unix seconds, resolved server-side so both sides order identically; the
 *  string fields are the fallback for a node the backend could not date. Those
 *  disagree on format: `YYYY-MM-DD` from git blame and from the Slack export, full
 *  ISO-8601 from the GitHub and Sentry adapters. */
function timestampOf(node: GraphNode): number | null {
  const data = node.data ?? {};
  const seconds = data.ts;
  if (typeof seconds === 'number' && Number.isFinite(seconds)) { return seconds * 1000; }
  for (const key of ['date', 'merged_at', 'first_seen', 'commit_date', 'created_at']) {
    const raw = data[key];
    if (typeof raw !== 'string' || !raw) { continue; }
    const parsed = Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw}T00:00:00Z` : raw);
    if (!Number.isNaN(parsed)) { return parsed; }
  }
  return null;
}

function dateLabel(ts: number | null): string {
  return ts === null ? '' : new Date(ts).toISOString().slice(0, 10);
}

function subtitleFor(node: GraphNode): string {
  const data = node.data ?? {};
  if (typeof data.title === 'string' && data.title) { return data.title; }
  if (typeof data.summary === 'string' && data.summary) { return data.summary; }
  if (Array.isArray(data.authors) && data.authors.length > 0) { return data.authors.join(', '); }
  if (typeof data.author === 'string' && data.author) { return data.author; }
  if (typeof data.status === 'string' && data.status) { return data.status; }
  return '';
}

function citationOf(node: GraphNode): number | null {
  const value = (node.data ?? {}).citation;
  return typeof value === 'number' ? value : null;
}

interface Row {
  node: GraphNode;
  chips: GraphNode[];
  y: number;
  height: number;
  cy: number;          // rail dot centre
  ts: number | null;
}

/** Rough advance width; SVG has no measurement pass and this only drives truncation. */
function chipWidth(label: string): number {
  return 14 + label.length * 5.4;
}

export function renderGraph(graph: Graph, opts: RenderOptions = {}): string {
  const M: Metrics = opts.fullscreen ? FULL : COMPACT;
  const CARD_W = M.cardW;
  if (graph.nodes.length === 0) {
    return '<p class="empty">No structural evidence resolved for this selection.</p>';
  }

  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  const incident = new Map<string, GraphEdge[]>();
  for (const edge of graph.edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) { continue; }
    for (const end of [edge.source, edge.target]) {
      if (!incident.has(end)) { incident.set(end, []); }
      incident.get(end)!.push(edge);
    }
  }

  const neighboursOf = (id: string): string[] =>
    (incident.get(id) ?? []).map((e) => (e.source === id ? e.target : e.source));

  // --- Fold satellites into their parent's card -----------------------------
  const chipsOf = new Map<string, GraphNode[]>();
  const foldedIn = new Set<string>();
  // child -> the row-bearing node that swallowed it. Arcs touching a folded node
  // are redirected here rather than dropped: a thread whose only structural link
  // was REFERENCES -> a folded ticket used to render as an orphan, visually
  // indistinguishable from a thread retrieved on similarity alone.
  const parentOf = new Map<string, string>();
  for (const node of graph.nodes) {
    if (!SATELLITE_TYPES.has(node.type)) { continue; }
    const parent = neighboursOf(node.id)
      .find((id) => { const o = byId.get(id); return !!o && !SATELLITE_TYPES.has(o.type); });
    // A satellite nothing claims stays on the rail; it is still evidence.
    if (!parent) { continue; }
    foldedIn.add(node.id);
    parentOf.set(node.id, parent);
    if (!chipsOf.has(parent)) { chipsOf.set(parent, []); }
    chipsOf.get(parent)!.push(node);
  }

  // --- Order the rail chronologically ---------------------------------------
  const ownTs = new Map<string, number | null>(graph.nodes.map((n) => [n.id, timestampOf(n)]));

  // An undated node (a bare PR, an unclaimed ticket) borrows the earliest date among
  // its neighbours rather than being exiled to the bottom, away from its own evidence.
  const effectiveTs = (node: GraphNode): number | null => {
    const own = ownTs.get(node.id) ?? null;
    if (own !== null) { return own; }
    let best: number | null = null;
    for (const other of neighboursOf(node.id)) {
      const t = ownTs.get(other) ?? null;
      if (t !== null && (best === null || t < best)) { best = t; }
    }
    return best;
  };

  const onRail = graph.nodes.filter((n) => !foldedIn.has(n.id));
  const anchor = onRail.find((n) => n.id === 'code') ?? null;
  const events = onRail.filter((n) => n !== anchor);
  // Array.prototype.sort is stable, so undated nodes keep their retrieval order.
  events.sort((a, b) => (effectiveTs(a) ?? Infinity) - (effectiveTs(b) ?? Infinity));
  const ordered = anchor ? [anchor, ...events] : events;

  // --- Place the rows -------------------------------------------------------
  const rows: Row[] = [];
  let cursor = MARGIN;
  ordered.forEach((node, index) => {
    const chips = chipsOf.get(node.id) ?? [];
    const subtitle = subtitleFor(node);
    const height = 42 + (subtitle ? 16 : 0) + (chips.length > 0 ? 22 : 0);
    if (index > 0) { cursor += index === 1 && anchor ? ANCHOR_GAP : ROW_GAP; }
    rows.push({ node, chips, y: cursor, height, cy: cursor + 21, ts: effectiveTs(node) });
    cursor += height;
  });

  const canvasHeight = cursor + MARGIN;
  const rowOf = new Map(rows.map((r, i) => [r.node.id, { row: r, index: i }]));
  /** A folded chip resolves to the card that carries it. */
  const rowFor = (id: string) => rowOf.get(id) ?? rowOf.get(parentOf.get(id) ?? '');

  // --- The rail: one continuous spine, so nothing reads as free-floating ----
  let railMarkup = '';
  if (rows.length > 1) {
    const last = rows[rows.length - 1];
    if (anchor) {
      // Dashed between the selection and the oldest event: that gap is "history
      // below", not an elapsed interval.
      railMarkup += `<path class="rail rail-head" d="M ${RAIL_X} ${rows[0].cy} L ${RAIL_X} ${rows[1].cy}" />`;
      if (rows.length > 2) {
        railMarkup += `<path class="rail" d="M ${RAIL_X} ${rows[1].cy} L ${RAIL_X} ${last.cy}" />`;
      }
    } else {
      railMarkup += `<path class="rail" d="M ${RAIL_X} ${rows[0].cy} L ${RAIL_X} ${last.cy}" />`;
    }
  }

  // --- Relationship arcs in the right gutter --------------------------------
  // Resolve both ends to rows first (folding a chip must not delete its edge),
  // drop self-loops, then dedupe: two REFERENCES from one thread to two tickets
  // folded into the same PR card are one relationship as drawn.
  interface Arc {
    edge: GraphEdge;
    a: number; b: number;       // row indices, a < b
    y0: number; y1: number;     // source/target y, in edge direction
    lane: number;
  }

  const arcs: Arc[] = [];
  const seenPair = new Set<string>();
  for (const edge of graph.edges) {
    const from = rowFor(edge.source);
    const to = rowFor(edge.target);
    if (!from || !to || from.index === to.index) { continue; }
    const key = `${Math.min(from.index, to.index)}:${Math.max(from.index, to.index)}:${edge.type}`;
    if (seenPair.has(key)) { continue; }
    seenPair.add(key);
    arcs.push({
      edge,
      a: Math.min(from.index, to.index),
      b: Math.max(from.index, to.index),
      y0: from.row.cy,
      y1: to.row.cy,
      lane: 0,
    });
  }

  // Lane assignment: greedy interval colouring over the rows an arc spans. Two
  // arcs share a lane only when their row ranges are disjoint, so nothing in a
  // lane can visually collide. Short arcs first keeps them nearest the cards.
  arcs.sort((p1, p2) => (p1.b - p1.a) - (p2.b - p2.a) || p1.a - p2.a);
  const laneRanges: Array<Array<[number, number]>> = [];
  for (const arc of arcs) {
    let lane = 0;
    for (; lane < M.maxLanes; lane++) {
      const taken = laneRanges[lane] ?? [];
      if (!taken.some(([lo, hi]) => arc.a < hi && lo < arc.b)) { break; }
    }
    if (lane === M.maxLanes) { lane = M.maxLanes - 1; }   // saturate gracefully
    arc.lane = lane;
    (laneRanges[lane] ??= []).push([arc.a, arc.b]);
  }

  const laneCount = Math.max(1, laneRanges.length);
  const bowOf = (lane: number) => LANE_BASE + lane * M.laneStep;
  const gutter = bowOf(laneCount - 1) + LANE_PAD;
  const canvasWidth = CARD_X + CARD_W + gutter + MARGIN;

  // Endpoint fan-out: several arcs landing on one row used to stack their
  // arrowheads on the identical pixel, on top of the card border. Spread them
  // across the card's right edge and start them just clear of it.
  const slots = new Map<number, number>();
  const slotOf = (index: number): number => {
    const n = slots.get(index) ?? 0;
    slots.set(index, n + 1);
    return n;
  };
  const FAN = 6;
  const fanned = (cy: number, row: number, height: number): number => {
    const k = slotOf(row);
    const reach = Math.min((height - 14) / 2, FAN * 2);
    const offset = ((k % 5) - 2) * (reach / 2.2);
    return cy + offset;
  };

  const EDGE_GAP = 3;   // keep the head off the card's stroke
  const arcMarkup = arcs.map((arc) => {
    const { edge } = arc;
    const fromIdx = rowFor(edge.source)!.index;
    const toIdx = rowFor(edge.target)!.index;
    const y0 = fanned(arc.y0, fromIdx, rows[fromIdx].height);
    const y1 = fanned(arc.y1, toIdx, rows[toIdx].height);
    const x0 = CARD_X + CARD_W + EDGE_GAP;
    const bow = bowOf(arc.lane);
    const inferred = edge.confidence === 'llm-flagged';
    const apexX = x0 + bow * 0.62;
    const apexY = (y0 + y1) / 2;
    const label = escapeHtml(edge.type.replace(/_/g, ' ').toLowerCase());
    return `
      <g class="edge${inferred ? ' inferred' : ''}" data-lane="${arc.lane}"
         data-source="${escapeAttr(edge.source)}" data-target="${escapeAttr(edge.target)}">
        <path class="hit" d="M ${x0} ${y0} C ${x0 + bow} ${y0}, ${x0 + bow} ${y1}, ${x0} ${y1}" />
        <path d="M ${x0} ${y0} C ${x0 + bow} ${y0}, ${x0 + bow} ${y1}, ${x0} ${y1}"
              marker-end="url(#arrow${inferred ? '-inferred' : ''})" />
        <g class="edge-label-g" transform="translate(${apexX} ${apexY})">
          <rect class="edge-label-bg" x="${-label.length * 2.5 - 5}" y="-7"
                width="${label.length * 5 + 10}" height="14" rx="7" />
          <text class="edge-label" x="0" y="4">${label}</text>
        </g>
      </g>`;
  }).join('');

  // --- Dots, stubs and cards ------------------------------------------------
  const rowMarkup = rows.map((row) => {
    const { node, chips, y, height, cy } = row;
    const style = TYPE_STYLE[node.type];
    const subtitle = subtitleFor(node);
    const citation = citationOf(node);
    const date = dateLabel(ownTs.get(node.id) ?? null);
    const payload = escapeAttr(JSON.stringify(node));

    let chipMarkup = '';
    if (chips.length > 0) {
      const chipY = y + (subtitle ? 56 : 40);
      let chipX = CARD_X + 12;
      const limit = CARD_X + CARD_W - 12;
      for (let i = 0; i < chips.length; i++) {
        const chip = chips[i];
        const label = `${TYPE_STYLE[chip.type].icon} ${truncate(chip.label, 16)}`;
        const width = chipWidth(label);
        if (chipX + width > limit) {
          // Out of room: stand the remainder up as a counter rather than clipping.
          chipMarkup += `<text class="chip-more" x="${chipX + 2}" y="${chipY + 12}">+${chips.length - i}</text>`;
          break;
        }
        chipMarkup += `
          <g class="chip-node ${TYPE_STYLE[chip.type].cssClass}"
             data-node-id="${escapeAttr(chip.id)}" data-node-json='${escapeAttr(JSON.stringify(chip))}'
             tabindex="0" role="button">
            <rect x="${chipX}" y="${chipY}" width="${width}" height="17" rx="8.5" />
            <text x="${chipX + 7}" y="${chipY + 12}">${escapeHtml(label)}</text>
          </g>`;
        chipX += width + 5;
      }
    }

    return `
      <g class="row">
        <line class="stub" x1="${RAIL_X + DOT_R}" y1="${cy}" x2="${CARD_X}" y2="${cy}" />
        <circle class="dot ${style.cssClass}" cx="${RAIL_X}" cy="${cy}" r="${DOT_R}" />
        <g class="node ${style.cssClass}${citation !== null ? ' linkable' : ''}"
           data-node-id="${escapeAttr(node.id)}" data-node-json='${payload}'
           ${citation !== null ? `data-citation="${citation}"` : ''} tabindex="0" role="button">
          <rect x="${CARD_X}" y="${y}" width="${CARD_W}" height="${height}" rx="8" />
          <rect class="accent" x="${CARD_X}" y="${y}" width="4" height="${height}" rx="2" />
          <text class="node-icon" x="${CARD_X + 14}" y="${y + 26}">${style.icon}</text>
          <text class="node-type" x="${CARD_X + 36}" y="${y + 18}">${escapeHtml(node.type)}</text>
          ${date ? `<text class="node-date" x="${CARD_X + CARD_W - 12}" y="${y + 18}">${escapeHtml(date)}</text>` : ''}
          <text class="node-label" x="${CARD_X + 36}" y="${y + 33}">${escapeHtml(truncate(node.label, citation !== null ? 20 : 24))}</text>
          ${citation !== null ? `<text class="node-cite" x="${CARD_X + CARD_W - 12}" y="${y + 33}">[${citation}] →</text>` : ''}
          ${subtitle ? `<text class="node-subtitle" x="${CARD_X + 14}" y="${y + 48}">${escapeHtml(truncate(subtitle, 40))}</text>` : ''}
        </g>
        ${chipMarkup}
      </g>`;
  }).join('');

  return `
    <div class="graph-toolbar">
      <button id="graph-zoom-in" class="graph-btn" title="Zoom in">+</button>
      <button id="graph-zoom-out" class="graph-btn" title="Zoom out">−</button>
      <button id="graph-zoom-reset" class="graph-btn" title="Fit to width">Fit</button>
      ${opts.fullscreen ? '' : '<button id="graph-expand" class="graph-btn" title="Open the timeline in a full editor tab" aria-label="Expand timeline">⤢</button>'}
      <span class="graph-hint">oldest first · click a thread to open its evidence · ⌘/ctrl+scroll to zoom</span>
    </div>
    <div class="graph-viewport-outer" id="graph-viewport-outer">
      <svg class="graph-svg${M.labelsAtRest ? ' labels-on' : ''}" id="graph-svg" data-canvas-width="${canvasWidth}" data-canvas-height="${canvasHeight}"
           viewBox="0 0 ${canvasWidth} ${canvasHeight}" width="${canvasWidth}" height="${canvasHeight}"
           role="img" aria-label="Provenance timeline">
        <defs>
          <!-- refX must equal the tip's x or the head floats past the endpoint.
               markerUnits=userSpaceOnUse keeps it one size: the default scales
               by stroke-width, so heads grew whenever .edge.active thickened
               the line. orient=auto (auto-start-reverse only affects
               marker-start) points it along the curve's incoming tangent. -->
          <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5"
                  markerWidth="9" markerHeight="9"
                  markerUnits="userSpaceOnUse" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          <marker id="arrow-inferred" viewBox="0 0 10 10" refX="10" refY="5"
                  markerWidth="9" markerHeight="9"
                  markerUnits="userSpaceOnUse" orient="auto">
            <path class="inferred-head" d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <g class="rails">${railMarkup}</g>
        <g class="edges">${arcMarkup}</g>
        <g class="nodes">${rowMarkup}</g>
      </svg>
    </div>
    <div class="graph-legend">
      ${LEGEND_TYPES.map(({ type, style }) => `
        <span class="legend-chip ${style.cssClass}">
          <span class="legend-icon">${style.icon}</span>${escapeHtml(type)}
        </span>`).join('')}
      <span class="legend-chip legend-edge">── proven by git / identity</span>
      <span class="legend-chip legend-edge inferred">- - flagged by the model</span>
    </div>
    <div id="node-details" class="node-details empty">Click a node above for its full detail.</div>`;
}
