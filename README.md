# Jev API Playground

A small web page for trying out TypeSafe's [Jev](https://developers.cloudflare.com/ai/models/typesafe/jev/) System One API. You give it some text (the *state*) plus typed questions, and it shows Jev's answers with probability bars:

- **choice**: pick one option from a set
- **score**: rate on a scale
- **noul**: yes or no

## Setup: put your API key in `.env`

Open the `.env` file in this folder and paste your key after the `=`:

```sh
TYPESAFE_API_KEY=your-key-here
```

The server adds the key to every request, so you never paste it in the browser. `.env` is listed in `.gitignore` and `.dockerignore`, so it stays out of git and out of the Docker image. If `.env` is missing, copy `.env.example` to `.env`.

## Option A: run in Docker (recommended)

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/), and it must be running.

1. Open a terminal in this folder:
   ```sh
   cd "/Users/rohitshidid/Documents/AntiGravity/Test projects/Jev Ai"
   ```
2. Build and start the container:
   ```sh
   docker compose up -d --build
   ```
3. Open http://localhost:8787. The key field should say **Using the key from .env**.
4. Pick an example (Support triage, Urgency check or Review sentiment), or write your own state and questions.
5. Click **Run** (or press ⌘+Enter). The answers show on the right, and the full JSON is under **Raw response**.

Useful commands:

| Task | Command |
| --- | --- |
| Show logs | `docker compose logs -f` |
| Stop | `docker compose down` |
| Apply an edited `.env` | `docker compose up -d` (Compose recreates the container) |
| Apply edits to `index.html` or `server.js` | `docker compose up -d --build` |

The container accepts connections from your own machine only (`127.0.0.1:8787`), so other devices on your network can't use your key.

## Option B: run with Node directly

Requires [Node.js](https://nodejs.org/) 20.12 or newer. No `npm install` needed.

```sh
node server.js
```

The server reads `.env` on its own. Open http://localhost:8787 and follow steps 4–5 above. Press `Ctrl+C` to stop it. To use a different port, run `PORT=3000 node server.js`.

## Using the page without a key in `.env`

If `TYPESAFE_API_KEY` is empty, the page asks you to paste a key instead, and it's sent with each request. A key pasted in the page also overrides the `.env` key for that page.

## Why there's a server

Jev's API refuses requests made directly from a web page (its CORS policy blocks them), so the page can't call it on its own. `server.js` serves the page and passes each request on to `https://api.typesafe.ai/v1/systemone`.

When the key is in `.env`, the server only accepts requests from the page it serves itself (http://localhost:8787). Requests from other websites get `403`, so a page you visit elsewhere can't spend your key through the server. As a result, opening `index.html` straight from disk only works in paste-a-key mode.

## Files

| File | What it does |
| --- | --- |
| `index.html` | The demo page: example requests, the request editor and the answer display |
| `server.js` | A small Node server that serves the page and forwards `/api/systemone` to the Jev API, adding the key from `.env` |
| `.env` | Your API key (not committed) |
| `.env.example` | Template for `.env` |
| `Dockerfile`, `docker-compose.yml` | The container setup |

## Writing questions

`questions` is a JSON object. Each key names a question, and each value sets its type:

```json
{
  "department": {
    "type": "choice",
    "instructions": "Which team should handle this ticket?",
    "criteria": { "billing": "Payment issues", "technical": "Bugs", "sales": "Pricing" }
  },
  "frustration": {
    "type": "score",
    "instructions": "How frustrated does the customer seem?",
    "criteria": ["Calm", "Frustrated but civil", "Very angry"]
  },
  "refund_requested": {
    "type": "noul",
    "instructions": "Is the customer asking for a refund?",
    "criteria": { "true": "Clearly asks for money back", "false": "No refund request" }
  }
}
```

The state can be plain text or JSON. The **Model** field is required and defaults to `jev-latest`.

## Calling the API without the page

**Copy as curl** copies the current request as a terminal command that calls the Jev API directly. Set your key first:

```sh
export TYPESAFE_API_KEY=your-key-here
```

## Troubleshooting

| What you see | What to do |
| --- | --- |
| **Couldn't reach the local server** | Start it (`docker compose up -d` or `node server.js`) and reload the page. |
| `HTTP 401`, `403` or `authentication_error` from Jev | The key in `.env` is missing or wrong. Fix it, then run `docker compose up -d`. |
| `Requests from other origins are blocked` | Open the page at http://localhost:8787 instead of opening the file directly. |
| `Cannot connect to the Docker daemon` | Start Docker Desktop and try again. |
| The page still looks old | Hard-reload with ⌘+Shift+R. |
| `HTTP 400` or `422` | The request doesn't match what Jev expects. Check **Raw response** for details. |
| `EADDRINUSE` or `port is already allocated` | Something else is using port 8787. Stop it, or change the port (`PORT=3000 node server.js`, or the left-hand port in `docker-compose.yml`). |
