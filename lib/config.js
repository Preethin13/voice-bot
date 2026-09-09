export const MODEL = process.env.OPENAI_REALTIME_MODEL || "gpt-realtime-2.1";

export const VOICES = [
  { id: "marin", name: "Marin", gender: "female", blurb: "Warm, natural, easy to follow" },
  { id: "coral", name: "Coral", gender: "female", blurb: "Bright and articulate" },
  { id: "cedar", name: "Cedar", gender: "male", blurb: "Calm and grounded" },
  { id: "ash", name: "Ash", gender: "male", blurb: "Steady and clear" },
];

export const VOICE_IDS = new Set(VOICES.map((v) => v.id));

export const INSTRUCTIONS = [
  "You are Lumen, a highly capable live companion. You have tools that look up",
  "current weather and live world knowledge. Use them. Never claim you lack internet,",
  "a knowledge cutoff, or that you cannot look something up.",
  "Sound like a real person: warm, clear, and conversational.",
  "Answer what they just said. Do not use a customer-service closer.",
  "Never start a turn with phrases like 'need a hand with anything else',",
  "'anything else I can help with', or 'want me to look something else up'.",
  "Those lines are not greetings.",
  "If they say hi, greet them briefly and wait. Do not call tools for greetings.",
  "If they ask a question, give a precise answer in one to three natural sentences, then stop.",
  "Do not thank them every time. Do not ask if they need anything else on every reply.",
  "If they interrupt you, drop what you were saying and respond to the new utterance.",
  "If the audio is noise or an echo of yourself, stay silent.",
  "When they ask about weather, temperature, or conditions outside, call get_current_weather",
  "right away. Do not ask where they are. Leave place empty unless they named a city.",
  "For news, sports, science, people, history, prices, scores, how things work, or any fact",
  "you are not certain about, call lookup_world with a clear query. Then speak the answer",
  "from the tool, not from memory. If the tool errors, say you could not find it — do not invent.",
].join(" ");

export const TOOLS = [
  {
    type: "function",
    name: "get_current_weather",
    description:
      "Get live weather for the user. Call this whenever they ask about the weather, temperature, or conditions. Do not ask where they are. Omit place to use their detected location. Only pass place if they named a city or region.",
    parameters: {
      type: "object",
      properties: {
        place: {
          type: "string",
          description: "City or region they named. Omit to use detected location.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "lookup_world",
    description:
      "Look up live facts from the web: news, sports, science, people, history, prices, scores, how things work, current events, and anything you are not sure about. Call this instead of guessing or saying you do not know. Do not use this for greetings.",
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "The question or topic to research, with any names, dates, or places.",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
];

export function sessionConfig(voice) {
  const chosen = VOICE_IDS.has(voice) ? voice : "marin";
  return {
    type: "realtime",
    model: MODEL,
    output_modalities: ["audio"],
    instructions: INSTRUCTIONS,
    tools: TOOLS,
    tool_choice: "auto",
    audio: {
      input: {
        format: { type: "audio/pcm", rate: 24000 },
        transcription: { model: "gpt-4o-mini-transcribe" },
        noise_reduction: { type: "near_field" },
        turn_detection: {
          type: "semantic_vad",
          eagerness: "medium",
          interrupt_response: true,
          create_response: true,
        },
      },
      output: {
        format: { type: "audio/pcm", rate: 24000 },
        voice: chosen,
        speed: 0.96,
      },
    },
  };
}
