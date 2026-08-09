import { Command } from "commander"
import { configureCleanupCommand } from "./cleanup-command"
import { configureRuntimeCommands } from "./runtime-commands"
import { createMcpOAuthCommand } from "./mcp-oauth"

const program = new Command()
program.name("oh-my-opencode")
program.command("install").alias("setup").description("install")
program.command("run <message>").description("run")
program.command("doctor").description("doctor")
configureCleanupCommand(program)
configureRuntimeCommands(program)
program.addCommand(createMcpOAuthCommand())
export { program }
