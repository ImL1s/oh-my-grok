import type { AgentConfig } from "./types.js";

const exploreAgent: AgentConfig = {
  name: "explore",
  description: "synthetic explore agent",
};

export function getAgentDefinitions(): AgentConfig[] {
  const agents: Record<string, AgentConfig> = {
    explore: exploreAgent,
  };
  return Object.values(agents);
}
