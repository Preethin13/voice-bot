import { VOICES } from "../lib/config.js";

export default async function handler(req, res) {
  res.status(200).json(VOICES);
}
