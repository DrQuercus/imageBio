/** What a person copies to plug a script or an assistant into imageBio (ADR 015): the MCP
 *  configuration and the command line, filled with the address of this server. The key itself
 *  is never put in them: it is shown once, and the person pastes it where it goes. */

export const KEY_PLACEHOLDER = "ibk_…";
export const SCRIPT_PLACEHOLDER = "/chemin/vers/imagebio_ai.py";
export const DAY_CHOICES = [7, 30, 90, 180, 366] as const;

/** The variables the client reads. In standalone mode the API answers this computer without a key. */
export function clientEnvironment(origin: string, standalone: boolean): Record<string, string> {
  return standalone ? { IMAGEBIO_URL: origin } : { IMAGEBIO_URL: origin, IMAGEBIO_KEY: KEY_PLACEHOLDER };
}

/** The ``mcpServers`` entry of an MCP client (Claude Desktop, and most others). */
export function mcpConfig(origin: string, standalone: boolean): string {
  const server = { command: "python3", args: [SCRIPT_PLACEHOLDER, "mcp"], env: clientEnvironment(origin, standalone) };
  return JSON.stringify({ mcpServers: { imagebio: server } }, null, 2);
}

/** The same server added to Claude Code. */
export function claudeCodeCommand(origin: string, standalone: boolean): string {
  const env = Object.entries(clientEnvironment(origin, standalone))
    .map(([name, value]) => `--env ${name}=${value}`)
    .join(" ");
  return `claude mcp add --transport stdio imagebio ${env} -- python3 ${SCRIPT_PLACEHOLDER} mcp`;
}

/** A first command, to check that the key and the address are right. */
export function shellExample(origin: string, standalone: boolean): string {
  const env = Object.entries(clientEnvironment(origin, standalone))
    .map(([name, value]) => `export ${name}=${value}`)
    .join("\n");
  return `${env}\npython3 imagebio_ai.py sessions`;
}

/** What is left of a key: "12 jours", "expirée". */
export function lifeLeft(expires: number, now: number): string {
  const days = (expires - now) / 86400;
  if (days <= 0) return "expirée";
  if (days < 1) return "moins d'un jour";
  const whole = Math.ceil(days); // a key of 90 days says 90 when it is made
  return whole === 1 ? "1 jour" : `${whole} jours`;
}
