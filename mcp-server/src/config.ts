import fs from 'node:fs';
import path from 'node:path';

export interface MomentServerConfig {
  apiKey: string;
  apiUrl: string;
  debug: boolean;
}

export const DEFAULT_API_URL = 'https://scientificintelligence.org';

interface RawConfigFile {
  apiKey?: string;
  apiUrl?: string;
}

type ArgValue = string | boolean;

function parseArgs(argv: string[]): Record<string, ArgValue> {
  const args: Record<string, ArgValue> = {};

  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith('--')) {
      continue;
    }

    const [flag, inlineValue] = token.split('=', 2);
    const key = flag.slice(2);

    if (inlineValue !== undefined) {
      args[key] = inlineValue;
      continue;
    }

    const next = argv[i + 1];
    if (next && !next.startsWith('--')) {
      args[key] = next;
      i++;
    } else {
      args[key] = true;
    }
  }

  return args;
}

function readConfigFile(configPath: string | undefined): RawConfigFile {
  if (!configPath) {
    return {};
  }

  const resolved = path.resolve(configPath);
  if (!fs.existsSync(resolved)) {
    console.warn(`Config file not found at ${resolved}`);
    return {};
  }

  try {
    const raw = fs.readFileSync(resolved, 'utf8');
    return JSON.parse(raw) as RawConfigFile;
  } catch (error) {
    console.warn(`Unable to read config file ${resolved}:`, error);
    return {};
  }
}

function coalesce(...values: Array<string | undefined>): string | undefined {
  for (const value of values) {
    if (typeof value === 'string' && value.trim().length > 0) {
      return value.trim();
    }
  }
  return undefined;
}

function normalizeUrl(baseUrl: string): string {
  return baseUrl.replace(/\/$/, '');
}

function coerceBoolean(value: ArgValue | undefined): boolean {
  if (value === undefined) {
    return false;
  }

  if (typeof value === 'boolean') {
    return value;
  }

  const normalized = value.trim().toLowerCase();
  return ['1', 'true', 'yes', 'on'].includes(normalized);
}

function formatApiKeyFingerprint(apiKey: string): string {
  const normalized = apiKey.replace(/[^a-zA-Z0-9]/g, '');
  if (normalized.length <= 12) {
    return normalized || 'unavailable';
  }
  return `${normalized.slice(0, 6)}…${normalized.slice(-6)}`;
}

export function loadConfig(argv = process.argv.slice(2)): MomentServerConfig {
  const args = parseArgs(argv);
  const configPath =
    (args.config as string | undefined) ?? process.env.MOMENT_MCP_CONFIG;
  const fileConfig = readConfigFile(configPath);

  // Read API key from args, config file, or environment (in priority order)
  const apiKey = coalesce(
    args['api-key'] as string | undefined,
    fileConfig.apiKey,
    process.env.MOMENT_MCP_API_KEY,
    process.env.MOMENT_API_KEY,
    process.env.MOMENT_MCP_KEY
  );

  if (!apiKey) {
    throw new Error(
      'Missing Moment API key. Set MOMENT_API_KEY environment variable, ' +
      'use --api-key flag, or add apiKey to config file.'
    );
  }

  if (apiKey.length < 32) {
    throw new Error('API key appears to be malformed. Expected at least 32 characters.');
  }

  const apiUrl = normalizeUrl(
    coalesce(
      args['api-url'] as string | undefined,
      fileConfig.apiUrl,
      process.env.MOMENT_MCP_API_URL,
      process.env.MCP_MOMENT_API_URL,
      process.env.MOMENT_API_URL,
      DEFAULT_API_URL
    )!
  );

  const debug = coerceBoolean(args.debug ?? process.env.MOMENT_MCP_DEBUG);

  console.error(
    `[moment-mcp] Target API: ${apiUrl} | API key fingerprint: ${formatApiKeyFingerprint(apiKey)}`
  );

  return {
    apiKey,
    apiUrl,
    debug,
  };
}
