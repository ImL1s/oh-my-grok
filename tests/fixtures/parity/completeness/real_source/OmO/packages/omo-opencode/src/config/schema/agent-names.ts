import { z } from "zod"

export const BuiltinAgentNameSchema = z.enum([
  "sisyphus",
  "oracle",
  "explore",
])

export const BuiltinSkillNameSchema = z.enum([
  "git-master",
  "frontend",
  "team-mode",
])
