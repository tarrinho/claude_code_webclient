#!/usr/bin/env python3
"""Simple HTTPS server serving the supervisor mockup."""
import http.server
import os
import ssl

os.chdir("/home/kali/projects/claude-code-webconsole/docs/superpowers/plans")
port = 8443
handler = http.server.SimpleHTTPRequestHandler

httpd = http.server.HTTPServer(("0.0.0.0", port), handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/tmp/https-cert/cert.pem", "/tmp/https-cert/key.pem")
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

print(f"Serving at https://kali-2.tail850c40.ts.net:{port}/")
print(f"Browse the mockup: https://kali-2.tail850c40.ts.net:{port}/supervisor-layout-c-resizable.html")
print("Press Ctrl+C to stop.")
httpd.ssl_context = ctx
httpd.serve_forever()