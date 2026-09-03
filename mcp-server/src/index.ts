#!/usr/bin/env node
/**
 * Moment MCP Server — stdio bootstrap.
 *
 * This file is the bin (`dist/index.js`) and nothing else: it parses the
 * environment, builds the server from `createMomentMcpServer` (src/server.ts),
 * and connects it to stdio. Tools, renderers, and the dispatcher live in
 * src/server.ts so the hosted /api/mcp endpoint can reuse them verbatim.
 *
 * Configuration:
 *   --api-key / MOMENT_MCP_API_KEY / MOMENT_API_KEY - Your Moment agent API key
 *   --api-url / MOMENT_MCP_API_URL / MOMENT_API_URL - Base URL for Moment API (default: https://www.scientific-intelligence.com)
 *   --config / MOMENT_MCP_CONFIG - Optional path to JSON file with { "apiKey", "apiUrl" }
 */

import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { loadConfig } from './config.js';
import { createMomentMcpServer } from './server.js';

export { createMomentMcpServer } from './server.js';
export type { MomentServerOptions } from './server.js';

async function main() {
  const config = loadConfig();
  const server = createMomentMcpServer(config);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('Moment MCP server running on stdio');
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});
