import { z } from "zod"

export const HookNameSchema = z.enum([
  "todo-continuation-enforcer",
  "comment-checker",
  "rules-injector",
  "background-notification",
  "hashline-read-enhancer",
])
