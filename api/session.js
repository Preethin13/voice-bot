import { MODEL, sessionConfig, VOICE_IDS } from "../lib/config.js";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }
  const apiKey = process.env.OPENAI_API_KEY || "";
  if (!apiKey) {
    res.status(500).json({ error: "Set OPENAI_API_KEY in the Vercel project." });
    return;
  }
  const voice = VOICE_IDS.has(req.body?.voice) ? req.body.voice : "marin";
  const response = await fetch("https://api.openai.com/v1/realtime/client_secrets", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ session: sessionConfig(voice) }),
  });
  const data = await response.json();
  if (!response.ok) {
    const message = data?.error?.message || "Could not start a session.";
    res.status(502).json({ error: message });
    return;
  }
  const value = data.value || data.client_secret?.value;
  if (!value) {
    res.status(502).json({ error: "OpenAI did not return a session key." });
    return;
  }
  res.status(200).json({ value, model: MODEL });
}
