import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");
  startKeepAlive(port);
});

function startKeepAlive(serverPort: number) {
  const INTERVAL_MS = 4 * 60 * 1000;

  setInterval(async () => {
    try {
      await fetch(`http://localhost:${serverPort}/api/healthz`, {
        signal: AbortSignal.timeout(10_000),
      });
      logger.info("keep-alive ping ok");
    } catch (err) {
      logger.warn({ err }, "keep-alive ping failed");
    }
  }, INTERVAL_MS);

  logger.info({ intervalMinutes: 4 }, "keep-alive started");
}
