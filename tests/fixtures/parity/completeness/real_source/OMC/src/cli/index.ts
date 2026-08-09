import { Command } from "commander";

const program = new Command();
program
  .command("hello")
  .description("synthetic hello");
program
  .command("team")
  .description("synthetic team");

program.parse();
