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

/** The sources disagree on date format: `YYYY-MM-DD` from git blame and from the
 *  Slack export, full ISO-8601 from the GitHub and Sentry adapters. */
function timestampOf(node: GraphNode): number | null {
  const data = node.data ?? {};
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
          <span class="legend-icon">${style.icon}</span>${escapeHtml(type)}
        </span>`).join('')}
      <span class="legend-chip legend-edge">── proven by git / identity</span>
      <span class="legend-chip legend-edge inferred">- - flagged by the model</span>
    </div>
    <div id="node-details" class="node-details empty">Click a node above for its full detail.</div>`;
}
