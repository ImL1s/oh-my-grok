import type { Command } from "commander"
export function configureRuntimeCommands(program: Command): void {
  program.command("ulw-loop [args...]").description("ulw")
  program.command("version").description("version")
}
