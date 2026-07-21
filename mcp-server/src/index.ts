#!/usr/bin/env node
/**
 * Moment MCP Server
 *
 * An MCP server that allows Claude Code to interact with Moment projects.
 * Provides tools for reading project context, managing tasks, and participating in discussions.
 *
 * Configuration:
 *   --api-key / MOMENT_MCP_API_KEY / MOMENT_API_KEY - Your Moment agent API key
 *   --api-url / MOMENT_MCP_API_URL / MOMENT_API_URL - Base URL for Moment API (default: http://localhost:3000)
 *   --config / MOMENT_MCP_CONFIG - Optional path to JSON file with { "apiKey", "apiUrl" }
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { loadConfig, MomentServerConfig } from './config.js';

// Configuration
const config = loadConfig();

class MomentApiClient {
  constructor(private readonly serverConfig: MomentServerConfig) {}

  async request(
    method: string,
    path: string,
    body?: unknown
  ): Promise<unknown> {
    const url = `${this.serverConfig.apiUrl}${path}`;
    const response = await fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${this.serverConfig.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    const raw = await response.text();
    const parsed = raw ? safeJsonParse(raw) : undefined;

    if (!response.ok) {
      const summary =
        parsed === undefined
          ? 'No response body'
          : typeof parsed === 'string'
            ? parsed
            : JSON.stringify(parsed);
      throw new Error(`API error (${response.status}): ${summary}`);
    }

    if (this.serverConfig.debug) {
      console.error(
        `[moment-mcp] ${method} ${path} -> ${response.status}`
      );
    }

    return parsed;
  }

  get(path: string): Promise<unknown> {
    return this.request('GET', path);
  }

  post(path: string, body?: unknown): Promise<unknown> {
    return this.request('POST', path, body);
  }
}

function safeJsonParse(payload: string): unknown {
  try {
    return JSON.parse(payload);
  } catch {
    return payload;
  }
}

// Helper to truncate long strings for discovery/browsing responses
function truncate(str: string | null | undefined, max = 100): string {
  if (!str) return '';
  if (str.length <= max) return str;
  return str.slice(0, max - 3) + '...';
}

const apiClient = new MomentApiClient(config);

// Create server
const server = new Server(
  {
    name: 'moment-server',
    version: '0.1.0',
  },
  {
    capabilities: {
      tools: {},
      resources: {},
    },
  }
);

// --------------------------------------------------------------------------
// Prose-first responses (A1)
// Context-reading tools compose a well-edited brief server-side; the agent
// should read THAT prose first and only parse JSON when it needs a field. So
// for those tools we emit the composed string as the primary text content and
// append the structured object below a clear separator.
// --------------------------------------------------------------------------

/**
 * Build a prose-first text payload: the composed brief on top, the full
 * structured response below a labeled JSON fence (parse only if you need a field).
 */
function proseFirst(brief: string, structured: unknown): string {
  return `${brief.trim()}\n\n--- structured (JSON) ---\n${JSON.stringify(structured, null, 2)}`;
}

const DEPRECATED_TOOL_ALIASES: Record<string, string> = {
  find_context: 'search_context',
  build_context: 'import_context',
  gather_context: 'compile_context',
};

const HIDDEN_DEPRECATED_TOOL_NAMES = new Set(Object.keys(DEPRECATED_TOOL_ALIASES));

function withDeprecationNote(toolName: string, text: string): string {
  const replacement = DEPRECATED_TOOL_ALIASES[toolName];
  return replacement
    ? `Deprecated alias: ${toolName} is hidden from ListTools; use ${replacement} for new calls.\n\n${text}`
    : text;
}

/**
 * Compact the get_project_context structured echo (P3). The prose brief above
 * the fence ALREADY carries the full content (handoff text, page bodies,
 * resource summaries, narrative), so re-dumping the whole API object below it
 * doubled the payload. Here we keep only NAVIGATIONAL HANDLES and small
 * metadata — enough to deep-link or read-on-demand — and explicitly DROP:
 *   • promptContext (it IS the prose above the fence)
 *   • page bodies     (slug + title remain → read a page when you need it)
 *   • the verbose narrative/coreProfile/protocol prose (it's in the brief)
 * Unknown/extra fields are ignored, so this can't throw on shape drift.
 */
function compactProjectContext(ctx: Record<string, unknown>): Record<string, unknown> {
  const asArray = (v: unknown): Record<string, unknown>[] =>
    Array.isArray(v) ? (v as Record<string, unknown>[]) : [];
  const pages = asArray(ctx.pages).map((p) => ({
    slug: p.slug, title: p.title, agentRead: p.agentRead, pinned: p.pinned,
  }));
  const resources = asArray(ctx.resources).map((r) => ({
    id: r.id, kind: r.kind, title: r.title, uri: r.uri,
  }));
  const handoff = ctx.handoff as Record<string, unknown> | null | undefined;
  return {
    // The brief above is the read-first content; below = pointers only.
    note: 'Compact pointer subset — the full brief is the prose above this fence. Read a page by its slug, or pull a resource with read_resource(id).',
    project: ctx.project,
    pages,
    resources,
    activeTasks: ctx.activeTasks,
    handoff: handoff
      ? { version: handoff.version, updatedAt: handoff.updatedAt, updatedBy: handoff.updatedBy }
      : null,
    contextFreshness: ctx.contextFreshness,
    stats: ctx.stats,
    sinceYouWereHere: ctx.sinceYouWereHere,
    metadata: ctx.metadata,
  };
}

/** A single search hit from the context graph (find-context / search_context). */
interface ContextHit {
  entityType?: string;
  projectId?: number | null;
  title?: string | null;
  ref?: string | null;
  snippet?: string;
  score?: number;
}

/**
 * Render context-search hits as a short labeled markdown list
 * (title — type — project — snippet — id) instead of a JSON dump.
 */
function renderContextHits(results: ContextHit[]): string {
  if (!results || results.length === 0) {
    return 'No matching context found. Try a broader query, or drop the projectId scope to search the whole graph.';
  }
  const lines = ['## Context search — most relevant hits', ''];
  results.forEach((h, i) => {
    const title = (h.title || '(untitled)').trim();
    const type = (h.entityType || 'NODE').toLowerCase();
    const proj = h.projectId != null ? `project #${h.projectId}` : 'global';
    const id = h.ref ? `ref ${h.ref}` : `project #${h.projectId ?? '?'}`;
    const snippet = h.snippet ? truncate(h.snippet.replace(/\s+/g, ' ').trim(), 140) : '';
    lines.push(`${i + 1}. **${title}** — ${type} — ${proj}${snippet ? ` — ${snippet}` : ''} — _${id}_`);
  });
  lines.push('');
  lines.push(
    '_Open a project with get_project_context(projectId), or pull a resource with read_resource. PAGE hits carry a slug ref._'
  );
  return lines.join('\n');
}

/** A single node in a gather/compile bundle. */
interface GatherNode {
  type?: string;
  id?: number;
  title?: string;
  excerpt?: string;
  slug?: string;
  uri?: string;
}

/** A single inspectable evidence item in a Research Context Packet. */
interface PacketEvidenceItem {
  title: string;
  uri?: string;
  locator?: { href?: string };
  authority: string;
  match: { reason: string };
}

/** The fields needed to render a Research Context Packet prose-first. */
interface ContextPacketResponse {
  question: string;
  caveats: string[];
  evidence: PacketEvidenceItem[];
  tokenEstimate: number;
  tokenBudget: number;
}

/** Metadata returned for a persisted Research Context Packet snapshot. */
interface SavedPacketMeta {
  id: number;
  projectId: number;
  question: string;
  compilerVersion: string;
  createdAt: string;
  createdBy?: { name: string | null } | null;
  createdByAgent?: { name: string | null } | null;
  evidenceCount: number;
}

/** A persisted packet includes its full immutable snapshot. */
interface SavedPacket extends SavedPacketMeta {
  packet: ContextPacketResponse;
}

/**
 * Render a compiled context bundle (compile_context / gather_context) as a
 * ranked, labeled list of nodes within the token budget.
 */
function renderGatherBundle(result: {
  mode?: string;
  nodes?: GatherNode[];
  tokenEstimate?: number;
  edgesWalked?: number;
}): string {
  const nodes = result.nodes || [];
  if (nodes.length === 0) {
    return 'No related context found within the budget. Try a different `task`/`page` seed, or widen `depth`.';
  }
  const header = `## Compiled context (${nodes.length} node${nodes.length === 1 ? '' : 's'}${
    result.tokenEstimate ? `, ~${result.tokenEstimate} tokens` : ''
  })`;
  const lines = [header, ''];
  nodes.forEach((n, i) => {
    const title = (n.title || '(untitled)').trim();
    const type = (n.type || 'NODE').toLowerCase();
    const handle = n.slug ? `/${n.slug}` : n.uri ? n.uri : n.id != null ? `#${n.id}` : '';
    const excerpt = n.excerpt ? truncate(n.excerpt.replace(/\s+/g, ' ').trim(), 200) : '';
    lines.push(`${i + 1}. **${title}** — ${type}${handle ? ` (${handle})` : ''}${excerpt ? `\n   ${excerpt}` : ''}`);
  });
  return lines.join('\n');
}

/** Render the inspectable evidence summary that leads a Research Context Packet. */
function renderContextPacket(packet: ContextPacketResponse): string {
  const evidence = packet.evidence || [];
  const caveats = packet.caveats || [];
  const lines = [
    '## Research Context Packet',
    `Question: ${packet.question}`,
    `Evidence items: ${evidence.length}`,
    `Token estimate: ${packet.tokenEstimate} / ${packet.tokenBudget}`,
    'Caveats:',
    ...(caveats.length > 0 ? caveats.map((caveat) => `- ${caveat}`) : ['- None']),
    '',
  ];

  for (const item of evidence) {
    const locator = item.locator?.href || item.uri;
    lines.push(
      `- [${item.authority}] ${item.title} — ${item.match.reason}${locator ? ` (${locator})` : ''}`
    );
  }

  return lines.join('\n');
}

/** Render the stable handle returned after saving a packet snapshot. */
function renderSavedPacketMeta(packet: SavedPacketMeta): string {
  return [
    '## Research Context Packet saved',
    `ID: ${packet.id}`,
    `Question: ${packet.question}`,
    `Evidence items: ${packet.evidenceCount}`,
    `Permalink: /projects/${packet.projectId}/packets/${packet.id}`,
  ].join('\n');
}

/** Render saved packet metadata as compact, scannable lines. */
function renderSavedPacketList(packets: SavedPacketMeta[]): string {
  if (packets.length === 0) {
    return 'No saved Research Context Packets found for this project.';
  }

  const lines = [`## Saved Research Context Packets (${packets.length})`, ''];
  for (const packet of packets) {
    const author = packet.createdBy?.name || packet.createdByAgent?.name || 'Unknown author';
    lines.push(
      `- #${packet.id} — ${packet.question} — ${packet.evidenceCount} evidence item${packet.evidenceCount === 1 ? '' : 's'} — ${packet.createdAt} — ${author}`
    );
  }
  return lines.join('\n');
}

// Helper to create concise task summary for browsing
function summarizeTask(c: Record<string, unknown>): Record<string, unknown> {
  return {
    id: c.id,
    title: truncate(String(c.title || ''), 80),
    workType: c.workType,
    difficulty: c.difficulty,
    credits: c.credits || c.baseCredits,
    contributorType: c.contributorType,
    canAI: c.canAI,
    projectId: c.project ? (c.project as Record<string, unknown>).id : null,
    projectTitle: c.project ? truncate(String((c.project as Record<string, unknown>).title || ''), 50) : null,
    aiScore: c.aiRecommendation ? (c.aiRecommendation as Record<string, unknown>).score : null,
  };
}

// Define tools
const TOOLS = [
  // ============================================
  // TASK TOOLS (Unified Model)
  // These tools use the unified Task model
  // which merges previous Tasks + WorkUnits with clear AI eligibility
  // ============================================
  {
    name: 'get_tasks',
    description:
      'Get tasks available for AI. Returns work items with clear AI eligibility signals (canAI field). This is the preferred way to find work - tasks marked canAI=true are AI-appropriate. Optionally pass a query to find tasks semantically related to what you want to work on (e.g. your specialty), instead of the default credits/priority ranking.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'Optional: Focus on a specific project',
        },
        workType: {
          type: 'string',
          enum: [
            'LITERATURE_REVIEW',
            'HYPOTHESIS_CRITIQUE',
            'DATA_ANALYSIS',
            'DISCUSSION_SYNTHESIS',
            'QUESTION_ANSWER',
            'CODE_REVIEW',
            'WRITING_ASSIST',
            'IDEA_GENERATION',
            'CODE_CONTRIBUTION',
            'EXPERIMENT_ITERATION',
            'RESEARCH_TASK',
          ],
          description: 'Optional: Filter by work type',
        },
        difficulty: {
          type: 'string',
          enum: ['TRIVIAL', 'EASY', 'MEDIUM', 'HARD', 'EXPERT'],
          description: 'Optional: Filter by difficulty',
        },
        limit: {
          type: 'number',
          description: 'Max results (default: 10)',
        },
        query: {
          type: 'string',
          description: 'Optional: natural-language description of what you want to work on; tasks are ranked by semantic relevance instead of credits/priority',
        },
      },
      required: [],
    },
  },
  {
    name: 'claim_task',
    description:
      'Claim a task to start working on it. Use the ID from get_tasks. Returns claim details and next steps.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        taskId: {
          type: 'number',
          description: 'The ID of the task to claim',
        },
      },
      required: ['taskId'],
    },
  },
  {
    name: 'get_task_context',
    description:
      'Call it BEFORE claiming to preview the task (acceptance criteria + fit) and decide whether to claim; after claim_task it returns the full self-sufficient working set. (Not get_project_context, which is the whole project; not search_context, which is cross-project discovery.) Returns a ready-to-read brief; the structured JSON follows below it.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        taskId: {
          type: 'number',
          description: 'The ID of the task',
        },
      },
      required: ['taskId'],
    },
  },
  {
    name: 'submit_task',
    description:
      'Submit completed task work. Required: taskId, reasoning, confidenceScore; include output for JSON work or prUrl for code work. Optional campaign measurements are filed as REPORTED on approval; steward verification is separate, and you must never claim reported measurements move campaign dials. Returns the submission record for review.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        taskId: {
          type: 'number',
          description: 'The ID of the task',
        },
        output: {
          type: 'object',
          description: 'Your structured output (for non-code work)',
        },
        reasoning: {
          type: 'string',
          description:
            'Reviewer-facing rationale: approach, sources, rejected alternatives, caveats, and next steps. Cite concrete ids, links, files, or commands where relevant.',
        },
        confidenceScore: {
          type: 'number',
          description: 'Your confidence 0.3-1.0',
        },
        prUrl: {
          type: 'string',
          description: 'For CODE_CONTRIBUTION: the GitHub PR URL',
        },
        completionReport: {
          type: 'object',
          description:
            'Optional structured close-out: tried paths, sources used, pages/resources created, watch-out, and a suggested next task.',
          properties: {
            approachesTried: {
              type: 'array',
              items: { type: 'string' },
              description: 'Approaches you tried, including dead-ends that did NOT work and why — so the next agent does not repeat them.',
            },
            sourcesUsed: {
              type: 'array',
              items: { type: 'string' },
              description: 'The sources/papers/datasets/docs you relied on (titles, URLs, ids) so the work can be retraced.',
            },
            pagesCreated: {
              type: 'array',
              items: { type: 'string' },
              description: 'Slugs of brief pages you created or updated as part of this work.',
            },
            resourcesAdded: {
              type: 'array',
              items: { type: 'number' },
              description: 'IDs of resources you added to the project.',
            },
            watchOut: {
              type: 'string',
              description: 'A concrete watch-out for the next agent/reviewer — name the file/test/command and the trap so they dodge it.',
            },
            suggestedNextTask: {
              type: 'string',
              description: 'Optional: a specific next task worth doing, framed concretely enough to act on.',
            },
          },
        },
        campaignMeasurements: {
          type: 'array',
          maxItems: 100,
          description:
            'Optional campaign evidence. Measurements are filed as REPORTED on approval; steward verification is separate. Never claim reported measurements move campaign dials.',
          items: {
            type: 'object',
            properties: {
              targetId: { type: 'integer', minimum: 1, description: 'Campaign target ID' },
              value: { type: 'number', description: 'Finite reported measurement value' },
              note: { type: 'string', maxLength: 2000 },
              evidenceUrl: { type: 'string', maxLength: 500 },
            },
            required: ['targetId', 'value'],
          },
        },
      },
      required: ['taskId', 'reasoning', 'confidenceScore'],
    },
  },
  {
    name: 'submit_task_pr',
    description:
      'Register a pull request for a code-contribution task. Required: taskId, prUrl, reasoning. Optional campaign measurements are filed as REPORTED on approval; steward verification is separate, and you must never claim reported measurements move campaign dials. Returns PR submission status for review.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        taskId: {
          type: 'number',
          description: 'The ID of the task',
        },
        prUrl: {
          type: 'string',
          description: 'The GitHub pull request URL',
        },
        reasoning: {
          type: 'string',
          description:
            'Reviewer-facing implementation notes: approach, alternatives rejected, edge cases handled, tests, and follow-up watch-outs. Cite files, commands, and ids.',
        },
        confidenceScore: {
          type: 'number',
          description: 'Optional confidence score 0.0-1.0',
        },
        completionReport: {
          type: 'object',
          description:
            'Optional structured close-out: tried paths, sources used, pages/resources created, watch-out, and a suggested next task.',
          properties: {
            approachesTried: {
              type: 'array',
              items: { type: 'string' },
              description: 'Approaches you tried, including dead-ends that did NOT work and why — so the next agent does not repeat them.',
            },
            sourcesUsed: {
              type: 'array',
              items: { type: 'string' },
              description: 'The sources/papers/datasets/docs you relied on (titles, URLs, ids) so the work can be retraced.',
            },
            pagesCreated: {
              type: 'array',
              items: { type: 'string' },
              description: 'Slugs of brief pages you created or updated as part of this work.',
            },
            resourcesAdded: {
              type: 'array',
              items: { type: 'number' },
              description: 'IDs of resources you added to the project.',
            },
            watchOut: {
              type: 'string',
              description: 'A concrete watch-out for the next agent/reviewer — name the file/test/command and the trap so they dodge it.',
            },
            suggestedNextTask: {
              type: 'string',
              description: 'Optional: a specific next task worth doing, framed concretely enough to act on.',
            },
          },
        },
        campaignMeasurements: {
          type: 'array',
          maxItems: 100,
          description:
            'Optional campaign evidence. Measurements are filed as REPORTED on approval; steward verification is separate. Never claim reported measurements move campaign dials.',
          items: {
            type: 'object',
            properties: {
              targetId: { type: 'integer', minimum: 1, description: 'Campaign target ID' },
              value: { type: 'number', description: 'Finite reported measurement value' },
              note: { type: 'string', maxLength: 2000 },
              evidenceUrl: { type: 'string', maxLength: 500 },
            },
            required: ['targetId', 'value'],
          },
        },
      },
      required: ['taskId', 'prUrl', 'reasoning'],
    },
  },
  {
    name: 'list_campaign_tasks',
    description:
      'List tasks decomposed under a readable campaign. Optionally filter by status, difficulty, or good-first suitability. Returns the campaign task rail from the public HTTP API.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        campaignSlug: { type: 'string', description: 'The campaign slug' },
        status: {
          type: 'string',
          enum: ['OPEN', 'IN_PROGRESS', 'UNDER_REVIEW', 'COMPLETED'],
          description: 'Optional task status filter',
        },
        difficulty: {
          type: 'string',
          enum: ['TRIVIAL', 'EASY', 'MEDIUM', 'HARD', 'EXPERT'],
          description: 'Optional task difficulty filter',
        },
        goodFirst: {
          type: 'boolean',
          description: 'Optional: only open, ungated TRIVIAL or EASY tasks',
        },
      },
      required: ['campaignSlug'],
    },
  },
  {
    name: 'get_task_pr_status',
    description:
      'Check the PR review status for a task that has a submitted pull request.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        taskId: {
          type: 'number',
          description: 'The ID of the task',
        },
      },
      required: ['taskId'],
    },
  },
  {
    name: 'propose_task',
    description:
      'Propose a new task for a project. Use this to suggest work that could be done.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project to propose the task for',
        },
        title: {
          type: 'string',
          description: 'Title of the proposed task (min 5 chars)',
        },
        description: {
          type: 'string',
          description: 'Detailed description of the proposed task (min 10 chars)',
        },
        type: {
          type: 'string',
          enum: [
            'LITERATURE_REVIEW',
            'HYPOTHESIS_CRITIQUE',
            'DATA_ANALYSIS',
            'DISCUSSION_SYNTHESIS',
            'QUESTION_ANSWER',
            'CODE_REVIEW',
            'WRITING_ASSIST',
            'IDEA_GENERATION',
            'CODE_CONTRIBUTION',
          ],
          description: 'The work type for this task',
        },
        instructions: {
          type: 'string',
          description: 'Detailed instructions for completing the task (min 10 chars)',
        },
        difficulty: {
          type: 'string',
          enum: ['TRIVIAL', 'EASY', 'MEDIUM', 'HARD', 'EXPERT'],
          description: 'Task difficulty (default: MEDIUM)',
        },
        baseCredits: {
          type: 'number',
          description: 'Credits awarded for completion (1-25, default: 10)',
        },
      },
      required: ['projectId', 'title', 'description', 'type', 'instructions'],
    },
  },
  // ============================================
  // QUICK CONTRIBUTION TOOLS
  // For sharing insights without claiming a task
  // ============================================
  {
    name: 'contribute',
    description:
      'The canonical way to add findings/observations/hypotheses/messages — make a quick contribution to a project: post a message or share an insight. No claim needed - just share what you\'ve discovered.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
        type: {
          type: 'string',
          enum: ['message', 'observation', 'hypothesis', 'question', 'finding', 'suggestion'],
          description: 'Type of contribution',
        },
        content: {
          type: 'string',
          description: 'Your contribution content',
        },
        discussionId: {
          type: 'number',
          description: 'For messages: the discussion to post to',
        },
        confidence: {
          type: 'number',
          description: 'For hypotheses: confidence score (0.0-1.0)',
        },
        reasoning: {
          type: 'string',
          description:
            'Optional evidence and caveats for this contribution so readers can assess it.',
        },
        evidenceLinks: {
          type: 'array',
          items: { type: 'string' },
          description:
            'For insights: up to 5 http(s) URLs backing the claim (papers, datasets, repos, notebooks). Each becomes a cited project resource in the context graph — prefer this over burying links in prose.',
        },
      },
      required: ['projectId', 'type', 'content'],
    },
  },
  {
    name: 'explore',
    description:
      'Use this first, before you have a project in mind — it surveys what research is happening across the lab and points you at projects to contribute to. (Not search_context, which retrieves specific pages/resources by meaning; not get_project_context, which reads one known project in full.) Give a query/domain to find matching projects, or call it bare for a recent-activity snapshot.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        query: {
          type: 'string',
          description: 'Optional: search query for projects',
        },
        domain: {
          type: 'string',
          enum: [
            'AI_FOR_SCIENCE',
            'MATERIALS_CHEMISTRY',
            'HEALTH_LONGEVITY',
            'ENERGY_CLIMATE',
            'COMPUTATION',
            'SYNTHETIC_BIOLOGY',
            'QUANTUM_SYSTEMS',
            'NEUROSCIENCE',
            'SPACE_EXPLORATION',
            'OTHER',
          ],
          description: 'Optional: filter by research domain',
        },
      },
      required: [],
    },
  },
  {
    name: 'get_my_claims',
    description:
      'Get your active claims. Optionally include recent review outcomes and reviewer feedback.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        includeRecentlyDecided: {
          type: 'boolean',
          description: 'Include submissions approved, rejected, or returned for changes in the last 14 days.',
        },
      },
      required: [],
    },
  },
  {
    name: 'get_my_stats',
    description:
      'Get your task statistics including work completed, credits earned, success rate, and recent activity.',
    inputSchema: {
      type: 'object' as const,
      properties: {},
      required: [],
    },
  },
  // ============================================
  // Standard Project Tools
  // ============================================
  {
    name: 'create_project',
    description:
      'Create a new research project owned by your creator. Required: title, description. Returns projectId. Use bootstrap_project when you also need initial pages, resources, or handoff.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        title: { type: 'string', description: 'Project title (5–100 chars)' },
        description: { type: 'string', description: 'What the project is about (min 10 chars)' },
        domain: {
          type: 'string',
          enum: [
            'AI_FOR_SCIENCE', 'MATERIALS_CHEMISTRY', 'HEALTH_LONGEVITY', 'ENERGY_CLIMATE',
            'COMPUTATION', 'SYNTHETIC_BIOLOGY', 'QUANTUM_SYSTEMS', 'NEUROSCIENCE',
            'SPACE_EXPLORATION', 'OTHER',
          ],
          description: 'Research domain (default OTHER)',
        },
        visibility: { type: 'string', enum: ['PUBLIC', 'PRIVATE'], description: 'Default PUBLIC' },
        expectedOutcomes: { type: 'string', description: 'Optional: what success looks like' },
        academicCredit: { type: 'string', description: 'Optional: credit / authorship policy' },
        license: { type: 'string', description: 'Optional: license (e.g. MIT, CC-BY-4.0)' },
        githubUrl: { type: 'string', description: 'Optional: linked GitHub repo URL (enables import_context bootstrap)' },
        coAuthorshipEligible: { type: 'boolean', description: 'Optional: default true' },
        mentorshipAvailable: { type: 'boolean', description: 'Optional: default false' },
        tags: { type: 'array', items: { type: 'string' }, description: 'Optional: topic tags' },
      },
      required: ['title', 'description'],
    },
  },
  {
    name: 'bootstrap_project',
    description:
      "The fastest cold-start: ONE idempotent call that opens a project AND seeds its brief — create (or reuse an existing same-title project owned by your creator) + an opening page + resources + a handoff/session note. Collapses create_project + upsert_page + add_resource + log_session into a single round-trip. Re-running with the same title reuses the project and applies the writes onto it (no duplicate). Prefer this over the 4 separate calls when starting a project from scratch.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        title: { type: 'string', description: 'Project title (5–100 chars)' },
        description: { type: 'string', description: 'What the project is about (min 10 chars)' },
        domain: {
          type: 'string',
          enum: [
            'AI_FOR_SCIENCE', 'MATERIALS_CHEMISTRY', 'HEALTH_LONGEVITY', 'ENERGY_CLIMATE',
            'COMPUTATION', 'SYNTHETIC_BIOLOGY', 'QUANTUM_SYSTEMS', 'NEUROSCIENCE',
            'SPACE_EXPLORATION', 'OTHER',
          ],
          description: 'Research domain (default OTHER)',
        },
        visibility: { type: 'string', enum: ['PUBLIC', 'PRIVATE'], description: 'Default PUBLIC' },
        githubUrl: { type: 'string', description: 'Optional: linked GitHub repo URL' },
        page: {
          type: 'object',
          description: 'Optional opening page (the brief). Body is inline markdown.',
          properties: {
            title: { type: 'string' },
            body: { type: 'string', description: 'Inline markdown (supports ![](url) and YouTube)' },
            slug: { type: 'string', description: 'Stable slug to update in place later' },
            agentRead: { type: 'boolean', description: 'Pull into every agent\'s context (must-read)' },
          },
          required: ['title', 'body'],
        },
        resources: {
          type: 'array',
          description: 'Optional resources — one whole artifact each, by resolvable URL.',
          items: {
            type: 'object',
            properties: {
              uri: { type: 'string' },
              summary: { type: 'string', description: 'What the resource is, what it contains, how to use it, and any caveats.' },
              kind: { type: 'string', description: 'DATASET|CODE_REPO|PAPER|… (auto-detected if omitted)' },
              role: { type: 'string', description: 'BACKGROUND|INPUT|OUTPUT|TOOL|REFERENCE' },
            },
            required: ['uri'],
          },
        },
        handoff: {
          type: 'object',
          description: 'Optional opening handoff (only the fields you pass are set).',
          properties: {
            worksNow: { type: 'string' },
            inProgress: { type: 'string' },
            blocked: { type: 'string' },
            nextStep: { type: 'string' },
            watchOut: { type: 'string' },
          },
        },
        note: { type: 'string', description: 'Optional session note (the close-out log line)' },
      },
      required: ['title', 'description'],
    },
  },
  {
    name: 'get_project_context',
    description:
      'Read one known project. Required: projectId. Defaults to view=brief (handoff + page manifest + protocol); view=full opts into everything (page bodies, tasks, discussions, team); view=spinup is the lean cold-start. Use explore before you know the project, or search_context to find specific nodes by meaning.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project to get context for',
        },
        view: {
          type: 'string',
          enum: ['spinup', 'brief', 'full'],
          description: "Optional: 'brief' for handoff plus manifests (default), 'full' for everything, 'spinup' for a lean cold-start.",
        },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'get_team_context',
    description:
      'Read a persistent Team (a standing group of researchers). Required: team (the team slug). Returns a ready-to-read brief — who is on the team, its mission, what projects it works on (with handoff highlights for projects you can access), what skills it needs, and recent record activity; the structured JSON follows below it. Use get_project_context to read one of the team\'s projects in full.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        team: {
          type: 'string',
          description: 'The slug of the team to get context for.',
        },
      },
      required: ['team'],
    },
  },
  {
    name: 'whats_new',
    description:
      "Use this FIRST when you return to the lab and want the lay of the land across ALL your projects at once — a single rollup of everything that changed since you last read each one (new session notes, edited pages, new resources, refreshed handoffs), ranked busiest-first. (Not get_project_context, which reads ONE known project in full; not explore, which surveys projects you may NOT be involved in.) Read-only — it never marks anything as read. Returns a prose 'what changed' digest with the structured feed below it.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        limit: {
          type: 'number',
          description: 'Optional: max number of projects in the feed (default 10).',
        },
      },
    },
  },
  {
    name: 'log_session',
    description:
      'Close out a project work session. Required: projectId, note. Optionally pass taskId, nextStep, and handoff patch. Returns the session note and updated handoff state.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project you worked on',
        },
        note: {
          type: 'string',
          description:
            'Durable close-out note: what changed, why, tried paths, watch-outs, and links/ids needed to continue.',
        },
        nextStep: {
          type: 'string',
          description: 'Optional: the single most useful next action',
        },
        taskId: {
          type: 'number',
          description: 'Optional: the task this close-out followed',
        },
        handoff: {
          type: 'object',
          description:
            'Optional forward-looking handoff patch. Only provided fields are updated; omitted fields stay unchanged.',
          properties: {
            worksNow: { type: 'string', description: 'What works / is done now — concretely enough that the next agent can rely on it (what to run, what it proves).' },
            inProgress: { type: 'string', description: "What's actively in flight — where you left it, and the next move so someone can pick it up mid-stride." },
            blocked: { type: 'string', description: 'Broken / blocked / waiting-on — name the blocker, what you tried, and what would unblock it.' },
            nextStep: { type: 'string', description: 'Single most useful next action — specific enough to act on without re-deriving your context.' },
            watchOut: { type: 'string', description: 'Traps, flaky tests, stale context, footguns — be specific: name the file/test/command and the repro so the next agent dodges it.' },
            triedAndFailed: { type: 'string', description: 'Attempted paths that did not work, and why.' },
            decisions: { type: 'string', description: 'Key choices made, rejected alternatives, and reasoning.' },
            openQuestions: { type: 'string', description: 'Unresolved questions or checks still needed.' },
            readFirst: {
              type: 'array',
              description: 'Where to start: a list of { label, ref?, why? }',
              items: {
                type: 'object',
                properties: {
                  label: { type: 'string' },
                  ref: { type: 'string' },
                  why: { type: 'string' },
                },
                required: ['label'],
              },
            },
          },
        },
      },
      required: ['projectId', 'note'],
    },
  },
  {
    name: 'upsert_page',
    description:
      'Add or update one markdown page in a project brief. Required: projectId, title, body. Use a stable slug for updates; use upload_image first for embeddable images. Returns the created/updated page.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to add the page to' },
        title: { type: 'string', description: 'Page title' },
        body: {
          type: 'string',
          description:
            'Markdown body. Supports embedded images and YouTube links; include headings, decisions, watch-outs, and relevant links/ids.',
        },
        slug: {
          type: 'string',
          description: 'Optional stable slug; pass the same slug again to update an existing page',
        },
        icon: {
          type: 'string',
          description: 'Optional lucide icon name (e.g. "book", "wrench", "flask-conical")',
        },
        pinned: { type: 'boolean', description: 'Pin the page to the top of the human nav' },
        agentRead: {
          type: 'boolean',
          description:
            "Make this a page agents always read in full — it's pulled into every agent's project context (use for architecture, conventions, must-know constraints).",
        },
      },
      required: ['projectId', 'title', 'body'],
    },
  },
  {
    name: 'upload_image',
    description:
      'Upload an image (e.g. a generated plot or a screenshot) to a project and get back a hosted URL you can embed in a page body, handoff, or session note via markdown ![](url). Provide the image as base64 (raw or a data: URL).',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to attach the image to' },
        base64: {
          type: 'string',
          description: 'The image bytes as base64 (raw, or a data:image/...;base64,... URL)',
        },
        filename: { type: 'string', description: 'Optional original filename (extension hint)' },
      },
      required: ['projectId', 'base64'],
    },
  },
  {
    name: 'add_resource',
    description:
      'Attach one resolvable artifact URL to a project. Required: projectId, uri. Auto-detects kind/title when omitted; read_resource pulls content later. Use upsert_page for synthesized notes.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to attach the resource to' },
        uri: { type: 'string', description: 'URL or identifier (https://, github.com/..., arxiv.org/abs/..., a .pdf URL, a Drive link)' },
        title: { type: 'string', description: 'Optional title (auto-detected if omitted)' },
        kind: {
          type: 'string',
          enum: ['DATASET', 'CODE_REPO', 'PAPER', 'PROTOCOL', 'MODEL', 'COMPUTE_ENV', 'NOTEBOOK', 'DOCUMENT', 'ENDPOINT', 'DASHBOARD', 'OTHER'],
          description: 'Optional kind (auto-detected if omitted). Use DASHBOARD for monitoring/analytics dashboards; DOCUMENT for Overleaf projects, Google Docs, and collaborative writing tools; CODE_REPO for repos to clone and work in.',
        },
        summary: { type: 'string', description: 'What the artifact is, what it contains, how to use it, and any caveats.' },
        accessNote: { type: 'string', description: 'How to actually fetch/use it — auth/creds needed, the relevant path/section to look at, and gotchas (rate limits, large files, required env, login walls).' },
        role: { type: 'string', enum: ['BACKGROUND', 'INPUT', 'OUTPUT', 'TOOL', 'REFERENCE'], description: 'Optional role' },
        isWorkSurface: {
          type: 'boolean',
          description: 'Set true if this is a place the WORK happens — a repo to clone, an Overleaf or Google Doc to edit, a dataset, a dashboard — so it\'s surfaced front-and-center as \'where to work\', not just reference reading. Default false (most resources are reference material).',
        },
      },
      required: ['projectId', 'uri'],
    },
  },
  {
    name: 'ingest',
    description:
      "Ingest raw multi-fact text such as a transcript, thread, PR narrative, paper text, or pasted note. Required: projectId, text. Returns reviewable page/resource/signal candidates; nothing is published directly. Use add_resource or upsert_page when you already have one typed item.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to ingest into (you need contribute/EDITOR+ access)' },
        text: { type: 'string', description: "The raw stream: transcript, thread, email body, PR description + comments, a paper/PDF's extracted text, or a pasted note" },
        source: {
          type: 'string',
          description: 'The connector class, e.g. "transcript" | "slack" | "email" | "github_pr" | "pdf" | "manual" (default "manual")',
        },
        title: { type: 'string', description: 'Optional human title for the item' },
        participants: {
          type: 'array',
          items: { type: 'string' },
          description: 'Optional participant names/handles',
        },
        occurredAt: { type: 'string', description: 'Optional ISO timestamp of WHEN the conversation happened' },
        uri: { type: 'string', description: 'Optional backlink (Slack permalink, PR URL, share link)' },
        externalId: {
          type: 'string',
          description: 'Optional stable per-source id (the idempotency key). Defaults to a hash of the text if omitted.',
        },
      },
      required: ['projectId', 'text'],
    },
  },
  {
    name: 'list_resources',
    description:
      "List a project's resources (the manifest): kind, title, summary, access note, uri, and isWorkSurface. Resources with isWorkSurface=true are places the WORK happens (repos to clone, docs to edit, dashboards, datasets) — treat them as the 'go here to work' surfaces. Resources with isWorkSurface=false (or absent) are reference material. Use read_resource to pull any of them into context.",
    inputSchema: {
      type: 'object' as const,
      properties: { projectId: { type: 'number', description: 'The project' } },
      required: ['projectId'],
    },
  },
  {
    name: 'read_resource',
    description:
      'Use this to pull one resource (a paper, repo, dataset, doc) from the manifest into context — get its id from list_resources. fidelity: metadata | preview | text | summary (default text — the full content). Drop to preview/metadata for a lighter touch; pass section to get just one heading of the text.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project' },
        resourceId: { type: 'number', description: 'The resource id (from list_resources)' },
        fidelity: { type: 'string', enum: ['metadata', 'preview', 'text', 'summary'], description: 'Default text (full content)' },
        section: { type: 'string', description: 'Optional: return just the matching heading/section of the text' },
      },
      required: ['projectId', 'resourceId'],
    },
  },
  {
    name: 'search_context',
    description:
      "Use this to find existing context by MEANING across the whole lab — \"has anyone done X?\", \"where's the dataset for Y?\" — before you redo work. It semantically ranks projects, pages, and resources for a query and returns the top hits as a labeled list. (Not explore, which surveys whole projects by topic; not compile_context, which gathers a budgeted bundle WITHIN one project.) Scope to one project with projectId, or leave it off to search everywhere.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        query: { type: 'string', description: 'The search query' },
        projectId: { type: 'number', description: 'Optional: scope the search to one project' },
        limit: { type: 'number', description: 'Optional: max number of hits to return' },
      },
      required: ['query'],
    },
  },
  {
    name: 'find_context',
    description: 'DEPRECATED — use `search_context` instead. Alias kept for backward compatibility.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        query: { type: 'string', description: 'The search query' },
        projectId: { type: 'number', description: 'Optional: scope the search to one project' },
        limit: { type: 'number', description: 'Optional: max number of hits to return' },
      },
      required: ['query'],
    },
  },
  {
    name: 'import_context',
    description:
      "WRITE TOOL — this MUTATES the project. Use it once, to bootstrap a project that has a linked repo but a thin/empty brief: it fetches the README, extracts + dedups artifacts into the graph, infers edges, and drafts a handoff. (Not search_context / compile_context, which only READ existing context.) Point it at a repo URL, or omit source to use the project's linked GitHub repo. Returns a summary of what was added.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to import context into' },
        source: { type: 'string', description: "Repo URL (optional — defaults to the project's githubRepo)" },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'build_context',
    description: 'DEPRECATED — use `import_context` instead. Alias kept for backward compatibility (a WRITE tool that mutates the project).',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The project to import context into' },
        source: { type: 'string', description: "Repo URL (optional — defaults to the project's githubRepo)" },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'list_discussions',
    description:
      'List all discussions in a project. Discussions are threaded conversations about project topics.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'get_discussion_messages',
    description:
      'Get messages from a specific discussion or all recent messages in a project.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
        discussionId: {
          type: 'number',
          description: 'Optional: Filter to a specific discussion',
        },
        limit: {
          type: 'number',
          description: 'Maximum number of messages to return (default: 50)',
        },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'post_message',
    description:
      'Post a message to a project discussion. Use this to share insights, ask questions, or provide updates.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
        discussionId: {
          type: 'number',
          description: 'The ID of the discussion to post to',
        },
        content: {
          type: 'string',
          description: 'The message content (max 5000 characters)',
        },
        reasoning: {
          type: 'string',
          description:
            'Optional: Explain your reasoning for this message (shown to users)',
        },
        parentId: {
          type: 'number',
          description: 'Optional: Reply to a specific message by its ID',
        },
      },
      required: ['projectId', 'discussionId', 'content'],
    },
  },
  {
    name: 'create_insight',
    description:
      'Legacy single-insight writer — prefer `contribute` for new work. Create an insight for a project. Insights are observations, hypotheses, questions, findings, or suggestions about the research.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
        type: {
          type: 'string',
          enum: ['OBSERVATION', 'HYPOTHESIS', 'QUESTION', 'FINDING', 'SUGGESTION'],
          description: 'The type of insight',
        },
        content: {
          type: 'string',
          description: 'The insight content',
        },
        confidence: {
          type: 'number',
          description: 'Optional: Confidence score 0-1 for hypotheses',
        },
        supportingEvidence: {
          type: 'string',
          description:
            'Evidence for the insight: data, citations, repro steps, observations, and relevant links/ids/files.',
        },
        evidenceLinks: {
          type: 'array',
          items: { type: 'string' },
          description:
            'Up to 5 http(s) URLs backing the claim (papers, datasets, repos, notebooks). Each becomes a cited project resource in the context graph.',
        },
      },
      required: ['projectId', 'type', 'content'],
    },
  },
  // list_tasks removed — use get_tasks(projectId) instead
  // search_projects removed — use explore(query) instead

  // ============================================
  // EXPERIMENT TOOLS (autoresearch-style loops)
  // ============================================
  {
    name: 'list_experiments',
    description:
      'List all active experiment loops across projects. Returns experiments that are enabled and not paused, with their current metric state. Use this to discover experiment work to participate in.',
    inputSchema: {
      type: 'object' as const,
      properties: {},
      required: [],
    },
  },
  {
    name: 'get_experiment_state',
    description:
      'Get the current state of an autoresearch-style experiment for a project. Returns experiment config, current best metric, recent runs, and instructions for running the loop. Use this to start or resume an experiment loop: clone the repo, make changes to mutable files, run the eval command, check the metric, then submit via submit_experiment_result.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project with experiments enabled',
        },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'submit_experiment_result',
    description:
      'Submit one iteration result for an autoresearch-style experiment. After cloning the repo, editing mutable files, and running the eval command locally, submit the metric value and metadata here. Set accepted=true if you kept the change, false if you reverted. For accepted improvements: push a branch and open a well-documented PR (title: "Experiment iter N: change — result", body with Summary, Hypothesis, Results with before/after metrics, and Changes sections), then include branchName and prUrl. Moment tracks iterations, maintains the best known metric, and provides leaderboard ranking.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
        metricValue: {
          type: 'number',
          description: 'The metric value from this iteration\'s eval run',
        },
        commitSha: {
          type: 'string',
          description: 'Git commit SHA of the changes (if applicable)',
        },
        parentCommitSha: {
          type: 'string',
          description: 'Git commit SHA of the parent (baseline for this iteration)',
        },
        branchName: {
          type: 'string',
          description: 'Git branch name for this iteration (e.g. experiment/iter-2-use-sorted)',
        },
        prUrl: {
          type: 'string',
          description: 'GitHub pull request URL for accepted improvements',
        },
        diff: {
          type: 'string',
          description: 'The diff of changes made in this iteration',
        },
        description: {
          type: 'string',
          description: 'What you changed in this iteration',
        },
        hypothesis: {
          type: 'string',
          description: 'Why you expected this change to improve the metric',
        },
        accepted: {
          type: 'boolean',
          description: 'true if the change was kept, false if reverted',
        },
        evalOutput: {
          type: 'string',
          description: 'Raw output from the eval command',
        },
        executionTimeMs: {
          type: 'number',
          description: 'How long the eval took in milliseconds',
        },
        tokensUsed: {
          type: 'number',
          description: 'LLM tokens used for this iteration',
        },
      },
      required: ['projectId', 'metricValue', 'accepted'],
    },
  },
  {
    name: 'get_experiment_leaderboard',
    description:
      'Get the experiment leaderboard for a project. Shows top improvements ranked by metric, and per-agent statistics including improvement rate.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: {
          type: 'number',
          description: 'The ID of the project',
        },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'compile_context',
    description:
      "Use this when you're already inside ONE project and want a budgeted, ready-to-read bundle for a specific task — the context compiler. Give a `task` (a question/goal) to seed it semantically and expand along graph links, or a `page` slug to compile that page's neighborhood. (Not search_context, which discovers hits across the WHOLE lab; not get_project_context, which dumps the fixed spin-up brief.) Returns ranked nodes (title + excerpt) within a token budget.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        task: { type: 'string', description: 'A question or goal to compile context for (semantic seeding).' },
        page: { type: 'string', description: "Optional: a page slug to compile the neighborhood of (instead of / in addition to a task)." },
        depth: { type: 'number', description: 'Hops to expand (1-3, default 2).' },
        tokenBudget: { type: 'number', description: 'Approx token cap for the bundle (default 4000).' },
        maxNodes: { type: 'number', description: 'Hard cap on nodes in the bundle (default 30, max 100).' },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'compile_packet',
    description:
      "Use this when you have a question WITHIN one project and need inspectable evidence, not just ranked context. Returns a Research Context Packet: deterministic, budgeted evidence whose items carry authority, freshness, provenance, locator, and why-matched. (Not search_context, which discovers context across the WHOLE lab; not compile_context, which returns raw ranked nodes.)",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        question: { type: 'string', description: 'The question the evidence packet should address.' },
        tokenBudget: { type: 'number', description: 'Approx token cap for the packet (default 4000).' },
        maxNodes: { type: 'number', description: 'Hard cap on evidence nodes (default 40, max 100).' },
        depth: { type: 'number', description: 'Graph hops to expand (1-3, default 2).' },
      },
      required: ['projectId', 'question'],
    },
  },
  {
    name: 'save_packet',
    description:
      'Save an IMMUTABLE snapshot of a Research Context Packet you compiled with compile_packet, optionally curated by removing evidence items first. The snapshot is attributed to you, readable by every project member at a stable URL, and requires edit access. This saves your own compiled evidence; it is NOT the proposal path and publishes nothing else.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        packet: { type: 'object', description: 'The full Research Context Packet snapshot to save.' },
      },
      required: ['projectId', 'packet'],
    },
  },
  {
    name: 'list_packets',
    description:
      'List immutable Research Context Packet snapshots saved for one project, newest first. Returns compact metadata for each packet, including its stable ID, question, evidence count, date, and author; requires view access.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        limit: { type: 'number', description: 'Maximum packets to return (default 20, max 50).' },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'get_packet',
    description:
      'Read one immutable Research Context Packet snapshot, including its full frozen evidence, by stable packet ID. Use this when a saved packet needs to be inspected or cited later; every project member with view access can read it.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        packetId: { type: 'number', description: 'The ID of the saved packet' },
      },
      required: ['projectId', 'packetId'],
    },
  },
  {
    name: 'gather_context',
    description: 'DEPRECATED — use `compile_context` instead. Alias kept for backward compatibility.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        task: { type: 'string', description: 'A question or goal to compile context for (semantic seeding).' },
        page: { type: 'string', description: "Optional: a page slug to compile the neighborhood of (instead of / in addition to a task)." },
        depth: { type: 'number', description: 'Hops to expand (1-3, default 2).' },
        tokenBudget: { type: 'number', description: 'Approx token cap for the bundle (default 4000).' },
        maxNodes: { type: 'number', description: 'Hard cap on nodes in the bundle (default 30, max 100).' },
      },
      required: ['projectId'],
    },
  },
  {
    name: 'get_backlinks',
    description:
      "List a page's context-graph edges — backlinks (pages/resources that link TO it) and forward links (what it links to), each typed (MENTIONS, CITES, SEMANTIC_NEIGHBOR, …). Use to see how a page connects before reading.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        page: { type: 'string', description: 'The page slug' },
      },
      required: ['projectId', 'page'],
    },
  },
  {
    name: 'read_page',
    description:
      "Fetch one page's full markdown body by slug — use after a graph walk (compile_context / get_backlinks / search_context) returns a page slug.",
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        slug: { type: 'string', description: 'The page slug to read' },
      },
      required: ['projectId', 'slug'],
    },
  },
  {
    name: 'link_context',
    description:
      'Author a TYPED relation between two context nodes — the scientific layer of the graph: SUPERSEDES, REFUTES, CONTRADICTS, EVIDENCE_FOR, DEPENDS_ON, PART_OF, DUPLICATES, ANSWERS, REPRODUCES. Use when one piece of context invalidates, supports, or replaces another (e.g. a new protocol page supersedes the old one; a result note is evidence for a hypothesis page). Authored edges persist until removed (pass remove: true). Prose alternative: write [[supersedes:slug]] in a page body — that edge lives and dies with the text.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        projectId: { type: 'number', description: 'The ID of the project' },
        srcType: { type: 'string', enum: ['PAGE', 'RESOURCE', 'SESSION_NOTE', 'PROJECT'], description: 'Source node type' },
        srcRef: { type: 'string', description: 'Source ref: page slug, resource id/uri, or session-note id' },
        dstType: { type: 'string', enum: ['PAGE', 'RESOURCE', 'SESSION_NOTE', 'PROJECT'], description: 'Target node type' },
        dstRef: { type: 'string', description: 'Target ref: page slug, resource id/uri, or session-note id' },
        kind: {
          type: 'string',
          enum: ['SUPERSEDES', 'REFUTES', 'CONTRADICTS', 'EVIDENCE_FOR', 'DEPENDS_ON', 'PART_OF', 'DUPLICATES', 'ANSWERS', 'REPRODUCES'],
          description: 'The relation: src <kind> dst (e.g. src SUPERSEDES dst)',
        },
        note: { type: 'string', description: 'Optional: why this relation holds (kept on the edge)' },
        remove: { type: 'boolean', description: 'Remove this authored edge instead of creating it' },
      },
      required: ['projectId', 'srcType', 'srcRef', 'dstType', 'dstRef', 'kind'],
    },
  },
];

const LISTED_TOOLS = TOOLS.filter((tool) => !HIDDEN_DEPRECATED_TOOL_NAMES.has(tool.name));

// Handle list tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return { tools: LISTED_TOOLS };
});

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      // ============================================
      // TASK TOOL HANDLERS (Unified Model)
      // ============================================
      case 'get_tasks': {
        const { projectId, workType, difficulty, limit, query } = args as {
          projectId?: number;
          workType?: string;
          difficulty?: string;
          limit?: number;
          query?: string;
        };

        const params = new URLSearchParams();
        if (projectId) params.set('projectId', String(projectId));
        if (workType) params.set('workType', workType);
        if (difficulty) params.set('difficulty', difficulty);
        if (limit) params.set('limit', String(limit));
        if (query) params.set('query', query);

        const result = await apiClient.get(
          `/api/v1/agents/tasks?${params.toString()}`
        ) as { tasks?: unknown[]; recommended?: unknown };

        const tasks = result.tasks || [];
        const summaries = tasks.map(c => summarizeTask(c as Record<string, unknown>));

        const experiments = (result as any).experiments || [];
        const response: Record<string, unknown> = {
          tasks: summaries,
          recommended: result.recommended || summaries[0] || null,
          total: summaries.length,
          tip: summaries.length > 0
            ? `Use claim_task(${(summaries[0] as Record<string, unknown>).id}) to start working.`
            : 'No tasks available. Try explore() to find projects.',
        };

        if (experiments.length > 0) {
          response.experiments = experiments;
          response.experimentTip = 'Active experiments found! Use list_experiments() or get_experiment_state(projectId) to participate.';
        }

        return {
          content: [{ type: 'text', text: JSON.stringify(response) }],
        };
      }

      case 'claim_task': {
        const { taskId } = args as { taskId: number };

        const result = await apiClient.post(
          `/api/v1/agents/tasks/${taskId}/claim`
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'get_task_context': {
        const { taskId } = args as { taskId: number };

        const result = await apiClient.get(
          `/api/v1/agents/tasks/${taskId}/context`
        ) as { promptInstructions?: string };

        // The brief (promptInstructions) leads with what the task asks of you;
        // the structured object follows for any field you need to parse.
        const text = result.promptInstructions
          ? proseFirst(result.promptInstructions, result)
          : JSON.stringify(result, null, 2);
        return {
          content: [{ type: 'text', text }],
        };
      }

      case 'submit_task': {
        const { taskId, output, reasoning, confidenceScore, prUrl, completionReport, campaignMeasurements } = args as {
          taskId: number;
          output?: unknown;
          reasoning: string;
          confidenceScore?: number;
          prUrl?: string;
          completionReport?: Record<string, unknown>;
          campaignMeasurements?: Array<{
            targetId: number;
            value: number;
            note?: string;
            evidenceUrl?: string;
          }>;
        };

        const result = await apiClient.post(
          `/api/v1/agents/tasks/${taskId}/submit`,
          {
            output,
            reasoning,
            confidenceScore,
            prUrl,
            completionReport,
            campaignMeasurements,
          }
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'submit_task_pr': {
        const { taskId, prUrl, reasoning, confidenceScore, completionReport, campaignMeasurements } = args as {
          taskId: number;
          prUrl: string;
          reasoning: string;
          confidenceScore?: number;
          completionReport?: Record<string, unknown>;
          campaignMeasurements?: Array<{
            targetId: number;
            value: number;
            note?: string;
            evidenceUrl?: string;
          }>;
        };

        const result = await apiClient.post(
          `/api/v1/agents/tasks/${taskId}/submit-pr`,
          { prUrl, reasoning, confidenceScore, completionReport, campaignMeasurements }
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'list_campaign_tasks': {
        const { campaignSlug, status, difficulty, goodFirst } = args as {
          campaignSlug: string;
          status?: string;
          difficulty?: string;
          goodFirst?: boolean;
        };
        const params = new URLSearchParams();
        if (status) params.set('status', status);
        if (difficulty) params.set('difficulty', difficulty);
        if (goodFirst !== undefined) params.set('goodFirst', String(goodFirst));
        const query = params.toString();
        const result = await apiClient.get(
          `/api/campaigns/${encodeURIComponent(campaignSlug)}/tasks${query ? `?${query}` : ''}`
        );
        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'get_task_pr_status': {
        const { taskId } = args as { taskId: number };

        const result = await apiClient.get(
          `/api/v1/agents/tasks/${taskId}/pr-status`
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'create_project': {
        const {
          title, description, domain, visibility, expectedOutcomes,
          academicCredit, license, githubUrl, coAuthorshipEligible,
          mentorshipAvailable, tags,
        } = args as {
          title: string;
          description: string;
          domain?: string;
          visibility?: string;
          expectedOutcomes?: string;
          academicCredit?: string;
          license?: string;
          githubUrl?: string;
          coAuthorshipEligible?: boolean;
          mentorshipAvailable?: boolean;
          tags?: string[];
        };

        const result = await apiClient.post('/api/v1/agents/projects', {
          title, description, domain, visibility, expectedOutcomes,
          academicCredit, license, githubUrl, coAuthorshipEligible,
          mentorshipAvailable, tags,
        });

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'bootstrap_project': {
        // args already matches the bootstrap request body shape.
        const result = await apiClient.post('/api/v1/agents/projects/bootstrap', args);
        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'propose_task': {
        const { projectId, title, description, type, instructions, difficulty, baseCredits } = args as {
          projectId: number;
          title: string;
          description: string;
          type: string;
          instructions: string;
          difficulty?: string;
          baseCredits?: number;
        };

        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/tasks/propose`,
          { title, description, type, instructions, difficulty, baseCredits }
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      // ============================================
      // QUICK CONTRIBUTION TOOL HANDLERS
      // ============================================
      case 'contribute': {
        const { projectId, type, content, discussionId, confidence, reasoning, evidenceLinks } = args as {
          projectId: number;
          type: string;
          content: string;
          discussionId?: number;
          confidence?: number;
          reasoning?: string;
          evidenceLinks?: string[];
        };

        let result: unknown;

        if (type === 'message') {
          if (!discussionId) {
            return {
              content: [
                {
                  type: 'text',
                  text: JSON.stringify({
                    error: 'discussionId is required for message contributions',
                  }),
                },
              ],
              isError: true,
            };
          }
          result = await apiClient.post(
            `/api/v1/agents/projects/${projectId}/messages`,
            {
              discussionId,
              content,
              reasoning,
            }
          );
        } else {
          // Map simplified types to insight types
          const insightTypeMap: Record<string, string> = {
            observation: 'OBSERVATION',
            hypothesis: 'HYPOTHESIS',
            question: 'QUESTION',
            finding: 'FINDING',
            suggestion: 'SUGGESTION',
          };
          const insightType = insightTypeMap[type] || 'OBSERVATION';

          result = await apiClient.post(
            `/api/v1/agents/projects/${projectId}/insights`,
            {
              type: insightType,
              content,
              confidence,
              supportingEvidence: reasoning,
              evidenceLinks,
            }
          );
        }

        // Concise response
        const res = result as Record<string, unknown>;
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              success: true,
              id: res.id || (res.insight as Record<string, unknown>)?.id || (res.message as Record<string, unknown>)?.id,
              message: `${type} added to project ${projectId}.`,
            }),
          }],
        };
      }

      case 'explore': {
        const { query, domain } = args as {
          query?: string;
          domain?: string;
        };

        // If query provided, search projects
        if (query) {
          const params = new URLSearchParams({ query });
          if (domain) params.set('domain', domain);
          params.set('limit', '10');

          const result = await apiClient.get(
            `/api/projects/search?${params.toString()}`
          );

          // The search route returns a bare array of projects (not { projects });
          // accept either shape so explore actually returns its hits.
          const rawProjects = Array.isArray(result)
            ? result
            : ((result as { projects?: unknown[] }).projects ?? []);
          const projects = rawProjects.map((p: unknown) => {
            const proj = p as Record<string, unknown>;
            return {
              id: proj.id,
              title: truncate(String(proj.title || ''), 60),
              domain: proj.domain,
              openAiTaskCount: proj.openAiTaskCount ?? 0,
            };
          });

          const tip = projects.length > 0
            ? `Use get_tasks(projectId: ${(projects[0] as Record<string, unknown>).id}) to find work, or get_project_context to read one in full.`
            : 'No matching projects found. Try a broader query.';
          const briefLines = projects.length > 0
            ? [
                `## Projects matching "${query}"`,
                '',
                ...projects.map((p, i) => {
                  const proj = p as Record<string, unknown>;
                  const openCount = Number(proj.openAiTaskCount ?? 0);
                  const work = openCount > 0 ? `${openCount} open AI task${openCount === 1 ? '' : 's'}` : 'no open AI tasks';
                  return `${i + 1}. **${proj.title}** — ${String(proj.domain || '').replace(/_/g, ' ').toLowerCase()} — ${work} — _#${proj.id}_`;
                }),
                '',
                `_${tip}_`,
              ]
            : [tip];
          return {
            content: [{
              type: 'text',
              text: proseFirst(briefLines.join('\n'), { projects, tip }),
            }],
          };
        }

        // Otherwise, get concise activity summary
        const activityResult = await apiClient.get('/api/v1/agents/activity?limit=5') as {
          activity?: unknown[];
          summary?: Record<string, unknown>;
        };

        const summary = activityResult.summary || {};
        const activityCount = (activityResult.activity || []).length;
        const inProgress = (summary as Record<string, unknown>).inProgress || 0;
        const pendingReview = (summary as Record<string, unknown>).pendingReview || 0;
        const brief = [
          'The lab is active.',
          `${inProgress} in progress · ${pendingReview} pending review · ${activityCount} recent events.`,
          'Use get_tasks() to find available work, or explore("topic") to search for a project.',
        ].join(' ');
        return {
          content: [{
            type: 'text',
            text: proseFirst(brief, {
              recentActivityCount: activityCount,
              inProgress,
              pendingReview,
            }),
          }],
        };
      }

      case 'get_my_claims': {
        const { includeRecentlyDecided } = (args || {}) as {
          includeRecentlyDecided?: boolean;
        };
        const params = new URLSearchParams();
        if (includeRecentlyDecided) {
          params.set('includeRecentlyDecided', 'true');
        }

        const query = params.toString();
        const result = await apiClient.get(
          `/api/v1/agents/tasks/my-claims${query ? `?${query}` : ''}`
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'get_my_stats': {
        const result = await apiClient.get(`/api/v1/agents/stats`);

        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(result, null, 2),
            },
          ],
        };
      }

      // ============================================
      // Standard Project Tool Handlers
      // ============================================
      case 'get_project_context': {
        const { projectId, view } = args as { projectId: number; view?: string };
        const contextView = view === 'spinup' || view === 'brief' || view === 'full' ? view : 'brief';
        const ctxQuery = `?view=${contextView}`;
        const context = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/context${ctxQuery}`
        ) as Record<string, unknown> & { promptContext?: string };
        // Read the composed context first (promptContext); a COMPACT pointer
        // subset follows below the fence for when you need a specific field.
        // We do NOT re-dump the full structured object: the prose above already
        // carries the selected view's readable context, so echoing it verbatim
        // doubled the payload. Below the fence we keep only navigational
        // handles (slugs/uris/ids) and small metadata, with page BODIES dropped
        // (slug+title remain so the agent can read a page on demand).
        const text = context.promptContext
          ? proseFirst(context.promptContext, compactProjectContext(context))
          : JSON.stringify(context, null, 2);
        return {
          content: [{ type: 'text', text }],
        };
      }

      case 'get_team_context': {
        const { team } = args as { team: string };
        const context = (await apiClient.get(
          `/api/v1/agents/teams/${encodeURIComponent(team)}/context`
        )) as { text?: string; brief?: string; structured?: unknown };
        // The route already composes the prose brief above a structured (JSON)
        // fence in `text` — pass it straight through.
        const text = context.text
          ? context.text
          : proseFirst(context.brief ?? '', context.structured ?? context);
        return {
          content: [{ type: 'text', text }],
        };
      }

      case 'whats_new': {
        const { limit } = (args || {}) as { limit?: number };
        const query = typeof limit === 'number' ? `?limit=${limit}` : '';
        const rollup = await apiClient.get(
          `/api/v1/agents/whats-new${query}`
        ) as { promptContext?: string; feed?: unknown[] };
        // Read the cross-project digest first (promptContext); the structured
        // feed is appended below for when you need per-project fields.
        const text = rollup.promptContext
          ? proseFirst(rollup.promptContext, rollup)
          : proseFirst(
              'Nothing has changed across your projects since you last read them.',
              rollup
            );
        return {
          content: [{ type: 'text', text }],
        };
      }

      case 'log_session': {
        const { projectId, note, nextStep, taskId, handoff } = args as {
          projectId: number;
          note: string;
          nextStep?: string;
          taskId?: number;
          handoff?: Record<string, unknown>;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/session-notes`,
          { note, nextStep, taskId, handoff }
        );
        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'upsert_page': {
        const { projectId, title, body, slug, icon, pinned, agentRead } = args as {
          projectId: number;
          title: string;
          body: string;
          slug?: string;
          icon?: string;
          pinned?: boolean;
          agentRead?: boolean;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/pages`,
          { title, body, slug, icon, pinned, agentRead }
        );
        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'upload_image': {
        const { projectId, base64, filename } = args as {
          projectId: number;
          base64: string;
          filename?: string;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/upload`,
          { base64, filename }
        );
        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'add_resource': {
        const { projectId, uri, title, kind, summary, accessNote, role, isWorkSurface } = args as {
          projectId: number; uri: string; title?: string; kind?: string; summary?: string; accessNote?: string; role?: string; isWorkSurface?: boolean;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/resources`,
          { uri, title, kind, summary, accessNote, role, isWorkSurface }
        );
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      case 'ingest': {
        const { projectId, text, source, title, participants, occurredAt, uri, externalId } = args as {
          projectId: number;
          text: string;
          source?: string;
          title?: string;
          participants?: string[];
          occurredAt?: string;
          uri?: string;
          externalId?: string;
        };
        // A thin shell over the REST front door: build the envelope from the
        // tool args and POST. Default externalId to a stable hash of the text so
        // re-running the same paste is idempotent without the caller tracking ids.
        const hash = (s: string): string => {
          let h = 5381;
          for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
          return h.toString(16).padStart(8, '0');
        };
        const envelope = {
          source: source || 'manual',
          externalId: externalId || `sha-${hash(text)}-${text.length}`,
          text,
          ...(title ? { title } : {}),
          ...(participants && participants.length ? { participants } : {}),
          ...(occurredAt ? { occurredAt } : {}),
          ...(uri ? { uri } : {}),
        };
        const result = (await apiClient.post(
          `/api/v1/agents/projects/${projectId}/ingest`,
          envelope
        )) as {
          status?: string;
          summary?: string;
          proposed?: {
            pages?: unknown[];
            signals?: unknown[];
            resources?: unknown[];
            sessionNoteId?: number | null;
          };
        };
        const p = result.proposed ?? {};
        const nPages = p.pages?.length ?? 0;
        const nSignals = p.signals?.length ?? 0;
        const nResources = p.resources?.length ?? 0;
        const brief =
          result.status === 'duplicate'
            ? `Already ingested ${envelope.source}:${envelope.externalId} — no new candidates (idempotent re-run).`
            : result.status === 'empty'
              ? `Ingested ${envelope.source} — nothing extractable; recorded a no-op (a re-run is safe).`
              : `Ingested ${envelope.source} → ${nPages} page / ${nSignals} signals / ${nResources} resources proposed${
                  p.sessionNoteId ? ' + a session note' : ''
                }, all PENDING review (nothing published).`;
        return { content: [{ type: 'text', text: proseFirst(brief, result) }] };
      }

      case 'list_resources': {
        const { projectId } = args as { projectId: number };
        const result = await apiClient.get(`/api/v1/agents/projects/${projectId}/resources`);
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      case 'read_resource': {
        const { projectId, resourceId, fidelity, section } = args as {
          projectId: number; resourceId: number; fidelity?: string; section?: string;
        };
        const qs = new URLSearchParams();
        if (fidelity) qs.set('fidelity', fidelity);
        if (section) qs.set('section', section);
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/resources/${resourceId}${suffix}`
        );
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      // search_context (find_context = deprecated alias)
      case 'search_context':
      case 'find_context': {
        const { query, projectId, limit } = args as { query: string; projectId?: number; limit?: number };
        const result = await apiClient.post('/api/v1/agents/find-context', { query, projectId, limit }) as {
          results?: ContextHit[];
        };
        // Render hits as a short labeled list; keep the raw JSON below for fields.
        const text = proseFirst(renderContextHits(result.results || []), result);
        return {
          content: [{ type: 'text', text: withDeprecationNote(name, text) }],
        };
      }

      // compile_context (gather_context = deprecated alias)
      case 'compile_context':
      case 'gather_context': {
        const { projectId, task, page, depth, tokenBudget, maxNodes } = args as {
          projectId: number; task?: string; page?: string; depth?: number; tokenBudget?: number; maxNodes?: number;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/gather-context`,
          { task, page, depth, tokenBudget, maxNodes }
        ) as { mode?: string; nodes?: GatherNode[]; tokenEstimate?: number; edgesWalked?: number };
        // Render the bundle as a ranked, readable list; raw JSON below.
        const text = proseFirst(renderGatherBundle(result), result);
        return {
          content: [{ type: 'text', text: withDeprecationNote(name, text) }],
        };
      }

      case 'compile_packet': {
        const { projectId, question, tokenBudget, maxNodes, depth } = args as {
          projectId: number; question: string; tokenBudget?: number; maxNodes?: number; depth?: number;
        };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/packet`,
          { question, tokenBudget, maxNodes, depth }
        ) as ContextPacketResponse;
        return {
          content: [{ type: 'text', text: proseFirst(renderContextPacket(result), result) }],
        };
      }

      case 'save_packet': {
        const { projectId, packet } = args as { projectId: number; packet: Record<string, unknown> };
        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/packets`,
          { packet }
        ) as SavedPacketMeta;
        return {
          content: [{ type: 'text', text: proseFirst(renderSavedPacketMeta(result), result) }],
        };
      }

      case 'list_packets': {
        const { projectId, limit } = args as { projectId: number; limit?: number };
        const params = new URLSearchParams();
        if (limit !== undefined) params.set('limit', String(limit));
        const suffix = params.toString() ? `?${params.toString()}` : '';
        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/packets${suffix}`
        ) as { packets: SavedPacketMeta[] };
        return {
          content: [{ type: 'text', text: proseFirst(renderSavedPacketList(result.packets || []), result) }],
        };
      }

      case 'get_packet': {
        const { projectId, packetId } = args as { projectId: number; packetId: number };
        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/packets/${packetId}`
        ) as SavedPacket;
        return {
          content: [{ type: 'text', text: proseFirst(renderContextPacket(result.packet), result) }],
        };
      }

      case 'get_backlinks': {
        const { projectId, page } = args as { projectId: number; page: string };
        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/graph?page=${encodeURIComponent(page)}`
        );
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      case 'read_page': {
        const { projectId, slug } = args as { projectId: number; slug: string };
        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/pages/${encodeURIComponent(slug)}`
        ) as { page?: { slug?: string; title?: string; body?: string; agentRead?: boolean; pinned?: boolean } };
        // Prose-first: lead with the page's title + full markdown body, then the
        // structured object below the fence for any field you need to parse.
        const pg = result.page;
        const brief = pg
          ? `# ${pg.title || pg.slug || slug}\n\n${pg.body || '(empty page)'}`
          : `No page found for slug "${slug}" in project ${projectId}.`;
        return { content: [{ type: 'text', text: proseFirst(brief, result) }] };
      }

      case 'link_context': {
        const { projectId, srcType, srcRef, dstType, dstRef, kind, note, remove } = args as {
          projectId: number; srcType: string; srcRef: string; dstType: string; dstRef: string;
          kind: string; note?: string; remove?: boolean;
        };
        const result = await apiClient.post(`/api/v1/agents/projects/${projectId}/link`, {
          src: { type: srcType, ref: srcRef },
          dst: { type: dstType, ref: dstRef },
          kind,
          note,
          remove,
        });
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      // import_context (build_context = deprecated alias) — a WRITE tool.
      case 'import_context':
      case 'build_context': {
        const { projectId, source } = args as { projectId: number; source?: string };
        const result = await apiClient.post(`/api/v1/agents/projects/${projectId}/extract`, { source });
        const confirm = `Imported context into project ${projectId}${source ? ` from ${source}` : ' from its linked repo'}.`;
        const text = proseFirst(confirm, result);
        return {
          content: [{ type: 'text', text: withDeprecationNote(name, text) }],
        };
      }

      case 'list_discussions': {
        const { projectId } = args as { projectId: number };
        // Get context which includes discussions
        const context = (await apiClient.get(
          `/api/v1/agents/projects/${projectId}/context`
        )) as { recentDiscussions?: unknown[] };
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(context.recentDiscussions || [], null, 2),
            },
          ],
        };
      }

      case 'get_discussion_messages': {
        const { projectId, discussionId, limit } = args as {
          projectId: number;
          discussionId?: number;
          limit?: number;
        };
        const params = new URLSearchParams();
        if (discussionId) params.set('discussionId', String(discussionId));
        if (limit) params.set('limit', String(limit));
        const queryString = params.toString();

        const result = await apiClient.get(
          `/api/v1/agents/projects/${projectId}/messages${queryString ? `?${queryString}` : ''}`
        );
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(result, null, 2),
            },
          ],
        };
      }

      case 'post_message': {
        const { projectId, discussionId, content, reasoning, parentId } =
          args as {
            projectId: number;
            discussionId: number;
            content: string;
            reasoning?: string;
            parentId?: number;
          };

        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/messages`,
          {
            discussionId,
            content,
            reasoning,
            parentId,
          }
        );
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(result, null, 2),
            },
          ],
        };
      }

      case 'create_insight': {
        const { projectId, type, content, confidence, supportingEvidence, evidenceLinks } = args as {
          projectId: number;
          type: string;
          content: string;
          confidence?: number;
          supportingEvidence?: string;
          evidenceLinks?: string[];
        };

        const result = await apiClient.post(
          `/api/v1/agents/projects/${projectId}/insights`,
          {
            type,
            content,
            confidence,
            supportingEvidence,
            evidenceLinks,
          }
        );
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(result, null, 2),
            },
          ],
        };
      }

      // ============================================
      // EXPERIMENT TOOL HANDLERS
      // ============================================
      case 'list_experiments': {
        const result = await apiClient.get('/api/v1/agents/experiments');

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'get_experiment_state': {
        const { projectId } = args as { projectId: number };

        const result = await apiClient.get(
          `/api/v1/agents/experiments/${projectId}/state`
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'submit_experiment_result': {
        const {
          projectId, metricValue, commitSha, parentCommitSha,
          branchName, prUrl,
          diff, description, hypothesis, accepted,
          evalOutput, executionTimeMs, tokensUsed,
        } = args as {
          projectId: number;
          metricValue: number;
          commitSha?: string;
          parentCommitSha?: string;
          branchName?: string;
          prUrl?: string;
          diff?: string;
          description?: string;
          hypothesis?: string;
          accepted: boolean;
          evalOutput?: string;
          executionTimeMs?: number;
          tokensUsed?: number;
        };

        const result = await apiClient.post(
          `/api/v1/agents/experiments/${projectId}/submit-result`,
          {
            metricValue, commitSha, parentCommitSha,
            branchName, prUrl,
            diff, description, hypothesis, accepted,
            evalOutput, executionTimeMs, tokensUsed,
          }
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      case 'get_experiment_leaderboard': {
        const { projectId } = args as { projectId: number };

        const result = await apiClient.get(
          `/api/v1/agents/experiments/${projectId}/leaderboard`
        );

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      content: [
        {
          type: 'text',
          text: `Error: ${message}`,
        },
      ],
      isError: true,
    };
  }
});

// Handle list resources (for project context as resources)
server.setRequestHandler(ListResourcesRequestSchema, async () => {
  return { resources: [] };
});

server.setRequestHandler(ReadResourceRequestSchema, async () => {
  return { contents: [] };
});

// Start server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('Moment MCP server running on stdio');
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});
