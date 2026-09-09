# Lumen

Realtime voice + text companion. Pick a voice, type or talk, interrupt anytime. It looks up live weather from your connection and live web facts when you ask about the world.

## What to upload to GitHub

Upload this folder. **Do not upload** `.env` or `.venv` (they are in `.gitignore`).

```
voice-chatbot/
  server.py              # web app
  realtime_config.py     # voices + instructions
  weather.py             # weather lookup
  knowledge.py           # live web lookup
  voice_chatbot.py       # optional terminal OpenAI voice
  claude_voice.py        # optional Claude + Whisper + macOS say
  requirements.txt
  Dockerfile
  .dockerignore
  .gitignore
  .env.example           # copy to .env — never commit real keys
  static/
    index.html
    app.js
    styles.css
    sw.js
    manifest.webmanifest
```

## Run locally

You need Python 3.10+, an [OpenAI API key](https://platform.openai.com/api-keys) with billing, and a microphone if you use voice.

```bash
cd voice-chatbot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY`. Then:

```bash
python server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Type in the conversation box, or click **Start talking**.

## Deploy on Vercel

This app uses short API routes plus the browser for voice, which Vercel supports.

1. Push the repo to GitHub.
2. Go to [vercel.com/new](https://vercel.com/new) and import **Voice-Bot**.
3. Add environment variable `OPENAI_API_KEY`.
4. Deploy. Your link looks like `https://voice-bot-….vercel.app`.

Or from this folder:

```bash
npx vercel login
npx vercel --prod
npx vercel env add OPENAI_API_KEY
```

Then redeploy after adding the key.

## Put it on GitHub

On [github.com/new](https://github.com/new), create an empty repo (no README). Then:

```bash
cd voice-chatbot
git add .
git status    # .env and .venv must not appear
git commit -m "Add Lumen voice chatbot"
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Replace `YOUR_USER` and `YOUR_REPO`. If GitHub asks you to sign in, use a personal access token or GitHub CLI (`gh auth login`).

## Docker (optional)

```bash
docker build -t lumen .
docker run --rm -p 8000:8000 --env-file .env lumen
```

## Terminal apps (optional)

Claude (Whisper + `say` on macOS) needs `ANTHROPIC_API_KEY` and PortAudio (`brew install portaudio`):

```bash
python voice_chatbot.py --provider claude
python voice_chatbot.py --provider openai --voice marin
```
