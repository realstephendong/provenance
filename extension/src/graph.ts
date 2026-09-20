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
//   * The rail carries chronology. The real graph edges are routed as orthogonal
//     connectors in the right gutter, unlabelled until you hover -- the structure
//     is still there, it just is not competing with the timeline for attention.
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

// Edge routing. Edges run as orthogonal connectors in the right gutter: out of
// the source card, along a vertical track, back into the target card.
//
// They used to be symmetric cubic Béziers with horizontal control arms of length
// `bow`. That geometry cannot work here, because bow (18-98px) is set by the lane
// while the vertical span is set by the timeline -- routinely 300px, up to 1300px
// on a full selection. A Bézier whose control arms are a twentieth of its span is
// a vertical sliver that turns through 90 degrees within a few pixels of each
// endpoint, which broke two things at once:
//   * The arrowhead is a straight triangle laid on the tangent *at* the
//     endpoint. Where the curve turns that hard, the tangent stops describing the
//     curve a pixel later, so the head visibly detached and pointed across the
//     line it was supposed to terminate.
//   * Lanes stopped separating anything. Ten near-vertical slivers 16px apart read
//     as one band, and dashed "inferred" edges read as a ladder.
// An orthogonal route has no such failure mode: the last segment into the card is
// straight and horizontal by construction, so the head is always flush, and a
// straight vertical track stays visually distinct from the track beside it however
// long the span.
const LANE_BASE = 20;       // x offset of the innermost track, past the card edge
const LANE_STEP = 14;       // spacing between tracks
const LANE_PAD = 18;        // breathing room past the outermost track
const CORNER = 7;           // max corner radius where a track meets a horizontal run

interface Metrics {
  cardW: number;
  laneStep: number;
  labelsAtRest: boolean;
  maxLanes: number;
  /** Labels sit centred on their track, so the outermost one needs room to its right. */
  lanePad: number;
}

const COMPACT: Metrics =
  { cardW: 250, laneStep: LANE_STEP, labelsAtRest: false, maxLanes: 8, lanePad: LANE_PAD };
const FULL: Metrics =
  { cardW: 420, laneStep: 30, labelsAtRest: true, maxLanes: 16, lanePad: 52 };

export interface RenderOptions {
  /** Editor-tab timeline: wider cards, more lanes, labels on without hovering. */
  fullscreen?: boolean;
}

// Vendor marks for the editor and for the systems the evidence comes from, inlined as path
// data. They cannot be loaded: the webview's CSP is `default-src 'none'` with no
// `img-src`, so a remote URL and a packaged file would both draw nothing. Each
// mark is the brand's official one from Simple Icons (CC0 1.0), on a 24x24 box.
//
// A mark is a list of subpaths. A subpath with no `fill` inherits the node's accent
// colour from CSS, which is what GitHub and Sentry want -- they are single-colour
// marks and reading as the node's colour is the point. Slack is not: it is four
// coloured arms, and drawing it in one flat purple loses the mark people recognise.
// So its eight subpaths carry Slack's own palette, which beats the inherited fill.
interface BrandSubpath {
  d: string;
  /** Absent means "take the node's accent colour", which CSS sets on the group. */
  fill?: string;
}

const BRAND_MARKS = {
  // The anchor node is the editor's own selection, so it takes the editor's mark,
  // in VS Code's brand blue rather than the theme's accent blue.
  vscode: [{ d: 'M23.15 2.587L18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128a.999.999 0 0 0-1.276.057L.327 7.261A1 1 0 0 0 .326 8.74L3.899 12 .326 15.26a1 1 0 0 0 .001 1.479L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352zm-5.146 14.861L10.826 12l7.178-5.448v10.896z', fill: '#007ACC' }],
  github: [{ d: 'M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12' }],
  slack: [
    { d: 'M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52z', fill: '#E01E5A' },
    { d: 'M6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z', fill: '#E01E5A' },
    { d: 'M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834z', fill: '#36C5F0' },
    { d: 'M8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z', fill: '#36C5F0' },
    { d: 'M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834z', fill: '#2EB67D' },
    { d: 'M17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z', fill: '#2EB67D' },
    { d: 'M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52z', fill: '#ECB22E' },
    { d: 'M15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z', fill: '#ECB22E' },
  ],
  sentry: [{ d: 'M13.91 2.505c-.873-1.448-2.972-1.448-3.844 0L6.904 7.92a15.478 15.478 0 0 1 8.53 12.811h-2.221A13.301 13.301 0 0 0 5.784 9.814l-2.926 5.06a7.65 7.65 0 0 1 4.435 5.848H2.194a.365.365 0 0 1-.298-.534l1.413-2.402a5.16 5.16 0 0 0-1.614-.913L.296 19.275a2.182 2.182 0 0 0 .812 2.999 2.24 2.24 0 0 0 1.086.288h6.983a9.322 9.322 0 0 0-3.845-8.318l1.11-1.922a11.47 11.47 0 0 1 4.95 10.24h5.915a17.242 17.242 0 0 0-7.885-15.28l2.244-3.845a.37.37 0 0 1 .504-.13c.255.14 9.75 16.708 9.928 16.9a.365.365 0 0 1-.327.543h-2.287c.029.612.029 1.223 0 1.831h2.297a2.206 2.206 0 0 0 1.922-3.31z' }],
} satisfies Record<string, BrandSubpath[]>;

type BrandKey = keyof typeof BRAND_MARKS;

interface TypeStyle {
  cssClass: string;
  /** Drawn for the types with no vendor behind them, and for chips at any type. */
  icon: string;
  /** Drawn instead of `icon` on the card and in the legend, where there is one. */
  brand?: BrandKey;
  description: string;
}

/** Matches `.node-icon`'s font-size: the box an emoji occupies on a card. */
const GLYPH = 13;

/** Vendor marks are drawn larger than that box -- at emoji size the GitHub cat and
 *  the Sentry wave are unreadable smudges. The extra width is centred on the emoji
 *  box, so the type and label columns beside it do not move. */
const MARK = 18;

const TYPE_STYLE: Record<NodeType, TypeStyle> = {
  Code: { cssClass: 'n-code', icon: '\u{1F9E9}', brand: 'vscode', description: 'Your selection' },
  Commit: { cssClass: 'n-commit', icon: '\u{1F517}', brand: 'github', description: 'git blame result' },
  PullRequest: { cssClass: 'n-pr', icon: '\u{1F500}', brand: 'github', description: 'Resolved from the commit' },
  SlackThread: { cssClass: 'n-slack', icon: '\u{1F4AC}', brand: 'slack', description: 'Retrieved evidence' },
  Ticket: { cssClass: 'n-ticket', icon: '\u{1F3AB}', description: 'Mock ticket tracker' },
  SentryIssue: { cssClass: 'n-sentry', icon: '\u{1F6A8}', brand: 'sentry', description: 'Mock incident tracker' },
  Person: { cssClass: 'n-person', icon: '\u{1F464}', description: 'git blame author' },
};

/** Baselines of the card's two header rows, measured from the card's top edge. The
 *  icon is positioned from these rather than from a constant of its own, so it stays
 *  aligned with the text if either row moves. */
const TYPE_BASELINE = 18;
const LABEL_BASELINE = 33;

/** The icon's optical centre: halfway down the block the header rows occupy, from
 *  the top of the type row's capitals to the label's baseline. Centring on the box
 *  bounds instead would hang the icon high, because both rows sit near the bottom
 *  of their line boxes. `.node-type`'s font-size is 8.5px; 0.72em is its cap height. */
const ICON_CY = (TYPE_BASELINE - 8.5 * 0.72 + LABEL_BASELINE) / 2;

/** The card's icon, centred on (`x` + half a GLYPH box, `cy`). An emoji hangs from
 *  its baseline and occupies roughly the em box above it, so it is dropped to sit on
 *  `cy`; a mark is scaled to MARK and centred on the same point. */
function cardIcon(style: TypeStyle, x: number, cy: number): string {
  if (style.brand === undefined) {
    return `<text class="node-icon" x="${x}" y="${(cy + GLYPH * 0.36).toFixed(2)}">${style.icon}</text>`;
  }
  return `<g class="node-glyph" transform="translate(${(x - (MARK - GLYPH) / 2).toFixed(2)} ${(cy - MARK / 2).toFixed(2)}) scale(${(MARK / 24).toFixed(4)})">
            ${markPaths(style.brand)}</g>`;
}

/** The subpaths of a mark. A subpath with its own colour states it inline: a fill
 *  attribute on the path outranks the fill the group inherits from CSS. */
function markPaths(brand: BrandKey): string {
  return BRAND_MARKS[brand]
    .map(p => `<path d="${p.d}"${'fill' in p ? ` fill="${p.fill}"` : ''} />`)
    .join('');
}

/** The legend's icon. HTML, not SVG markup -- the legend lives outside the canvas. */
function legendIcon(style: TypeStyle): string {
  if (style.brand === undefined) {
    return `<span class="legend-icon">${style.icon}</span>`;
  }
  return `<svg class="legend-icon legend-glyph" viewBox="0 0 24 24" aria-hidden="true"
               >${markPaths(style.brand)}</svg>`;
}

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

/** Git and provider identities can be handles (for example, `Awais-H`) rather
 * than presentation names. The graph needs only the familiar first-name part. */
function firstName(value: string): string {
  return value.trim().split(/[\s_-]+/, 1)[0] || value;
}

function subtitleFor(node: GraphNode): string {
  const data = node.data ?? {};
  if (typeof data.title === 'string' && data.title) { return data.title; }
  if (typeof data.summary === 'string' && data.summary) { return data.summary; }
  if (Array.isArray(data.authors) && data.authors.length > 0) {
    return data.authors.filter((author): author is string => typeof author === 'string')
      .map(firstName).join(', ');
  }
  if (typeof data.author === 'string' && data.author) { return firstName(data.author); }
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
    const height = 42 + (subtitle ? 16 : 0);
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

  // --- Relationship connectors in the right gutter ---------------------------
  // Resolve both ends to rows first (folding a chip must not delete its edge),
  // drop self-loops, then dedupe: two REFERENCES from one thread to two tickets
  // folded into the same PR card are one relationship as drawn.
  interface Conn {
    edge: GraphEdge;
    a: number; b: number;             // row indices, a < b
    fromIdx: number; toIdx: number;   // row indices in edge direction
    y0: number; y1: number;           // source/target y, in edge direction
    lane: number;
  }

  const conns: Conn[] = [];
  const seenPair = new Set<string>();
  for (const edge of graph.edges) {
    const from = rowFor(edge.source);
    const to = rowFor(edge.target);
    if (!from || !to || from.index === to.index) { continue; }
    const key = `${Math.min(from.index, to.index)}:${Math.max(from.index, to.index)}:${edge.type}`;
    if (seenPair.has(key)) { continue; }
    seenPair.add(key);
    conns.push({
      edge,
      a: Math.min(from.index, to.index),
      b: Math.max(from.index, to.index),
      fromIdx: from.index,
      toIdx: to.index,
      y0: from.row.cy,
      y1: to.row.cy,
      lane: 0,
    });
  }

  // Lane assignment: greedy interval colouring over the rows a connector spans.
  // Short connectors first keeps them nearest the cards.
  //
  // Ranges that merely *touch* count as conflicting, which they did not have to
  // under the old Bézier routing. Two connectors ending and starting on one row --
  // a PART_OF into #318 and a SUPERSEDES out of it -- bowed away from each other
  // as curves. As orthogonal tracks they would abut end-to-end at the same x and
  // read as one continuous edge running straight past the row they actually meet
  // at, which is precisely the wrong thing to say about a supersede chain.
  conns.sort((p1, p2) => (p1.b - p1.a) - (p2.b - p2.a) || p1.a - p2.a);
  const laneRanges: Array<Array<[number, number]>> = [];
  const conflicts = (lane: number, conn: Conn): number =>
    (laneRanges[lane] ?? []).filter(([lo, hi]) => conn.a <= hi && lo <= conn.b).length;

  for (const conn of conns) {
    let lane = 0;
    for (; lane < M.maxLanes; lane++) {
      if (conflicts(lane, conn) === 0) { break; }
    }
    if (lane === M.maxLanes) {
      // Out of tracks. Dumping every overflow connector in the last lane rebuilt the
      // fused band this routing exists to avoid; spread them over whichever
      // track they collide with least instead.
      lane = 0;
      for (let k = 1; k < M.maxLanes; k++) {
        if (conflicts(k, conn) < conflicts(lane, conn)) { lane = k; }
      }
    }
    conn.lane = lane;
    (laneRanges[lane] ??= []).push([conn.a, conn.b]);
  }

  const laneCount = Math.max(1, laneRanges.length);
  const trackX = (lane: number) => CARD_X + CARD_W + LANE_BASE + lane * M.laneStep;
  // Labels that are always on hang off the right of their track, so the outermost
  // one has to fit inside the canvas or the SVG viewport clips it. Measured from
  // this graph's longest edge type rather than assumed: a selection whose edges
  // are all `part of` should not pay for the width of `conflicts with`.
  const longestType = conns.reduce((n, a) => Math.max(n, a.edge.type.length), 0);
  const labelRoom = M.labelsAtRest ? longestType * 5 + 16 : 0;
  const gutter = LANE_BASE + (laneCount - 1) * M.laneStep + Math.max(M.lanePad, labelRoom);
  const canvasWidth = CARD_X + CARD_W + gutter + MARGIN;

  // Endpoint fan-out: several connectors landing on one row used to stack their
  // arrowheads on the identical pixel, on top of the card border. Spread them
  // across the card's right edge and start them just clear of it.
  //
  // Counted up front rather than cycled through a fixed set of offsets: the hub
  // of a busy selection takes eight edges, and five offsets meant the sixth,
  // seventh and eighth heads landed exactly on the first three. Knowing the
  // total lets the row space its endpoints to fit the card it has.
  const endpoints = new Map<number, number>();
  for (const conn of conns) {
    for (const idx of [conn.fromIdx, conn.toIdx]) {
      endpoints.set(idx, (endpoints.get(idx) ?? 0) + 1);
    }
  }

  const slots = new Map<number, number>();
  const slotOf = (index: number): number => {
    const n = slots.get(index) ?? 0;
    slots.set(index, n + 1);
    return n;
  };
  const FAN_MAX = 7;          // widest gap between two endpoints on one card edge
  const fanned = (cy: number, row: number, height: number): number => {
    const total = endpoints.get(row) ?? 1;
    if (total < 2) { return cy; }
    const k = slotOf(row);
    const spacing = Math.min(FAN_MAX, Math.max(0, height - 16) / total);
    return cy + (k - (total - 1) / 2) * spacing;
  };

  const EDGE_GAP = 3;   // keep the head off the card's stroke

  /**
   * One edge as an orthogonal connector: out of the source card at y0, a rounded
   * turn onto the lane's vertical track, down (or up) to y1, a rounded turn back,
   * and a straight horizontal run into the target card.
   *
   * The final run is what the arrowhead sits on. It is horizontal by construction
   * and at least `CORNER` long, so the head lies flush along the line rather than
   * on a tangent the curve has already left -- which is the whole point of routing
   * this way instead of bowing.
   */
  const route = (xCard: number, y0: number, xTrack: number, y1: number): string => {
    const dy = y1 - y0;
    const dir = dy >= 0 ? 1 : -1;
    // Never let a corner eat more than half the run it turns out of, or adjacent
    // rows (a short vertical hop) would round into a lens rather than an elbow.
    const r = Math.max(0, Math.min(CORNER, Math.abs(dy) / 2, (xTrack - xCard) / 2));
    return [
      `M ${xCard} ${y0}`,
      `L ${xTrack - r} ${y0}`,
      `Q ${xTrack} ${y0} ${xTrack} ${y0 + dir * r}`,
      `L ${xTrack} ${y1 - dir * r}`,
      `Q ${xTrack} ${y1} ${xTrack - r} ${y1}`,
      `L ${xCard} ${y1}`,
    ].join(' ');
  };

  // Where labels are always on they must clear the cards, so they hang off the
  // right of their track and the gutter above was widened to hold them. Where
  // they only appear on hover they behave like a tooltip, and centring them on
  // the track costs no permanent gutter -- which in a 340px sidebar is the whole
  // budget.
  const LABEL_H = 16;
  interface Drawn {
    conn: Conn; d: string; label: string; pillW: number;
    xTrack: number; y: number; top: number; bottom: number; inferred: boolean;
  }

  const drawn: Drawn[] = conns.map((conn) => {
    const { edge, fromIdx, toIdx } = conn;
    const y0 = fanned(conn.y0, fromIdx, rows[fromIdx].height);
    const y1 = fanned(conn.y1, toIdx, rows[toIdx].height);
    const x0 = CARD_X + CARD_W + EDGE_GAP;
    const xTrack = trackX(conn.lane);
    const label = escapeHtml(edge.type.replace(/_/g, ' ').toLowerCase());
    // Labels ride their track's vertical run, so they may sit anywhere between
    // its two corners. Start them staggered by lane parity: neighbouring lanes
    // then rarely want the same height in the first place.
    const top = Math.min(y0, y1) + CORNER;
    const bottom = Math.max(y0, y1) - CORNER;
    const along = y0 + (y1 - y0) * (conn.lane % 2 === 0 ? 0.38 : 0.62);
    return {
      conn,
      d: route(x0, y0, xTrack, y1),
      label,
      pillW: label.length * 5 + 10,
      xTrack,
      y: bottom > top ? Math.min(bottom, Math.max(top, along)) : (y0 + y1) / 2,
      top: Math.min(top, bottom),
      bottom: Math.max(top, bottom),
      inferred: edge.confidence === 'llm-flagged',
    };
  });

  // Labels shown all at once have to be de-conflicted for real: two lanes whose
  // connectors happen to centre at the same height will collide however they are
  // staggered, and a half-covered "part of" is worse than no label. Each label
  // may slide anywhere along its own track, so walk them in order and push each
  // to the nearest free height its run allows. Hover-only labels skip this --
  // at most a couple are visible at a time, and they are free to overlap.
  if (M.labelsAtRest) {
    const placed: Drawn[] = [];
    for (const item of drawn) {
      const near = (other: Drawn) =>
        item.xTrack < other.xTrack + other.pillW + 6 &&
        other.xTrack < item.xTrack + item.pillW + 6;
      const free = (y: number) =>
        !placed.some((o) => near(o) && Math.abs(o.y - y) < LABEL_H);
      if (!free(item.y)) {
        const start = item.y;
        for (let step = 4; step <= 160; step += 4) {
          if (start + step <= item.bottom && free(start + step)) { item.y = start + step; break; }
          if (start - step >= item.top && free(start - step)) { item.y = start - step; break; }
        }
      }
      placed.push(item);
    }
  }

  const connMarkup = drawn.map((item) => {
    const { conn, d, label, pillW, xTrack, y, inferred } = item;
    const { edge } = conn;
    // Hover labels are centred on their track, and the gutter is not widened for
    // them -- so on the outermost lane a wide one would run past the canvas and
    // be clipped by the SVG viewport. Slide it back inside instead.
    const centred = Math.min(-pillW / 2, canvasWidth - MARGIN - xTrack - pillW);
    const pillX = M.labelsAtRest ? 2 : centred;
    const textX = M.labelsAtRest ? pillX + 5 : pillX + pillW / 2;
    const anchor = M.labelsAtRest ? 'start' : 'middle';
    return `
      <g class="edge${inferred ? ' inferred' : ''}" data-lane="${conn.lane}"
         data-source="${escapeAttr(edge.source)}" data-target="${escapeAttr(edge.target)}">
        <path class="hit" d="${d}" />
        <path class="line" d="${d}" marker-end="url(#arrow${inferred ? '-inferred' : ''})" />
        <g class="edge-label-g" transform="translate(${xTrack} ${y})">
          <rect class="edge-label-bg" x="${pillX}" y="-7" width="${pillW}" height="14" rx="7" />
          <text class="edge-label" x="${textX}" y="4" text-anchor="${anchor}">${label}</text>
        </g>
      </g>`;
  }).join('');

  // --- Dots, stubs and cards ------------------------------------------------
  const rowMarkup = rows.map((row) => {
    const { node, y, height, cy } = row;
    const style = TYPE_STYLE[node.type];
    const subtitle = subtitleFor(node);
    const citation = citationOf(node);
    const date = dateLabel(ownTs.get(node.id) ?? null);
    const payload = escapeAttr(JSON.stringify(node));

    return `
      <g class="row">
        <line class="stub" x1="${RAIL_X + DOT_R}" y1="${cy}" x2="${CARD_X}" y2="${cy}" />
        <circle class="dot ${style.cssClass}" cx="${RAIL_X}" cy="${cy}" r="${DOT_R}" />
        <g class="node ${style.cssClass}${citation !== null ? ' linkable' : ''}"
           data-node-id="${escapeAttr(node.id)}" data-node-json='${payload}'
           ${citation !== null ? `data-citation="${citation}"` : ''} tabindex="0" role="button">
          <rect x="${CARD_X}" y="${y}" width="${CARD_W}" height="${height}" rx="8" />
          <rect class="accent" x="${CARD_X}" y="${y}" width="4" height="${height}" rx="2" />
          ${cardIcon(style, CARD_X + 14, y + ICON_CY)}
          <text class="node-type" x="${CARD_X + 36}" y="${y + TYPE_BASELINE}">${escapeHtml(node.type)}</text>
          ${date ? `<text class="node-date" x="${CARD_X + CARD_W - 12}" y="${y + TYPE_BASELINE}">${escapeHtml(date)}</text>` : ''}
          <text class="node-label" x="${CARD_X + 36}" y="${y + LABEL_BASELINE}">${escapeHtml(truncate(node.label, citation !== null ? 20 : 24))}</text>
          ${citation !== null ? `<text class="node-cite" x="${CARD_X + CARD_W - 12}" y="${y + LABEL_BASELINE}">[${citation}] →</text>` : ''}
          ${subtitle ? `<text class="node-subtitle" x="${CARD_X + 14}" y="${y + 48}">${escapeHtml(truncate(subtitle, 28))}</text>` : ''}
        </g>
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
               the line. orient=auto points it along the incoming tangent, which
               the routing above guarantees is horizontal for at least CORNER
               units -- so markerWidth must stay under that run or the head's
               tail rides up into the corner. -->
          <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5"
                  markerWidth="8" markerHeight="8"
                  markerUnits="userSpaceOnUse" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          <marker id="arrow-inferred" viewBox="0 0 10 10" refX="10" refY="5"
                  markerWidth="8" markerHeight="8"
                  markerUnits="userSpaceOnUse" orient="auto">
            <path class="inferred-head" d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <g class="rails">${railMarkup}</g>
        <g class="edges">${connMarkup}</g>
        <g class="nodes">${rowMarkup}</g>
      </svg>
    </div>
    <div class="graph-legend">
      ${LEGEND_TYPES.map(({ type, style }) => `
        <span class="legend-chip ${style.cssClass}">
          ${legendIcon(style)}${escapeHtml(type)}
        </span>`).join('')}
      <span class="legend-chip legend-edge">── proven by git / identity</span>
      <span class="legend-chip legend-edge inferred">- - flagged by the model</span>
    </div>
    <div id="node-details" class="node-details empty">Click a node above for its full detail.</div>`;
}
