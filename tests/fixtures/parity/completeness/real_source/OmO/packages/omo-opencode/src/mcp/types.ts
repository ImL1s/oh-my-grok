import { z } from "zod"

export const McpNameSchema = z.enum(["websearch", "lsp", "codegraph"])

export type McpName = z.infer<typeof McpNameSchema>
