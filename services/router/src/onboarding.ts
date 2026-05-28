// Helpers shared by the onboarding FSM (in webhook.ts).
//
// - inferTimezoneFromCountry: map a free-text country answer to an IANA
//   timezone string. Lookup covers ~25 most-likely-tenant countries; falls
//   through to UTC for anything else. Tenant can correct anytime via chat.
// - renderUserMd: build the USER.md content from the captured FSM fields,
//   suitable for writing into the volume's identity/USER.md.
// - writeUserMdToContainer: docker exec a `cat > USER.md` into the running
//   tenant container. Triggers the syncer to commit + push.
import { spawn } from "child_process";
import { log } from "./log.js";

// ── Country → IANA timezone ─────────────────────────────────────────────

const COUNTRY_TO_TZ: Record<string, string> = {
  // Asia
  india: "Asia/Kolkata", in: "Asia/Kolkata", bharat: "Asia/Kolkata",
  pakistan: "Asia/Karachi", pk: "Asia/Karachi",
  bangladesh: "Asia/Dhaka", bd: "Asia/Dhaka",
  "sri lanka": "Asia/Colombo", lk: "Asia/Colombo",
  nepal: "Asia/Kathmandu", np: "Asia/Kathmandu",
  china: "Asia/Shanghai", cn: "Asia/Shanghai",
  japan: "Asia/Tokyo", jp: "Asia/Tokyo",
  "south korea": "Asia/Seoul", korea: "Asia/Seoul", kr: "Asia/Seoul",
  singapore: "Asia/Singapore", sg: "Asia/Singapore",
  malaysia: "Asia/Kuala_Lumpur", my: "Asia/Kuala_Lumpur",
  indonesia: "Asia/Jakarta", id: "Asia/Jakarta",
  thailand: "Asia/Bangkok", th: "Asia/Bangkok",
  vietnam: "Asia/Ho_Chi_Minh", vn: "Asia/Ho_Chi_Minh",
  philippines: "Asia/Manila", ph: "Asia/Manila",
  uae: "Asia/Dubai", "united arab emirates": "Asia/Dubai", ae: "Asia/Dubai",
  "saudi arabia": "Asia/Riyadh", sa: "Asia/Riyadh",
  israel: "Asia/Jerusalem", il: "Asia/Jerusalem",
  turkey: "Europe/Istanbul", tr: "Europe/Istanbul",
  // Europe
  uk: "Europe/London", "united kingdom": "Europe/London",
  britain: "Europe/London", england: "Europe/London", scotland: "Europe/London",
  ireland: "Europe/Dublin", ie: "Europe/Dublin",
  germany: "Europe/Berlin", de: "Europe/Berlin", deutschland: "Europe/Berlin",
  france: "Europe/Paris", fr: "Europe/Paris",
  spain: "Europe/Madrid", es: "Europe/Madrid",
  italy: "Europe/Rome", it: "Europe/Rome",
  netherlands: "Europe/Amsterdam", nl: "Europe/Amsterdam", holland: "Europe/Amsterdam",
  belgium: "Europe/Brussels", be: "Europe/Brussels",
  switzerland: "Europe/Zurich", ch: "Europe/Zurich",
  austria: "Europe/Vienna", at: "Europe/Vienna",
  portugal: "Europe/Lisbon", pt: "Europe/Lisbon",
  poland: "Europe/Warsaw", pl: "Europe/Warsaw",
  sweden: "Europe/Stockholm", se: "Europe/Stockholm",
  norway: "Europe/Oslo", no: "Europe/Oslo",
  denmark: "Europe/Copenhagen", dk: "Europe/Copenhagen",
  finland: "Europe/Helsinki", fi: "Europe/Helsinki",
  russia: "Europe/Moscow", ru: "Europe/Moscow",
  // Americas
  us: "America/New_York", usa: "America/New_York",
  "united states": "America/New_York", "united states of america": "America/New_York",
  canada: "America/Toronto", ca: "America/Toronto",
  mexico: "America/Mexico_City", mx: "America/Mexico_City",
  brazil: "America/Sao_Paulo", br: "America/Sao_Paulo",
  argentina: "America/Argentina/Buenos_Aires", ar: "America/Argentina/Buenos_Aires",
  chile: "America/Santiago", cl: "America/Santiago",
  colombia: "America/Bogota", co: "America/Bogota",
  // Oceania
  australia: "Australia/Sydney", au: "Australia/Sydney",
  "new zealand": "Pacific/Auckland", nz: "Pacific/Auckland",
  // Africa
  "south africa": "Africa/Johannesburg", za: "Africa/Johannesburg",
  nigeria: "Africa/Lagos", ng: "Africa/Lagos",
  egypt: "Africa/Cairo", eg: "Africa/Cairo",
  kenya: "Africa/Nairobi", ke: "Africa/Nairobi",
};

export function inferTimezoneFromCountry(country: string): string {
  const key = country.toLowerCase().trim().replace(/\s+/g, " ");
  return COUNTRY_TO_TZ[key] ?? "Etc/UTC";
}

// ── USER.md rendering + writing ─────────────────────────────────────────

interface UserMdFields {
  name: string | null;
  country: string | null;
  occupation: string | null;
  use_case: string | null;
  timezone: string;
}

/**
 * Render the captured onboarding answers into a USER.md the agent will
 * load from the workspace at next session start. Keeps the same section
 * structure as the state-template's identity/USER.md so platform updates
 * don't surprise the tenant.
 */
export function renderUserMd(fields: UserMdFields): string {
  const today = new Date().toISOString().slice(0, 10);
  return `# USER.md

> Captured from the first-time conversation on ${today}. The user can
> ask Arete to update any of this at any point — Arete will edit the
> file in place.

---

## Identity

- Name: ${fields.name ?? "_unknown_"}
- Occupation: ${fields.occupation ?? "_unknown_"}
- Country: ${fields.country ?? "_unknown_"}
- Timezone: ${fields.timezone}

## Communication preferences

- Tone: casual, warm, direct
- Brevity: short — texting a friend, not authoring a document
- Language: English (default; user can override via "talk to me in <language>")
- Format: plain text. No Markdown.

## What this assistant is for

- ${fields.use_case ?? "_to be discovered_"}

## Working hours and availability

- Country / timezone: ${fields.country ?? "_unknown_"} / ${fields.timezone}
- Working hours: Mon-Fri 9am-6pm local (default; user can adjust)

## Trust posture

- Confirm before doing anything that sends a message, books a meeting, or spends money
- Act freely for read-only tasks: lookups, summaries, briefings
- For destructive actions (delete, unsubscribe, decline), always ask first

## Anti-patterns to avoid

- _to be discovered as user gives feedback_
`;
}

/**
 * Write USER.md into the tenant's volume via `docker exec`. The syncer
 * running in the container will pick it up on its next poll and commit
 * + push it to the tenant's state repo.
 *
 * Container must be running. ensureRunning + waitHealthy should be
 * called before this.
 */
export async function writeUserMdToContainer(
  containerName: string,
  userMd: string,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const p = spawn(
      "docker",
      [
        "exec",
        "-i",
        containerName,
        "sh",
        "-c",
        "cat > /home/agent/state/identity/USER.md",
      ],
      { stdio: ["pipe", "pipe", "pipe"] },
    );
    let stderr = "";
    p.stderr.on("data", (d: Buffer) => (stderr += d.toString("utf8")));
    p.on("close", (code) => {
      if (code === 0) {
        resolve();
      } else {
        log.warn(
          { containerName, exit: code, stderr: stderr.slice(0, 300) },
          "writeUserMdToContainer failed",
        );
        reject(new Error(`docker exec USER.md write exit ${code}`));
      }
    });
    p.stdin.write(userMd);
    p.stdin.end();
  });
}
