const WMO = {
  0: "clear",
  1: "mainly clear",
  2: "partly cloudy",
  3: "overcast",
  45: "foggy",
  48: "foggy",
  51: "light drizzle",
  53: "drizzle",
  55: "heavy drizzle",
  61: "light rain",
  63: "rain",
  65: "heavy rain",
  71: "light snow",
  73: "snow",
  75: "heavy snow",
  80: "rain showers",
  81: "rain showers",
  82: "heavy rain showers",
  85: "snow showers",
  86: "heavy snow showers",
  95: "thunderstorms",
  96: "thunderstorms with hail",
  99: "thunderstorms with hail",
};

function clientIp(req) {
  const forwarded = req.headers["x-forwarded-for"] || req.headers["x-real-ip"] || "";
  return String(forwarded).split(",")[0].trim() || "";
}

function isPublicIp(value) {
  if (!value) return false;
  if (value === "127.0.0.1" || value === "::1") return false;
  if (value.startsWith("10.") || value.startsWith("192.168.") || value.startsWith("172.")) return false;
  return true;
}

async function getJson(url) {
  const response = await fetch(url, { headers: { "User-Agent": "lumen-weather/1.0" } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function weather({ place, ip }) {
  let loc;
  if (place && String(place).trim()) {
    const q = encodeURIComponent(String(place).trim());
    const data = await getJson(`https://geocoding-api.open-meteo.com/v1/search?name=${q}&count=1&language=en`);
    const hit = (data.results || [])[0];
    if (!hit) return JSON.stringify({ error: `Could not find ${place}.` });
    loc = {
      latitude: hit.latitude,
      longitude: hit.longitude,
      label: [hit.name, hit.admin1, hit.country].filter(Boolean).join(", "),
      country_code: String(hit.country_code || "").toUpperCase().slice(0, 2),
    };
  } else {
    const query = isPublicIp(ip) ? ip : "";
    let data = await getJson(`https://ipwho.is/${query}`);
    if (data.success === false) data = await getJson("https://ipapi.co/json/");
    if (data.latitude == null || data.longitude == null) {
      return JSON.stringify({ error: "Could not detect location. Ask for a city name." });
    }
    const city = data.city || data.region || "your area";
    loc = {
      latitude: Number(data.latitude),
      longitude: Number(data.longitude),
      label: [city, data.region || data.regionName, data.country_code || data.country]
        .filter(Boolean)
        .join(", "),
      country_code: String(data.country_code || data.country || "").toUpperCase().slice(0, 2),
    };
  }
  const useF = ["US", "BS", "BZ", "KY", "PW"].includes(loc.country_code);
  const params = new URLSearchParams({
    latitude: String(loc.latitude),
    longitude: String(loc.longitude),
    current:
      "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
    temperature_unit: useF ? "fahrenheit" : "celsius",
    wind_speed_unit: useF ? "mph" : "kmh",
    precipitation_unit: useF ? "inch" : "mm",
    timezone: "auto",
  });
  const forecast = await getJson(`https://api.open-meteo.com/v1/forecast?${params}`);
  const current = forecast.current || {};
  const code = Number(current.weather_code || 0);
  return JSON.stringify({
    place: loc.label,
    condition: WMO[code] || "unknown conditions",
    temperature: current.temperature_2m,
    feels_like: current.apparent_temperature,
    unit: useF ? "F" : "C",
    humidity_percent: current.relative_humidity_2m,
    wind: current.wind_speed_10m,
    wind_unit: useF ? "mph" : "km/h",
    precipitation: current.precipitation,
  });
}

function outputText(data) {
  if (data.output_text) return data.output_text.trim();
  const chunks = [];
  for (const item of data.output || []) {
    if (item.type !== "message") continue;
    for (const part of item.content || []) {
      if (part.type === "output_text" || part.type === "text") chunks.push(part.text || "");
    }
  }
  return chunks.join("\n").trim();
}

async function lookup(query, apiKey) {
  const topic = String(query || "").trim();
  if (!topic) return JSON.stringify({ error: "Empty lookup query." });
  const prompt =
    "Research this with live web search. Return accurate current facts only. " +
    "Include key names, numbers, dates, and places. Under 150 words.\n\n" +
    topic;
  const models = [process.env.OPENAI_LOOKUP_MODEL || "gpt-4.1-mini", "gpt-4o-mini", "gpt-4o"];
  const seen = new Set();
  for (const model of models) {
    if (seen.has(model)) continue;
    seen.add(model);
    const response = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        tools: [{ type: "web_search" }],
        input: prompt,
      }),
    });
    if (!response.ok) continue;
    const text = outputText(await response.json());
    if (text) return JSON.stringify({ answer: text.slice(0, 4000), source: "live_web" });
  }
  return JSON.stringify({ error: "No live results found." });
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }
  const apiKey = process.env.OPENAI_API_KEY || "";
  const name = req.body?.name || "";
  let args = req.body?.arguments || {};
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      args = {};
    }
  }
  try {
    let output = JSON.stringify({ error: `Unknown tool ${name}` });
    if (name === "get_current_weather") {
      output = await weather({ place: args.place, ip: clientIp(req) });
    } else if (name === "lookup_world") {
      output = await lookup(args.query, apiKey);
    }
    res.status(200).json({ output });
  } catch (err) {
    res.status(200).json({ output: JSON.stringify({ error: String(err.message || err) }) });
  }
}
