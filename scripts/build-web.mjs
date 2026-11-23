// Builds the static PRISM console into dist/ for Vercel.
// Copies web/*, the shared apps.json, config.example.json and writes meta.json (build time + commit).
import { cpSync, mkdirSync, rmSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { execSync } from "node:child_process";

const out = "dist";
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync("web", out, { recursive: true });
for (const f of ["apps.json", "config.example.json", "README.md"]) if (existsSync(f)) cpSync(f, `${out}/${f}`);

let sha = process.env.VERCEL_GIT_COMMIT_SHA || "";
let msg = process.env.VERCEL_GIT_COMMIT_MESSAGE || "";
try { if (!sha) sha = execSync("git rev-parse HEAD").toString().trim(); } catch {}
try { if (!msg) msg = execSync("git log -1 --pretty=%s").toString().trim(); } catch {}

const apps = JSON.parse(readFileSync("apps.json", "utf8")).apps ?? [];
writeFileSync(`${out}/meta.json`, JSON.stringify({
  built_at: new Date().toISOString(),
  commit: sha.slice(0, 7),
  commit_message: msg,
  app_count: apps.length,
  repo: "https://github.com/meloxcyber/prism",
}, null, 2));
console.log(`PRISM console built: ${apps.length} apps, commit ${sha.slice(0, 7) || "(local)"}`);
