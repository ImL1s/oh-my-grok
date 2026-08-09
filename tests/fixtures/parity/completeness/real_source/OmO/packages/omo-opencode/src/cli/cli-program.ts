import { Command } from "commander"
import { configureCleanupCommand } from "./cleanup-command"
import { configureRuntimeCommands } from "./runtime-commands"

const program = new Command()
program.name("oh-my-opencode")
program.command("install").description("install")
program.command("run <message>").description("run")
program.command("doctor").description("doctor")
configureCleanupCommand(program)
configureRuntimeCommands(program)
export { program }
