import { Graph, GraphNode, NodeType } from './types';

// A hand-rolled, but genuinely interactive, SVG renderer: pan, zoom, hover-to-highlight
// the connected subgraph, and click-through to a details drawer. Pulling in a graph
// library for ~15 nodes would be more code, not less -- the interactivity below is
// wired up from panel.ts's single webview <script> block, since a webview may only
// call `acquireVsCodeApi()` once and this markup has to share that script's scope.

const NODE_WIDTH = 210;
const NODE_HEIGHT = 66;
const H_GAP = 56;
const V_GAP = 110;
const MARGIN = 48;

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

export const LEGEND_TYPES: { type: NodeType; style: TypeStyle }[] =
  (Object.keys(TYPE_STYLE) as NodeType[]).map((type) => ({ type, style: TYPE_STYLE[type] }));

interface Placed {
  node: GraphNode;
  x: number;
  y: number;
}

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
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`;
}

/** BFS depth from the code node, so layout follows the evidence chain outwards. */
function assignLayers(graph: Graph): Map<string, number> {
  const adjacency = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!adjacency.has(edge.source)) { adjacency.set(edge.source, []); }
    adjacency.get(edge.source)!.push(edge.target);
    // Undirected for layering purposes: a Slack thread that only has an incoming
    // DISCUSSED_IN edge should still land one layer out from its source, not be
    // treated as unreachable.
    if (!adjacency.has(edge.target)) { adjacency.set(edge.target, []); }
    adjacency.get(edge.target)!.push(edge.source);
  }

  const layers = new Map<string, number>();
  const root = graph.nodes.find((n) => n.id === 'code')?.id ?? graph.nodes[0]?.id;
  if (!root) { return layers; }

  const queue: string[] = [root];
  layers.set(root, 0);
  while (queue.length > 0) {
    const current = queue.shift()!;
    const depth = layers.get(current)!;
    for (const next of adjacency.get(current) ?? []) {
      if (!layers.has(next)) {
        layers.set(next, depth + 1);
        queue.push(next);
      }
    }
  }

  const maxDepth = Math.max(0, ...layers.values());
  for (const node of graph.nodes) {
    if (!layers.has(node.id)) { layers.set(node.id, maxDepth + 1); }
  }
  return layers;
}

function subtitleFor(node: GraphNode): string {
  const data = node.data ?? {};
  if (typeof data.title === 'string' && data.title) { return data.title; }
  if (Array.isArray(data.authors) && data.authors.length > 0) { return data.authors.join(', '); }
  if (typeof data.date === 'string' && data.date) { return data.date; }
  if (typeof data.author === 'string' && data.author) { return data.author; }
  return '';
}

export function renderGraph(graph: Graph): string {
  if (graph.nodes.length === 0) {
    return '<p class="empty">No structural evidence resolved for this selection.</p>';
  }

  const layers = assignLayers(graph);
  const byLayer = new Map<number, GraphNode[]>();
  for (const node of graph.nodes) {
    const depth = layers.get(node.id) ?? 0;
    if (!byLayer.has(depth)) { byLayer.set(depth, []); }
    byLayer.get(depth)!.push(node);
  }

  const depths = [...byLayer.keys()].sort((a, b) => a - b);
  const widest = Math.max(...depths.map((d) => byLayer.get(d)!.length));
  const canvasWidth = MARGIN * 2 + widest * NODE_WIDTH + Math.max(0, widest - 1) * H_GAP;

  const placed = new Map<string, Placed>();
  for (const depth of depths) {
    const row = byLayer.get(depth)!;
    const rowWidth = row.length * NODE_WIDTH + (row.length - 1) * H_GAP;
    const startX = (canvasWidth - rowWidth) / 2;
    row.forEach((node, index) => {
      placed.set(node.id, {
        node,
        x: startX + index * (NODE_WIDTH + H_GAP),
        y: MARGIN + depth * (NODE_HEIGHT + V_GAP),
      });
    });
  }

  const canvasHeight = MARGIN * 2 + depths.length * NODE_HEIGHT + Math.max(0, depths.length - 1) * V_GAP;

  const edgeMarkup = graph.edges.map((edge) => {
    const from = placed.get(edge.source);
    const to = placed.get(edge.target);
    if (!from || !to) { return ''; }
    const x1 = from.x + NODE_WIDTH / 2;
    const y1 = from.y + NODE_HEIGHT;
    const x2 = to.x + NODE_WIDTH / 2;
    const y2 = to.y;
    const midY = (y1 + y2) / 2;
    const inferred = edge.confidence === 'llm-flagged';
    return `
      <g class="edge${inferred ? ' inferred' : ''}"
         data-source="${escapeAttr(edge.source)}" data-target="${escapeAttr(edge.target)}">
        <path d="M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}"
              marker-end="url(#arrow${inferred ? '-inferred' : ''})" />
        <text class="edge-label" x="${(x1 + x2) / 2}" y="${midY - 4}">${escapeHtml(edge.type)}</text>
      </g>`;
  }).join('');

  const nodeMarkup = [...placed.values()].map(({ node, x, y }) => {
    const style = TYPE_STYLE[node.type];
    const subtitle = subtitleFor(node);
    const payload = escapeAttr(JSON.stringify(node));
    return `
      <g class="node ${style.cssClass}" data-node-id="${escapeAttr(node.id)}"
         data-node-json='${payload}' tabindex="0" role="button">
        <rect x="${x}" y="${y}" width="${NODE_WIDTH}" height="${NODE_HEIGHT}" rx="9" />
        <rect class="accent" x="${x}" y="${y}" width="5" height="${NODE_HEIGHT}" rx="2" />
        <text class="node-icon" x="${x + 16}" y="${y + 26}">${style.icon}</text>
        <text class="node-type" x="${x + 40}" y="${y + 20}">${escapeHtml(node.type)}</text>
        <text class="node-label" x="${x + 40}" y="${y + 38}">${escapeHtml(truncate(node.label, 20))}</text>
        ${subtitle ? `<text class="node-subtitle" x="${x + 16}" y="${y + 55}">${escapeHtml(truncate(subtitle, 28))}</text>` : ''}
      </g>`;
  }).join('');

  return `
    <div class="graph-toolbar">
      <button id="graph-zoom-in" class="graph-btn" title="Zoom in">+</button>
      <button id="graph-zoom-out" class="graph-btn" title="Zoom out">−</button>
      <button id="graph-zoom-reset" class="graph-btn" title="Fit to view">Fit</button>
      <span class="graph-hint">scroll to zoom · drag to pan · hover a node to trace its edges · click for details</span>
    </div>
    <div class="graph-viewport-outer" id="graph-viewport-outer">
      <svg class="graph-svg" id="graph-svg" data-canvas-width="${canvasWidth}" data-canvas-height="${canvasHeight}"
           viewBox="0 0 ${canvasWidth} ${canvasHeight}" width="${canvasWidth}" height="${canvasHeight}"
           role="img" aria-label="Provenance graph">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          <marker id="arrow-inferred" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path class="inferred-head" d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <g class="edges">${edgeMarkup}</g>
        <g class="nodes">${nodeMarkup}</g>
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
