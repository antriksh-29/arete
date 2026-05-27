import pino from "pino";
import { config } from "./config.js";

export const log = pino({
  level: config.LOG_LEVEL,
  base: { service: "arete-router" },
  timestamp: pino.stdTimeFunctions.isoTime,
});
