from http.server import BaseHTTPRequestHandler
import json
import os


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps(
            {
                "status": "ok",
                "service": "swarm-city",
                "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
            }
        )
        self.wfile.write(body.encode())
