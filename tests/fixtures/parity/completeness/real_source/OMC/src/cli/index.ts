import { Command } from "commander";

const program = new Command();
program
  .command("hello")
  .description("synthetic hello");
program
  .command("team")
  .description("synthetic team");
program
  .command("remove <path>")
  .alias("rm")
  .description("synthetic remove (alias rm)");
program
  .command("session")
  .alias("sessions")
  .description("synthetic session (alias sessions)");

program.parse();
