// Serves index.html and forwards /api/systemone to the Jev API.
// The Jev API rejects browser (CORS) requests, so the page talks to this server instead.
//
// API key: set TYPESAFE_API_KEY (in .env or the environment) and the server adds it to every
// request, so the browser never sees it. Without it, the page must send its own key.
const http = require("http");
const fs = require("fs");
const path = require("path");

// Load .env when running outside Docker (Compose injects it via env_file instead).
try { process.loadEnvFile(path.join(__dirname, ".env")); } catch { /* no .env file */ }

const PORT = process.env.PORT || 8787;
const HOST = process.env.HOST || "127.0.0.1";
const UPSTREAM = "https://api.typesafe.ai/v1/systemone";
const SERVER_KEY = (process.env.TYPESAFE_API_KEY || "").trim();

http.createServer(async (req, res) => {
  // With a server-side key, only same-origin pages (http://localhost:PORT) may call us.
  // Otherwise any website open in your browser could spend your key through this proxy.
  if (!SERVER_KEY) {
    // Browser-key mode: allow the page to work when opened as a local file.
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
  }

  if (req.method === "GET" && (req.url === "/" || req.url === "/index.html")) {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    fs.createReadStream(path.join(__dirname, "index.html")).pipe(res);
    return;
  }

  if (req.method === "GET" && req.url === "/api/config") {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(JSON.stringify({ serverKey: Boolean(SERVER_KEY) }));
    return;
  }

  if (req.method === "POST" && req.url === "/api/systemone") {
    if (SERVER_KEY && req.headers.origin && req.headers.origin !== `http://${req.headers.host}`) {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Requests from other origins are blocked when the server holds the API key." }));
      return;
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    // A key typed into the page takes priority; otherwise use the one from .env.
    const auth = req.headers.authorization || (SERVER_KEY ? `Bearer ${SERVER_KEY}` : "");
    try {
      const upstream = await fetch(UPSTREAM, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": auth },
        body: Buffer.concat(chunks)
      });
      res.writeHead(upstream.status, {
        "Content-Type": upstream.headers.get("content-type") || "application/json"
      });
      res.end(Buffer.from(await upstream.arrayBuffer()));
    } catch (e) {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Could not reach the Jev API", detail: String(e) }));
    }
    return;
  }

  res.writeHead(404);
  res.end("Not found");
}).listen(PORT, HOST, () => {
  console.log(`Jev playground running at http://localhost:${PORT}`);
  console.log(SERVER_KEY ? "Using TYPESAFE_API_KEY from the environment." : "No TYPESAFE_API_KEY set: paste a key in the page.");
});
