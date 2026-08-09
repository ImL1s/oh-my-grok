import { Command } from "commander"
export function createMcpOAuthCommand(): Command {
  const mcp = new Command("mcp").description("MCP")
  const oauth = new Command("oauth").description("OAuth")
  oauth.command("login <server-name>").description("login")
  oauth.command("logout <server-name>").description("logout")
  oauth.command("status [server-name]").description("status")
  mcp.addCommand(oauth)
  return mcp
}
