import { lspTools } from "../tools/lsp-tools.js";
import type { ToolDef } from "./types.js";

function tagCategory(tools: ToolDef[], category: string): ToolDef[] {
  return tools.map((t) => ({ ...t, category }));
}

export const allTools: ToolDef[] = [
  ...tagCategory(lspTools as unknown as ToolDef[], "lsp"),
];
