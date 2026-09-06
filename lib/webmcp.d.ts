interface WebMcpTool {
  name: string;
  title?: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations?: {
    readOnlyHint?: boolean;
    untrustedContentHint?: boolean;
  };
  execute(input: unknown): Promise<unknown>;
}

interface WebMcpModelContext {
  registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }): void | Promise<void>;
}

interface Document {
  readonly modelContext?: WebMcpModelContext;
}
