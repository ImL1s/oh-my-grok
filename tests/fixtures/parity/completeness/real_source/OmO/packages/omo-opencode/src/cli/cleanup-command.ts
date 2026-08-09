import type { Command } from "commander"
export function configureCleanupCommand(program: Command): void {
  program.command("cleanup").alias("uninstall").description("cleanup")
}
