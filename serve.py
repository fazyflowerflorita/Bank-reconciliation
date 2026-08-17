#!/usr/bin/env python3
"""
Simple HTTP server to serve the reconciliation website locally
No data is stored - everything runs in the browser
"""

import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

class CORSRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

if __name__ == '__main__':
    port = 8000
    server_address = ('', port)

    # Change to the directory containing this script
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    httpd = HTTPServer(server_address, CORSRequestHandler)

    print(f"""
╔════════════════════════════════════════════════════════════════╗
║      Intelligent Bank Reconciliation Website Server            ║
╚════════════════════════════════════════════════════════════════╝

✅ Server is running!

🌐 Open your browser and go to:
   http://localhost:{port}/reconciliation_website.html

📁 Files will be served from:
   {script_dir}

🛑 To stop the server: Press Ctrl+C

🔒 Privacy: All processing happens in your browser
           No files are sent to this server
           Data is cleared when you close the browser

Notes:
- Make sure you're in the correct directory
- Files must be in the same folder as this script
- Use Ctrl+C to stop the server
""")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n✅ Server stopped")
        sys.exit(0)
